"""`fares` CLI (ARCHITECTURE.md sections 7, 7a): migrate, seed, targets, poll, budget, key,
alert-test, health.

Environment is read when a command runs, never at import time. Every command that touches the
database applies migrations first.
"""

import errno
import logging
import os
import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml
from dotenv import load_dotenv
from pydantic import ValidationError

from src.alerts.notifier import LogNotifier, NtfyNotifier
from src.db import BudgetLimits, Database, utcnow
from src.engine.rules import fmt_money
from src.ingestion.amadeus_client import AmadeusProvider
from src.ingestion.mock_client import MockProvider
from src.ingestion.serpapi_client import SerpApiProvider
from src.models import Alert, AlertKind, Provider, Regime, ResolvedWatchlist, WatchlistFile
from src.runner import health as health_checks
from src.runner.demo import demo_watchlist_file, write_demo_config
from src.runner.poll import Notifier, NullNotifier, ProviderRegistry, run_poll
from src.runner.seed import SeedScenario, generate

log = logging.getLogger(__name__)

app = typer.Typer(help="Risk-aware airline fare tracker.", no_args_is_help=True)

DEFAULT_DB = "data/fares.db"
DEFAULT_SEED_DB = "data/seed.db"
DEFAULT_CONFIG = "config/watchlists.yaml"
DEFAULT_DEMO_CONFIG = "config/watchlists.demo.yaml"
DEFAULT_RAW_DIR = "data/raw"
# Budget defaults (ARCHITECTURE.md section 6), shared by `fares poll` and `fares budget`.
DEFAULT_MAX_CALLS_PER_RUN = 2
DEFAULT_MAX_DAILY_API_CALLS = 3
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
DEFAULT_HEALTH_MAX_AGE_HOURS = 36
DEFAULT_BACKUP_DIR = "data/backups"
#: ntfy priority for operational pings that need a human to fix something (high, not urgent).
ACTION_PRIORITY = 4

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


class NotifierChoice(StrEnum):
    NTFY = "ntfy"
    LOG = "log"
    NULL = "null"


class PingNotifierChoice(StrEnum):
    NTFY = "ntfy"
    LOG = "log"


