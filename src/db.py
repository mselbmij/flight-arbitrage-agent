"""SQLite persistence, migrations and the circuit breaker (ARCHITECTURE.md sections 2, 3, 6).

Stdlib sqlite3 only. The connection runs in autocommit mode and every write goes through
`_tx()` (BEGIN IMMEDIATE ... COMMIT), so the breaker read and the api_calls insert are one
transaction and two overlapping runs cannot both pass.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.models import (
    Alert,
    AlertKind,
    ApiCall,
    ApiCallStatus,
    BenchmarkBand,
    FareBrand,
    FareObservation,
    Itinerary,
    PriceLevel,
    Provider,
    Regime,
    RiskVerdict,
    Run,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_COUNT_TABLES = ("runs", "api_calls", "observations", "benchmark_bands", "alerts")


def utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_per_run: int  # MAX_CALLS_PER_RUN
    max_per_day: int  # MAX_DAILY_API_CALLS


class BudgetTripped(Exception):  # noqa: N818 - name fixed by the phase-2 brief
    """A breaker gate refused a call. `used` is the count/sum before the rejected call."""

    def __init__(self, gate: Literal["run", "day"], used: int, limit: int) -> None:
        super().__init__(f"budget tripped: {gate} gate used={used} limit={limit}")
        self.gate = gate
        self.used = used
        self.limit = limit


# --------------------------------------------------------------------------- #
# (de)serialisation helpers
# --------------------------------------------------------------------------- #


def _aware(dt: datetime) -> datetime:
    """Naive datetimes are a bug at the boundary (they would be silently read as UTC)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt


def _ts(dt: datetime) -> str:
    """Instant -> fixed-width UTC ISO text, so text comparison is time comparison."""
    return _aware(dt).astimezone(UTC).isoformat(timespec="microseconds")


def _ts_or_none(dt: datetime | None) -> str | None:
    return None if dt is None else _ts(dt)


