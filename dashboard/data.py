"""Dashboard data layer (ARCHITECTURE.md section 8): DB + watchlist YAML in, DataFrames out.

Pure: no Streamlit, no formatting. Money columns are integer cents (pandas ``int64``, or the
nullable ``Int64`` where a value can be missing); formatting to dollars happens in app.py.
``now`` is always a parameter so callers (and tests) control the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict

from src.db import BudgetLimits, Database
from src.models import (
    BenchmarkBand,
    FareObservation,
    ResolvedWatchlist,
    SearchTarget,
    WatchlistFile,
)


class DashboardSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    db_path: Path
    config_path: Path


@dataclass
class Loaded:
    wf: WatchlistFile
    resolved: list[ResolvedWatchlist]
    db: Database

    def close(self) -> None:
        self.db.__exit__(None, None, None)


def load(source: DashboardSource) -> Loaded:
    """Open the DB (applying pending migrations, so a fresh file renders as empty) and parse the
    YAML. Raises FileNotFoundError for a missing path rather than silently creating a database."""
    if not source.config_path.is_file():
        raise FileNotFoundError(f"watchlist config not found: {source.config_path}")
    if not source.db_path.is_file():
        raise FileNotFoundError(f"database not found: {source.db_path}")
    wf = WatchlistFile.model_validate(yaml.safe_load(source.config_path.read_text()))
    db = Database(source.db_path).__enter__()
    try:
        db.migrate()
    except BaseException:
        db.__exit__(None, None, None)
        raise
    return Loaded(wf=wf, resolved=wf.resolved(), db=db)


# --------------------------------------------------------------------------- #
# frame construction
# --------------------------------------------------------------------------- #

_TABLE_SCHEMA = {
    "watchlist_id": "str",
    "target_id": "str",
    "origin": "str",
    "destination": "str",
    "depart": "object",
    "return_": "object",
    "regime": "str",
    "dtd": "int64",
    "cheapest_passed_cents": "Int64",
    "strike_reference_cents": "Int64",
    "strike_multiplier": "float64",
    "strike_reference_name": "str",
    "in_strike_zone": "boolean",
    "last_polled": "datetime64[us, UTC]",
    "n_obs": "int64",
    "n_rejected": "int64",
}
_SERIES_SCHEMA = {
    "observed_at": "datetime64[us, UTC]",
    "dtd": "int64",
    "price_cents": "int64",
    "fare_brand": "str",
    "stops": "int64",
    "provider": "str",
    "risk_passed": "bool",
    "risk_rejections": "str",
    "brand_unverified": "bool",
    "itinerary_key": "str",
}
_BAND_SCHEMA = {
    "observed_at": "datetime64[us, UTC]",
    "typical_low_cents": "Int64",
    "typical_high_cents": "Int64",
    "lowest_price_cents": "Int64",
    "price_level": "str",
}
_ALERT_SCHEMA = {
    "created_at": "datetime64[us, UTC]",
    "watchlist_id": "str",
    "target_id": "str",
    "kind": "str",
    "regime": "str",
    "price_cents": "Int64",
    "reference_cents": "Int64",
    "message": "str",
    "delivered": "bool",
}


def _frame(rows: list[dict], schema: dict[str, str]) -> pd.DataFrame:
    """Build a frame with every column present and an explicit dtype, empty or not."""
    df = pd.DataFrame(rows, columns=list(schema))
    for col, dtype in schema.items():
        if dtype.startswith("datetime64"):
            df[col] = pd.to_datetime(df[col], utc=True).astype(dtype)
        elif dtype != "object":
            df[col] = df[col].astype(dtype)
    return df


# --------------------------------------------------------------------------- #
# strike reference (ARCHITECTURE.md section 3)
# --------------------------------------------------------------------------- #


def strike_reference(
    wl: ResolvedWatchlist,
    target: SearchTarget,
    band: BenchmarkBand | None,
    lowest_seen: int | None,
    *,
    now: datetime | None = None,
) -> tuple[int | None, float, str]:
    """(reference_cents, multiplier, reference_name) for the target's regime at ``now``.

    POST_PURCHASE and EXPIRED have no strike rule: (None, 0, "-"). A band-based reference with no
    usable band, or ``lowest_seen`` with no passed fare yet, gives (None, multiplier, name)."""
    rule = wl.strike_rule(wl.regime_for(target, now))
    if rule is None:
        return None, 0.0, "-"
    reference: int | None
    if rule.reference == "ceiling":
        reference = wl.ceiling_cents
    elif rule.reference == "lowest_seen":
        reference = lowest_seen
    elif band is None:
        reference = None
    elif rule.reference == "typical_low":
        reference = band.typical_low_cents
    else:
        reference = band.typical_high_cents
    return reference, rule.multiplier, rule.reference


# --------------------------------------------------------------------------- #
# frames
# --------------------------------------------------------------------------- #


def target_table(loaded: Loaded, now: datetime) -> pd.DataFrame:
    """One row per target of every watchlist. ``cheapest_passed_cents`` is ``lowest_seen``;
    ``in_strike_zone`` compares it with ``reference * multiplier`` (None when either is missing)."""
    db = loaded.db
    ids = [t.target_id for w in loaded.resolved for t in w.targets]
    polled = db.last_polled(ids)
    counts = {
        r["target_id"]: (r["n"], r["rejected"])
        for r in db.con.execute(
            "SELECT target_id, COUNT(*) AS n, COALESCE(SUM(risk_passed = 0), 0) AS rejected "
            "FROM observations GROUP BY target_id"
        )
    }
    rows = []
    for wl in loaded.resolved:
        for t in wl.targets:
            tid = t.target_id
            lowest = db.lowest_seen(tid)
            ref, mult, name = strike_reference(wl, t, db.latest_band(tid), lowest, now=now)
            n_obs, n_rejected = counts.get(tid, (0, 0))
            rows.append(
                {
                    "watchlist_id": wl.id,
                    "target_id": tid,
                    "origin": t.origin,
                    "destination": t.destination,
                    "depart": t.depart,
                    "return_": t.return_,
                    "regime": wl.regime_for(t, now).value,
                    "dtd": t.days_to_departure(now),
                    "cheapest_passed_cents": lowest,
                    "strike_reference_cents": ref,
                    "strike_multiplier": mult,
                    "strike_reference_name": name,
                    "in_strike_zone": (
                        None if ref is None or lowest is None else lowest <= ref * mult
                    ),
                    "last_polled": polled[tid],
                    "n_obs": n_obs,
                    "n_rejected": n_rejected,
                }
            )
    return _frame(rows, _TABLE_SCHEMA)


def _series_row(o: FareObservation) -> dict:
    risk = o.risk
    return {
        "observed_at": o.observed_at,
        "dtd": o.days_to_departure,
        "price_cents": o.price_cents,
        "fare_brand": o.fare_brand.value,
        "stops": max(sl.stops for sl in o.itinerary.slices),
        "provider": o.provider.value,
        "risk_passed": bool(risk and risk.passed),  # no verdict = not verified as passed
        "risk_rejections": ", ".join(risk.rejections) if risk else "",
        "brand_unverified": bool(risk and risk.brand_unverified),
        "itinerary_key": o.itinerary_key,
    }


def price_series(loaded: Loaded, target_id: str) -> pd.DataFrame:
    """One row per observation of the target, oldest first."""
    rows = [_series_row(o) for o in loaded.db.observations_for(target_id)]
    return _frame(rows, _SERIES_SCHEMA).sort_values("observed_at", kind="stable", ignore_index=True)


def band_series(loaded: Loaded, target_id: str) -> pd.DataFrame:
    """One row per stored benchmark band of the target, oldest first."""
    cur = loaded.db.con.execute(
        "SELECT observed_at, typical_low_cents, typical_high_cents, lowest_price_cents, "
        "price_level FROM benchmark_bands WHERE target_id=? ORDER BY observed_at, id",
        (target_id,),
    )
    return _frame([dict(r) for r in cur], _BAND_SCHEMA)


def budget_today(loaded: Loaded, limits: BudgetLimits, now: datetime) -> dict[str, int]:
    """Calls used today (the breaker's own definition of "today", evaluated at ``now``)."""
    db = loaded.db
    previous, db.now = db.now, lambda: now
    try:
        used = db.calls_today()
    finally:
        db.now = previous
    return {
        "used": used,
        "limit": limits.max_per_day,
        "calls_per_day": loaded.wf.calls_per_day(),
    }


def alerts_recent(loaded: Loaded, *, limit: int = 20) -> pd.DataFrame:
    """Newest alerts first, all watchlists."""
    cur = loaded.db.con.execute(
        "SELECT created_at, watchlist_id, target_id, kind, regime, price_cents, reference_cents, "
        "message, delivered_at IS NOT NULL AS delivered FROM alerts "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return _frame([dict(r) for r in cur], _ALERT_SCHEMA)


def last_run_at(loaded: Loaded) -> datetime | None:
    """Start time of the most recent run (any trigger), or None before the first run."""
    value = loaded.db.con.execute("SELECT MAX(started_at) FROM runs").fetchone()[0]
    return None if value is None else datetime.fromisoformat(value)