def _ntfy_from_env() -> NtfyNotifier | None:
    """NTFY_SERVER / NTFY_TOPIC / NTFY_TOKEN; None when no topic is configured."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return None
    return NtfyNotifier(
        os.environ.get("NTFY_SERVER") or DEFAULT_NTFY_SERVER,
        topic,
        os.environ.get("NTFY_TOKEN") or None,
    )


def _notifier(choice: NotifierChoice | PingNotifierChoice | None) -> Notifier:
    """Build the notifier for `--notifier`. None = the poll default: ntfy when NTFY_TOPIC is
    set, otherwise the null notifier with a warning. An explicit `ntfy` without a topic is
    an error, never a silent downgrade."""
    load_dotenv()
    if choice == NotifierChoice.NULL:
        return NullNotifier()
    if choice == NotifierChoice.LOG:
        return LogNotifier()
    ntfy = _ntfy_from_env()
    if ntfy is not None:
        return ntfy
    if choice is None:
        msg = "NTFY_TOPIC is not set: alerts are stored undelivered (use --notifier log to print)"
        log.warning(msg)
        typer.echo(f"warning: {msg}", err=True)
        return NullNotifier()
    typer.echo("error: --notifier ntfy needs NTFY_TOPIC (see .env.example)", err=True)
    raise typer.Exit(1)


def _ping(sink: Notifier, key: str, title: str, body: str, priority: int) -> str | None:
    """Send an operational message that is not a stored alert (it never touches the DB)."""
    alert = Alert(
        created_at=utcnow(),
        watchlist_id="fare-tracker",
        kind=AlertKind.PROVIDER_ERROR,
        regime=Regime.MONITOR,
        message=f"{title}\n{body}",
        alert_key=f"fare-tracker:{key}",
    )
    try:
        return sink.send(alert, priority=priority)
    except Exception:  # the ping is best effort; the exit code still reports the failure
        log.exception("could not send %r", title)
        return None


def _poll_failed(sink: Notifier, message: str, fix: str) -> NoReturn:
    """`fares poll` cannot do its job: say so on stderr (ops/poll.log) and on the phone."""
    typer.echo(message, err=True)
    _ping(
        sink, "poll-failed", "fare tracker: poll failed", f"{message}\nFix: {fix}", ACTION_PRIORITY
    )
    raise typer.Exit(1)


def _fix_for(e: BaseException) -> str:
    """One line telling the human what to do about an exception that stopped a poll."""
    text = str(e).lower()
    if (isinstance(e, OSError) and e.errno == errno.ENOSPC) or "disk is full" in text:
        return "disk full: prune data/raw/ and data/backups/, then run `fares poll` by hand"
    if isinstance(e, sqlite3.OperationalError) and "locked" in text:
        return "database locked: look for a stuck `fares poll` (ps aux | grep fares) and kill it"
    if isinstance(e, sqlite3.DatabaseError) and ("malformed" in text or "not a database" in text):
        return (
            "database corrupt: restore the newest data/backups/ file (README, Backup and restore)"
        )
    if isinstance(e, PermissionError):
        return "permission denied: the cron user must own data/ and ops/"
    if isinstance(e, yaml.YAMLError | ValidationError):
        return "config/watchlists.yaml is invalid: fix it, then check with `fares budget`"
    return "see the traceback in ops/poll.log; run `fares poll` by hand to reproduce"


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
    db: Annotated[
        Path | None,
        typer.Option("--db", help=f"SQLite path (default {DEFAULT_SEED_DB}, NOT $FARES_DB_PATH)."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            exists=True,
            dir_okay=False,
            help="Watchlist YAML to seed (default: built-in demo watchlists).",
        ),
    ] = None,
    days: Annotated[int, typer.Option(min=1, help="Days of history per target.")] = 45,
    end: Annotated[
        str | None, typer.Option(help="ISO timestamp of the last observation (default: now).")
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Delete existing seed data first.")
    ] = False,
    write_config: Annotated[
        Path,
        typer.Option(help="Where to write the demo watchlists (only when --config is omitted)."),
    ] = Path(DEFAULT_DEMO_CONFIG),
    allow_live_db: Annotated[
        bool,
        typer.Option("--allow-live-db", help="Seed the live database ($FARES_DB_PATH) anyway."),
    ] = False,
) -> None:
    """Fill a SEPARATE database with synthetic history for every pollable target.

    Without --config the demo watchlists are used (departures relative to --end) and written to
    config/watchlists.demo.yaml (--write-config) so a dashboard can load the pair. The default
    database is data/seed.db, never the live one: synthetic rows must not become a strike
    reference. Refuses to write to $FARES_DB_PATH (default data/fares.db) without
    --allow-live-db. Idempotent with --replace; refuses to run on top of existing seed data
    without it."""
    end_at = datetime.fromisoformat(end) if end else datetime.now(UTC)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=UTC)
    end_at = end_at.astimezone(UTC)

    load_dotenv()
    db_path = db or Path(DEFAULT_SEED_DB)
    live = Path(os.environ.get("FARES_DB_PATH", DEFAULT_DB))
    if db_path.resolve() == live.resolve() and not allow_live_db:
        typer.echo(
            f"refusing to seed {db_path}: it is the live database ($FARES_DB_PATH). Synthetic "
            "history there would be read as real prices and become a strike reference. Seed a "
            "separate file (the default is data/seed.db) or pass --allow-live-db.",
            err=True,
        )
        raise typer.Exit(1)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    wf = demo_watchlist_file(end_at) if config is None else _load(config)
    totals = {"runs": 0, "api_calls": 0, "observations": 0, "benchmark_bands": 0}
    unbounded = BudgetLimits(max_per_run=10**9, max_per_day=10**9)
    with Database(db_path) as d:
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
    if config is None:
        write_demo_config(write_config, wf)
        typer.echo(f"wrote {write_config}")
    typer.echo("seeded: " + " ".join(f"{k}={v}" for k, v in totals.items()))


# Where the generator puts the one-day dip and the Basic Economy trap, in days to departure.
DIP_DTD = 34
TRAP_DTD = 30


def _scenario(target, wl: ResolvedWatchlist, days: int, end: datetime) -> SeedScenario:
    """Scenario for one target. The dip and trap are clamped into the DTD range the history
    covers (dtd_at_end .. dtd_at_end + days - 1): a target far enough out that DTD 34 / 30 are
    behind it gets them at dtd_at_end + 4 / + 8, so every seeded target has both."""
    first = (target.depart - end.date()).days  # DTD of the last observation
    last = first + days - 1  # DTD of the first observation

    def clamp(configured: int, fallback_offset: int) -> int:
        if first <= configured <= last:
            return configured
        return min(first + fallback_offset, last)

    base = max(wl.ceiling_cents * 90 // 100, 1)
    return SeedScenario(
        target=target,
        days=days,
        end=end,
        base_price_cents=base,
        typical_low_cents=base * 90 // 100,
        typical_high_cents=base * 125 // 100,
        dip_at_dtd=clamp(DIP_DTD, 4),
        basic_economy_trap_at_dtd=clamp(TRAP_DTD, 8),
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


class Trigger(StrEnum):
    CRON = "cron"
    MANUAL = "manual"


def _amadeus_configured() -> bool:
    return bool(os.environ.get("AMADEUS_CLIENT_ID") and os.environ.get("AMADEUS_CLIENT_SECRET"))


def _registry_from_env() -> ProviderRegistry:
    providers: dict[Provider, object] = {}
    if key := os.environ.get("SERPAPI_KEY"):
        providers[Provider.SERPAPI] = SerpApiProvider(key)
    client_id = os.environ.get("AMADEUS_CLIENT_ID")
    client_secret = os.environ.get("AMADEUS_CLIENT_SECRET")
    if client_id and client_secret:
        host = os.environ.get("AMADEUS_HOST")
        extra = {"host": host} if host else {}
        providers[Provider.AMADEUS] = AmadeusProvider(client_id, client_secret, **extra)
    return ProviderRegistry(providers)  # type: ignore[arg-type]


def _with_mock_only(wf: WatchlistFile) -> WatchlistFile:
    """Every watchlist polls [mock] and nothing else."""
    watchlists = [
        w.model_copy(
            update={
                "poll": (w.poll or wf.defaults.poll).model_copy(
                    update={"providers": [Provider.MOCK]}
                )
            }
        )
        for w in wf.watchlists
    ]
    return wf.model_copy(update={"watchlists": watchlists})


@app.command()
def poll(
    db: DbOpt = None,
    config: ConfigOpt = Path(DEFAULT_CONFIG),
    trigger: Annotated[Trigger, typer.Option(help="Recorded on the run.")] = Trigger.MANUAL,
    provider_override: Annotated[
        str | None,
        typer.Option(
            "--provider-override",
            help="Replace every watchlist's providers. Only 'mock' (needs --fixtures).",
        ),
    ] = None,
    fixtures: Annotated[
        Path | None,
        typer.Option("--fixtures", exists=True, file_okay=False, help="Mock fixture directory."),
    ] = None,
    raw_dir: Annotated[
        Path, typer.Option("--raw-dir", help="Where raw provider payloads are written.")
    ] = Path(DEFAULT_RAW_DIR),
    allow_live_db: Annotated[
        bool,
        typer.Option(
            "--allow-live-db",
            help="Poll the live database anyway: with --provider-override, or with Amadeus "
            "credentials on a non-production AMADEUS_HOST.",
        ),
    ] = False,
    notifier: Annotated[
        NotifierChoice | None,
        typer.Option(
            "--notifier",
            help="ntfy | log | null. Default: ntfy when NTFY_TOPIC is set, else null (warns).",
        ),
    ] = None,
) -> None:
    """Run one poll: every pollable target, every provider, risk filters, rules, alerts.

    Exit 0 ok, 2 budget tripped, 1 error. --provider-override mock still spends the breaker, so
    it refuses the live database ($FARES_DB_PATH) without --allow-live-db: mock rows there would
    become a strike reference. So does Amadeus with AMADEUS_HOST other than 'production': test
    host prices are fake and would pollute the live history."""
    load_dotenv()
    if provider_override is not None and provider_override != "mock":
        raise typer.BadParameter("only 'mock' is supported", param_hint="--provider-override")
    if provider_override is not None and fixtures is None:
        raise typer.BadParameter("--provider-override mock needs --fixtures DIR")

    live = Path(os.environ.get("FARES_DB_PATH", DEFAULT_DB))
    db_path = db or live
    if provider_override and db_path.resolve() == live.resolve() and not allow_live_db:
        typer.echo(
            f"refusing to poll {db_path} with --provider-override: it is the live database "
            "($FARES_DB_PATH) and mock prices there would be read as real. Use a scratch "
            "database (--db) or pass --allow-live-db.",
            err=True,
        )
        raise typer.Exit(1)

    sink = _notifier(notifier)
    try:
        wf = _load(config)
    except (OSError, yaml.YAMLError, ValidationError) as e:
        _poll_failed(sink, f"error: cannot load {config}: {_short(e)}", _fix_for(e))
    if provider_override is not None:
        assert fixtures is not None
        wf = _with_mock_only(wf)
        registry = ProviderRegistry({Provider.MOCK: MockProvider(fixtures)})
    else:
        if (
            db_path.resolve() == live.resolve()
            and not allow_live_db
            and _amadeus_configured()
            and os.environ.get("AMADEUS_HOST") != "production"
        ):
            _poll_failed(
                sink,
                f"refusing to poll {db_path}: it is the live database ($FARES_DB_PATH) and "
                "Amadeus credentials are set but AMADEUS_HOST is not 'production'. Test-host "
                "prices would pollute live history. Set AMADEUS_HOST=production, use a scratch "
                "database (--db), or pass --allow-live-db.",
                "set AMADEUS_HOST=production in .env",
            )
        registry = _registry_from_env()
        if registry.is_empty():
            _poll_failed(
                sink,
                "no providers configured (set SERPAPI_KEY and/or AMADEUS_CLIENT_ID + "
                "AMADEUS_CLIENT_SECRET, or use --provider-override mock --fixtures DIR)",
                "set SERPAPI_KEY in .env (cron reads .env from the checkout)",
            )
    limits = BudgetLimits(
        max_per_run=int(os.environ.get("MAX_CALLS_PER_RUN", DEFAULT_MAX_CALLS_PER_RUN)),
        max_per_day=int(os.environ.get("MAX_DAILY_API_CALLS", DEFAULT_MAX_DAILY_API_CALLS)),
    )

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with Database(db_path) as d:
            run = run_poll(
                d,
                wf,
                registry,
                limits=limits,
                notifier=sink,
                raw_dir=raw_dir,
                trigger=trigger.value,  # type: ignore[arg-type]
                now=utcnow,
            )
            _print_poll_summary(d, run.run_id)
            status = run.status
    except Exception as e:  # run_poll has already marked the run errored, if it got that far
        log.exception("poll failed")
        _poll_failed(sink, f"error: {e!r}"[:300], _fix_for(e))
    if status == "budget_tripped":
        raise typer.Exit(2)


def _short(e: BaseException) -> str:
    """First line of an exception, clipped: pydantic errors run to dozens of lines."""
    return (str(e).splitlines() or [type(e).__name__])[0][:200]


def _print_poll_summary(d: Database, run_id: str) -> None:
    run = d.get_run(run_id)
    assert run is not None
    started = run.started_at.astimezone(UTC).isoformat(timespec="microseconds")

    def scalar(sql: str, *args: object) -> int:
        return d.con.execute(sql, args).fetchone()[0]

    n_obs = scalar("SELECT COUNT(*) FROM observations WHERE run_id=?", run_id)
    n_alerts = scalar("SELECT COUNT(*) FROM alerts WHERE created_at >= ?", started)
    n_sent = scalar(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= ? AND delivered_at IS NOT NULL", started
    )
    typer.echo(f"run: {run.run_id}")
    typer.echo(f"status: {run.status}")
    typer.echo(f"calls: {run.calls_made}")
    typer.echo(f"observations: {n_obs} inserted")
    typer.echo(f"alerts: {n_alerts} inserted, {n_sent} delivered")
    targets = d.con.execute(
        "SELECT DISTINCT target_id FROM api_calls WHERE run_id=? AND status != 'skipped_breaker' "
        "AND target_id IS NOT NULL ORDER BY id",
        (run_id,),
    ).fetchall()
    for (tid,) in targets:
        total = scalar(
            "SELECT COUNT(*) FROM observations WHERE run_id=? AND target_id=?", run_id, tid
        )
        passed = scalar(
            "SELECT COUNT(*) FROM observations WHERE run_id=? AND target_id=? AND risk_passed=1",
            run_id,
            tid,
        )
        cheapest = d.con.execute(
            "SELECT MIN(price_cents) FROM observations "
            "WHERE run_id=? AND target_id=? AND risk_passed=1",
            (run_id, tid),
        ).fetchone()[0]
        kinds = [
            r[0]
            for r in d.con.execute(
                "SELECT kind FROM alerts WHERE target_id=? AND created_at >= ? ORDER BY id",
                (tid, started),
            )
        ]
        typer.echo(
            f"  {tid}  obs={total} passed={passed} "
            f"cheapest={'-' if cheapest is None else fmt_money(cheapest)} "
            f"alerts={','.join(kinds) or '-'}"
        )


@app.command()
def budget(config: ConfigOpt = Path(DEFAULT_CONFIG)) -> None:
    """Compare the config's calls/day with MAX_DAILY_API_CALLS; exit 1 if over."""
    load_dotenv()
    limit = int(os.environ.get("MAX_DAILY_API_CALLS", DEFAULT_MAX_DAILY_API_CALLS))
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