def _dt(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


def _int_or_none(b: bool | None) -> int | None:
    return None if b is None else int(b)


def _bool_or_none(v: int | None) -> bool | None:
    return None if v is None else bool(v)


def _row_to_run(r: sqlite3.Row) -> Run:
    return Run(
        run_id=r["run_id"],
        started_at=_dt(r["started_at"]),
        finished_at=_dt(r["finished_at"]),
        trigger=r["trigger"],
        calls_made=r["calls_made"],
        status=r["status"],
        error=r["error"],
    )


def _row_to_obs(r: sqlite3.Row) -> FareObservation:
    risk = None
    if r["risk_passed"] is not None:
        risk = RiskVerdict(
            passed=bool(r["risk_passed"]),
            rejections=json.loads(r["risk_rejections_json"] or "[]"),
            brand_unverified=bool(r["risk_brand_unverified"]),
            inbound_unverified=bool(r["risk_inbound_unverified"]),
            config_hash=r["risk_config_hash"],
        )
    return FareObservation(
        id=r["id"],
        watchlist_id=r["watchlist_id"],
        target_id=r["target_id"],
        observed_at=_dt(r["observed_at"]),
        provider=Provider(r["provider"]),
        run_id=r["run_id"],
        itinerary=Itinerary.model_validate_json(r["itinerary_json"]),
        price_cents=r["price_cents"],
        currency=r["currency"],
        fare_brand=FareBrand(r["fare_brand"]),
        fare_brand_raw=r["fare_brand_raw"],
        changeable=_bool_or_none(r["changeable"]),
        refundable=_bool_or_none(r["refundable"]),
        carry_on_included=_bool_or_none(r["carry_on_included"]),
        seats_remaining=r["seats_remaining"],
        days_to_departure=r["days_to_departure"],
        booking_url=r["booking_url"],
        raw_payload_ref=r["raw_payload_ref"],
        risk=risk,
    )


def _row_to_band(r: sqlite3.Row) -> BenchmarkBand:
    return BenchmarkBand(
        id=r["id"],
        watchlist_id=r["watchlist_id"],
        target_id=r["target_id"],
        observed_at=_dt(r["observed_at"]),
        provider=Provider(r["provider"]),
        run_id=r["run_id"],
        lowest_price_cents=r["lowest_price_cents"],
        typical_low_cents=r["typical_low_cents"],
        typical_high_cents=r["typical_high_cents"],
        price_level=PriceLevel(r["price_level"]),
        currency=r["currency"],
    )


def _row_to_alert(r: sqlite3.Row) -> Alert:
    return Alert(
        id=r["id"],
        created_at=_dt(r["created_at"]),
        watchlist_id=r["watchlist_id"],
        target_id=r["target_id"],
        kind=AlertKind(r["kind"]),
        regime=Regime(r["regime"]),
        observation_id=r["observation_id"],
        itinerary_key=r["itinerary_key"],
        price_cents=r["price_cents"],
        reference_cents=r["reference_cents"],
        message=r["message"],
        alert_key=r["alert_key"],
        delivered_at=_dt(r["delivered_at"]),
        channel=r["channel"],
    )


_INSERT_API_CALL = (
    "INSERT INTO api_calls (run_id, called_at, provider, endpoint, watchlist_id, target_id, "
    "status, cost_units, latency_ms, error) VALUES (?,?,?,?,?,?,?,?,?,?)"
)


def _api_call_params(call: ApiCall, status: ApiCallStatus) -> tuple:
    return (
        call.run_id,
        _ts(call.called_at),
        call.provider.value,
        call.endpoint,
        call.watchlist_id,
        call.target_id,
        status.value,
        call.cost_units,
        call.latency_ms,
        call.error,
    )


def _statements(script: str) -> Iterator[str]:
    """Split a SQL script into complete statements (trigger bodies stay whole)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf
            buf = ""
    if buf.strip() and not all(
        ln.strip().startswith("--") or not ln.strip() for ln in buf.splitlines()
    ):
        yield buf


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #


class Database:
    def __init__(self, path: str | Path, *, now: Callable[[], datetime] = utcnow) -> None:
        self.path = Path(path)
        self.now = now
        self._con: sqlite3.Connection | None = None

    # -- connection ---------------------------------------------------------- #

    def __enter__(self) -> Database:
        con = sqlite3.connect(self.path, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        # Switching a fresh file to WAL needs an exclusive lock, and SQLite does not always
        # consult the busy handler for that pragma. Two connections opening a new DB at the
        # same moment (first cron run + dashboard) can therefore see "database is locked";
        # retry briefly instead of failing the whole run.
        for attempt in range(20):
            try:
                con.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == 19:
                    con.close()
                    raise
                time.sleep(0.05 * (attempt + 1))
        con.execute("PRAGMA foreign_keys=ON")
        self._con = con
        return self

    def __exit__(self, *exc: object) -> None:
        if self._con is not None:
            self._con.close()  # the closed handle is kept so later use raises ProgrammingError

    def _now(self) -> datetime:
        """The injected clock, validated on every call."""
        return _aware(self.now())

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            raise RuntimeError("Database must be used as a context manager")
        return self._con

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        con = self.con
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")

    # -- migrations ---------------------------------------------------------- #

    def migrate(self) -> list[str]:
        """Apply pending migrations. Per migration: BEGIN IMMEDIATE, read which versions are
        applied, apply the script and record it, COMMIT -- so two processes racing on a fresh
        file serialise on the write lock and the loser finds the version already recorded."""
        con = self.con
        applied: list[str] = []
        for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = script.name.split("_", 1)[0]
            stamp = _ts(self._now())
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations "
                    "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                done = {r[0] for r in con.execute("SELECT version FROM schema_migrations")}
                if version not in done:
                    # executescript() would COMMIT first, so run the statements one by one
                    for stmt in _statements(script.read_text()):
                        con.execute(stmt)
                    con.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, stamp),
                    )
                    applied.append(version)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        return applied

    # -- runs ---------------------------------------------------------------- #

    def start_run(self, run: Run) -> None:
        with self._tx() as con:
            con.execute(
                'INSERT INTO runs (run_id, started_at, finished_at, "trigger", calls_made, '
                "status, error) VALUES (?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    _ts(run.started_at),
                    _ts_or_none(run.finished_at),
                    run.trigger,
                    run.calls_made,
                    run.status,
                    run.error,
                ),
            )

    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE runs SET status=?, error=?, finished_at=? WHERE run_id=?",
                (status, error, _ts(self._now()), run_id),
            )

    def get_run(self, run_id: str) -> Run | None:
        row = self.con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return None if row is None else _row_to_run(row)

    # -- api calls + breaker (section 6) -------------------------------------- #

    def _day_start(self) -> datetime:
        n = self._now().astimezone(UTC)
        return n.replace(hour=0, minute=0, second=0, microsecond=0)

    def _sum_today(self, con: sqlite3.Connection) -> int:
        return con.execute(
            "SELECT COALESCE(SUM(cost_units), 0) FROM api_calls "
            "WHERE called_at >= ? AND status != ?",
            (_ts(self._day_start()), ApiCallStatus.SKIPPED_BREAKER.value),
        ).fetchone()[0]

    def reserve_api_call(self, call: ApiCall, limits: BudgetLimits) -> int:
        tripped: BudgetTripped | None = None
        _ts(call.called_at)  # reject a naive timestamp before anything is written
        with self._tx() as con:
            row = con.execute(
                "SELECT calls_made FROM runs WHERE run_id=?", (call.run_id,)
            ).fetchone()
            calls_made = 0 if row is None else row[0]
            used_today = self._sum_today(con)
            if calls_made + 1 > limits.max_per_run:
                tripped = BudgetTripped("run", calls_made, limits.max_per_run)
            elif used_today + call.cost_units > limits.max_per_day:
                tripped = BudgetTripped("day", used_today, limits.max_per_day)

            if tripped is not None:
                cur = con.execute(
                    _INSERT_API_CALL, _api_call_params(call, ApiCallStatus.SKIPPED_BREAKER)
                )
            else:
                cur = con.execute(_INSERT_API_CALL, _api_call_params(call, ApiCallStatus.OK))
                con.execute(
                    "UPDATE runs SET calls_made = calls_made + 1 WHERE run_id=?", (call.run_id,)
                )
            call_id = cur.lastrowid
        if tripped is not None:
            raise tripped
        assert call_id is not None
        return call_id

    def complete_api_call(
        self,
        call_id: int,
        status: ApiCallStatus,
        latency_ms: int | None,
        error: str | None = None,
    ) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE api_calls SET status=?, latency_ms=?, error=? WHERE id=?",
                (status.value, latency_ms, error, call_id),
            )

    def calls_today(self) -> int:
        return self._sum_today(self.con)

    # -- observations / bands ------------------------------------------------- #

    def insert_observations(self, obs: list[FareObservation]) -> list[int]:
        ids: list[int] = []
        with self._tx() as con:
            for o in obs:
                r = o.risk
                cur = con.execute(
                    "INSERT INTO observations (watchlist_id, target_id, observed_at, provider, "
                    "run_id, itinerary_key, itinerary_json, price_cents, currency, fare_brand, "
                    "fare_brand_raw, changeable, refundable, carry_on_included, seats_remaining, "
                    "days_to_departure, booking_url, raw_payload_ref, risk_passed, "
                    "risk_rejections_json, risk_brand_unverified, risk_inbound_unverified, "
                    "risk_config_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        o.watchlist_id,
                        o.target_id,
                        _ts(o.observed_at),
                        o.provider.value,
                        o.run_id,
                        o.itinerary_key,
                        o.itinerary.model_dump_json(exclude={"itinerary_key"}),
                        o.price_cents,
                        o.currency,
                        o.fare_brand.value,
                        o.fare_brand_raw,
                        _int_or_none(o.changeable),
                        _int_or_none(o.refundable),
                        _int_or_none(o.carry_on_included),
                        o.seats_remaining,
                        o.days_to_departure,
                        o.booking_url,
                        o.raw_payload_ref,
                        None if r is None else int(r.passed),
                        None if r is None else json.dumps(r.rejections),
                        None if r is None else int(r.brand_unverified),
                        None if r is None else int(r.inbound_unverified),
                        None if r is None else r.config_hash,
                    ),
                )
                assert cur.lastrowid is not None
                ids.append(cur.lastrowid)
        return ids

    def insert_band(self, band: BenchmarkBand) -> int:
        with self._tx() as con:
            cur = con.execute(
                "INSERT INTO benchmark_bands (watchlist_id, target_id, observed_at, provider, "
                "run_id, lowest_price_cents, typical_low_cents, typical_high_cents, "
                "price_level, currency) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    band.watchlist_id,
                    band.target_id,
                    _ts(band.observed_at),
                    band.provider.value,
                    band.run_id,
                    band.lowest_price_cents,
                    band.typical_low_cents,
                    band.typical_high_cents,
                    band.price_level.value,
                    band.currency,
                ),
            )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def observations_for(
        self, target_id: str, *, since: datetime | None = None, passed_only: bool = False
    ) -> list[FareObservation]:
        sql = "SELECT * FROM observations WHERE target_id=?"
        args: list[object] = [target_id]
        if since is not None:
            sql += " AND observed_at >= ?"
            args.append(_ts(since))
        if passed_only:
            sql += " AND risk_passed = 1"
        sql += " ORDER BY observed_at, id"
        return [_row_to_obs(r) for r in self.con.execute(sql, args)]

    def lowest_seen(self, target_id: str) -> int | None:
        # an unverified brand (FareBrand.UNKNOWN) passes the risk rules but is never a strike
        # reference: it might be Basic Economy
        return self.con.execute(
            "SELECT MIN(price_cents) FROM observations WHERE target_id=? AND risk_passed = 1 "
            "AND COALESCE(risk_brand_unverified, 0) = 0",
            (target_id,),
        ).fetchone()[0]

    def latest_band(self, target_id: str) -> BenchmarkBand | None:
        row = self.con.execute(
            "SELECT * FROM benchmark_bands WHERE target_id=? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
        return None if row is None else _row_to_band(row)

    def last_polled(self, target_ids: list[str]) -> dict[str, datetime | None]:
        out: dict[str, datetime | None] = dict.fromkeys(target_ids)
        if not target_ids:
            return out
        marks = ",".join("?" * len(target_ids))
        rows = self.con.execute(
            f"SELECT target_id, MAX(called_at) FROM api_calls "
            f"WHERE status != ? AND target_id IN ({marks}) GROUP BY target_id",
            [ApiCallStatus.SKIPPED_BREAKER.value, *target_ids],
        )
        for tid, called_at in rows:
            out[tid] = _dt(called_at)
        return out

    def runs_today_for_watchlist(self, watchlist_id: str) -> int:
        """Distinct runs since 00:00 UTC (by this database's clock) that paid for a call
        (`ok` or `error`) for `watchlist_id`. Breaker-skipped rows are not polls."""
        return self.con.execute(
            "SELECT COUNT(DISTINCT run_id) FROM api_calls "
            "WHERE watchlist_id = ? AND called_at >= ? AND status != ?",
            (watchlist_id, _ts(self._day_start()), ApiCallStatus.SKIPPED_BREAKER.value),
        ).fetchone()[0]

    # -- alerts ---------------------------------------------------------------- #

    def insert_alert(self, alert: Alert, *, dedup_window_hours: int = 24) -> int | None:
        created = _ts(alert.created_at)
        window_start = _ts(alert.created_at - timedelta(hours=dedup_window_hours))
        if alert.delivered_at is not None:
            _ts(alert.delivered_at)
        with self._tx() as con:
            dup = con.execute(
                "SELECT 1 FROM alerts WHERE alert_key=? AND created_at > ? LIMIT 1",
                (alert.alert_key, window_start),
            ).fetchone()
            if dup is not None:
                return None
            cur = con.execute(
                "INSERT INTO alerts (created_at, watchlist_id, target_id, kind, regime, "
                "observation_id, itinerary_key, price_cents, reference_cents, message, "
                "alert_key, delivered_at, channel) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    created,
                    alert.watchlist_id,
                    alert.target_id,
                    alert.kind.value,
                    alert.regime.value,
                    alert.observation_id,
                    alert.itinerary_key,
                    alert.price_cents,
                    alert.reference_cents,
                    alert.message,
                    alert.alert_key,
                    _ts_or_none(alert.delivered_at),
                    alert.channel,
                ),
            )
            return cur.lastrowid

    def alert_kinds_for(self, target_id: str) -> set[AlertKind]:
        """Distinct kinds ever inserted for this target, of any age. Watchlist-level alerts
        (target_id NULL) are not included. Gates FENCE_WARNING and DOT_24H (once per target)."""
        rows = self.con.execute("SELECT DISTINCT kind FROM alerts WHERE target_id=?", (target_id,))
        return {AlertKind(r[0]) for r in rows}

    def mark_delivered(self, alert_id: int, channel: str) -> None:
        with self._tx() as con:
            con.execute(
                "UPDATE alerts SET delivered_at=?, channel=? WHERE id=?",
                (_ts(self._now()), channel, alert_id),
            )

    def undelivered_alerts(self, *, max_age_hours: int) -> list[Alert]:
        oldest = _ts(self._now() - timedelta(hours=max_age_hours))
        rows = self.con.execute(
            "SELECT * FROM alerts WHERE delivered_at IS NULL AND created_at > ? "
            "ORDER BY created_at, id",
            (oldest,),
        )
        return [_row_to_alert(r) for r in rows]

    # -- dashboard / cli helpers ------------------------------------------------ #

    def observation(self, obs_id: int) -> FareObservation | None:
        row = self.con.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        return None if row is None else _row_to_obs(row)

    def delete_runs_by_trigger(self, trigger: str) -> int:
        """Remove runs with this trigger and every row that references them
        (api_calls, observations, benchmark_bands, alerts). Returns runs deleted.
        Used by `fares seed --replace`."""
        with self._tx() as con:
            ids = [
                r[0] for r in con.execute('SELECT run_id FROM runs WHERE "trigger"=?', (trigger,))
            ]
            if not ids:
                return 0
            q = ",".join("?" * len(ids))
            obs_ids = [
                r[0]
                for r in con.execute(f"SELECT id FROM observations WHERE run_id IN ({q})", ids)  # noqa: S608
            ]
            if obs_ids:
                oq = ",".join("?" * len(obs_ids))
                con.execute(f"DELETE FROM alerts WHERE observation_id IN ({oq})", obs_ids)  # noqa: S608
            for table in ("api_calls", "observations", "benchmark_bands"):
                con.execute(f"DELETE FROM {table} WHERE run_id IN ({q})", ids)  # noqa: S608
            con.execute(f"DELETE FROM runs WHERE run_id IN ({q})", ids)  # noqa: S608
            return len(ids)

    def counts(self) -> dict[str, int]:
        return {
            t: self.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in _COUNT_TABLES
        }
