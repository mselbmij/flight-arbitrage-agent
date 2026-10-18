"""`fares` CLI (ARCHITECTURE.md section 7): migrate, seed, targets, budget, key.

Environment is read when a command runs, never at import time. Every command that touches the
database applies migrations first.
"""

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
import yaml
from dotenv import load_dotenv

from src.db import BudgetLimits, Database
from src.models import ResolvedWatchlist, WatchlistFile
from src.runner.seed import SeedScenario, generate

app = typer.Typer(help="Risk-aware airline fare tracker.", no_args_is_help=True)

DEFAULT_DB = "data/fares.db"
DEFAULT_CONFIG = "config/watchlists.yaml"

DbOpt = Annotated[Path | None, typer.Option("--db", help="SQLite path (default $FARES_DB_PATH).")]
ConfigOpt = Annotated[
    Path,
    typer.Option("--config", exists=True, dir_okay=False, help="Watchlist YAML."),
]


def _db_path(db: Path | None) -> Path:
    load_dotenv()
    path = db or Path(os.environ.get("FARES_DB_PATH", DEFAULT_DB))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load(config: Path) -> WatchlistFile:
    return WatchlistFile.model_validate(yaml.safe_load(config.read_text()))


def _open(db: Path | None) -> Database:
    return Database(_db_path(db))


@app.command()
def migrate(db: DbOpt = None) -> None:
    """Apply pending migrations and print the versions applied."""
    with _open(db) as d:
        applied = d.migrate()
    typer.echo(f"applied: {', '.join(applied)}" if applied else "up to date")


@app.command()
def seed(
    db: DbOpt = None,
    config: ConfigOpt = Path(DEFAULT_CONFIG),
    days: Annotated[int, typer.Option(min=1, help="Days of history per target.")] = 45,
    end: Annotated[
        str | None, typer.Option(help="ISO timestamp of the last observation (default: now).")
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Delete existing seed data first.")
    ] = False,
) -> None:
    """Fill the database with synthetic history for every pollable target.

    Idempotent with --replace; refuses to run on top of existing seed data without it."""
    end_at = datetime.fromisoformat(end) if end else datetime.now(UTC)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=UTC)
    end_at = end_at.astimezone(UTC)

    wf = _load(config)
    totals = {"runs": 0, "api_calls": 0, "observations": 0, "benchmark_bands": 0}
    unbounded = BudgetLimits(max_per_run=10**9, max_per_day=10**9)
    with _open(db) as d:
        d.migrate()
        existing = d.con.execute("SELECT COUNT(*) FROM runs WHERE \"trigger\"='seed'").fetchone()[0]
        if existing and not replace:
            typer.echo(
                f"{existing} seed runs already present; re-run with --replace to regenerate.",
                err=True,
            )
            raise typer.Exit(1)
        if existing:
            typer.echo(f"replaced {d.delete_runs_by_trigger('seed')} seed runs")
        try:
            for wl in wf.resolved():
                for target in wl.pollable_targets:
                    scenario = _scenario(target, wl, days, end_at)
                    runs, calls, obs, bands = generate(
                        scenario, watchlist=wl, run_id_prefix=f"seed-{target.target_id}"
                    )
                    for run, call in zip(runs, calls, strict=True):
                        # reserve_api_call bumps calls_made itself
                        d.start_run(run.model_copy(update={"calls_made": 0}))
                        d.reserve_api_call(call, unbounded)
                    d.insert_observations(obs)
                    for band in bands:
                        d.insert_band(band)
                    totals["runs"] += len(runs)
                    totals["api_calls"] += len(calls)
                    totals["observations"] += len(obs)
                    totals["benchmark_bands"] += len(bands)
        except sqlite3.IntegrityError as e:
            typer.echo(f"seed data already present or inconsistent: {e}", err=True)
            raise typer.Exit(1) from e
    typer.echo("seeded: " + " ".join(f"{k}={v}" for k, v in totals.items()))


def _scenario(target, wl: ResolvedWatchlist, days: int, end: datetime) -> SeedScenario:
    base = max(wl.ceiling_cents * 90 // 100, 1)
    return SeedScenario(
        target=target,
        days=days,
        end=end,
        base_price_cents=base,
        typical_low_cents=base * 90 // 100,
        typical_high_cents=base * 125 // 100,
    )


@app.command()
def targets(
    watchlist_id: Annotated[str | None, typer.Argument(help="Only this watchlist.")] = None,
    config: ConfigOpt = Path(DEFAULT_CONFIG),
    db: DbOpt = None,
) -> None:
    """List the expanded grid: target, regime, days to departure, last polled."""
    wf = _load(config)
    resolved = [w for w in wf.resolved() if watchlist_id in (None, w.id)]
    if watchlist_id is not None and not resolved:
        typer.echo(f"unknown watchlist: {watchlist_id}", err=True)
        raise typer.Exit(2)
    all_targets = [t for w in resolved for t in w.targets]
    with _open(db) as d:
        d.migrate()
        polled = d.last_polled([t.target_id for t in all_targets])
    typer.echo("target_id  regime  DTD  last_polled")
    for w in resolved:
        for t in w.targets:
            lp = polled[t.target_id]
            stamp = lp.strftime("%Y-%m-%d %H:%M") if lp else "-"
            typer.echo(f"{t.target_id}  {w.regime_for(t).value}  {t.days_to_departure()}  {stamp}")


@app.command()
def budget(config: ConfigOpt = Path(DEFAULT_CONFIG)) -> None:
    """Compare the config's calls/day with MAX_DAILY_API_CALLS; exit 1 if over."""
    load_dotenv()
    limit = int(os.environ.get("MAX_DAILY_API_CALLS", "4"))
    calls = _load(config).calls_per_day()
    over = calls > limit
    typer.echo(f"calls/day: {calls}  limit: {limit}  ({'OVER' if over else 'ok'})")
    if over:
        raise typer.Exit(1)


@app.command()
def key(obs_id: int, db: DbOpt = None) -> None:
    """Print an observation's itinerary_key and target_id (for a `purchase:` block)."""
    with _open(db) as d:
        d.migrate()
        obs = d.observation(obs_id)
    if obs is None:
        typer.echo(f"no observation with id {obs_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"itinerary_key: {obs.itinerary_key}")
    typer.echo(f"target_id: {obs.target_id}")


if __name__ == "__main__":
    app()