@app.command("alert-test")
def alert_test(
    notifier: Annotated[
        PingNotifierChoice, typer.Option("--notifier", help="ntfy | log.")
    ] = PingNotifierChoice.NTFY,
) -> None:
    """Send one PROVIDER_ERROR-priority ping; exit 0 if delivered, 1 if not. Never touches the DB.

    Run it before installing cron: confirms the phone is subscribed to the topic."""
    sink = _notifier(notifier)
    alert = Alert(
        created_at=utcnow(),
        watchlist_id="fare-tracker",
        kind=AlertKind.PROVIDER_ERROR,
        regime=Regime.MONITOR,
        message="fare tracker: test ping\nIf you can read this, the topic works.",
        alert_key="fare-tracker:alert-test",
    )
    channel = sink.send(alert)
    typer.echo(channel or "undelivered")
    if channel is None:
        raise typer.Exit(1)


@app.command()
def health(
    db: DbOpt = None,
    max_age_hours: Annotated[
        int, typer.Option("--max-age-hours", min=1, help="Alert when the last run is older.")
    ] = DEFAULT_HEALTH_MAX_AGE_HOURS,
    backup_dir: Annotated[
        Path | None,
        typer.Option("--backup-dir", help="Where backup_db.sh writes (default $BACKUP_DIR)."),
    ] = None,
    backup_max_age_hours: Annotated[
        int, typer.Option("--backup-max-age-hours", min=1, help="Alert when backups are older.")
    ] = health_checks.DEFAULT_BACKUP_MAX_AGE_HOURS,
    min_free_mb: Annotated[
        int, typer.Option("--min-free-mb", min=0, help="Alert when the DB disk has less free.")
    ] = health_checks.DEFAULT_MIN_FREE_MB,
    notifier: Annotated[
        PingNotifierChoice, typer.Option("--notifier", help="ntfy | log.")
    ] = PingNotifierChoice.NTFY,
) -> None:
    """Watchdog for silent failures: no run in --max-age-hours, last poll errored or was killed,
    every API call failing, no fares stored, alerts not delivered, stale backups, disk almost
    full. Sends one ntfy message listing every problem with its fix, and exits 1. Stateless:
    reads the DB, writes nothing, stores no alert."""
    path = _db_path(db)
    load_dotenv()
    backups = backup_dir or Path(os.environ.get("BACKUP_DIR") or DEFAULT_BACKUP_DIR)
    now = utcnow()
    problems = health_checks.check(
        path,
        now=now,
        max_age_hours=max_age_hours,
        backup_dir=backups,
        backup_max_age_hours=backup_max_age_hours,
        min_free_mb=min_free_mb,
    )
    if not problems:
        latest = _latest_run_start(path)
        assert latest is not None
        age = (now - latest).total_seconds() / 3600
        typer.echo(f"ok: last run {age:.1f}h ago (limit {max_age_hours}h)")
        return
    for p in problems:
        typer.echo(f"PROBLEM: {p.title} ({p.detail}) -- fix: {p.fix}", err=True)
    body = "\n".join(
        f"{p.title.removeprefix('fare tracker: ')}: {p.detail}\nFix: {p.fix}" for p in problems
    )
    _ping(_notifier(notifier), "health", problems[0].title, body, ACTION_PRIORITY)
    raise typer.Exit(1)


def _latest_run_start(path: Path) -> datetime | None:
    """started_at of the newest non-seed run, or None. Read-only."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT MAX(started_at) FROM runs WHERE \"trigger\" != 'seed'").fetchone()
    finally:
        con.close()
    return None if row[0] is None else datetime.fromisoformat(row[0])


if __name__ == "__main__":
    app()
