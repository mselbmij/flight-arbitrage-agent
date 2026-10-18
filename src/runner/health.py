"""`fares health` checks (ARCHITECTURE.md section 7a): read-only, stateless, one pass.

Each check looks for one way the tracker can fail silently and returns a `Problem` that names it
and says how to fix it. Nothing here writes to the database or creates files; a missing or
unmigrated database reads as "no runs".
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

#: A run still `running` after this long was killed (OOM, reboot, kill -9) before finish_run.
STUCK_RUN_HOURS = 2
#: Nightly backup at 03:00 + one missed night, checked at 09:00 / 21:00.
DEFAULT_BACKUP_MAX_AGE_HOURS = 30
DEFAULT_MIN_FREE_MB = 200
_MAX_DETAIL = 160
_MAX_TITLES = 3


@dataclass(frozen=True)
class Problem:
    title: str  # goes in the ntfy Title, first problem only
    detail: str  # what was seen
    fix: str  # what to do about it


def _clip(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("SELECT 1 FROM runs LIMIT 1")
    except sqlite3.OperationalError:  # no runs table yet
        con.close()
        return None
    return con


def check(
    path: Path,
    *,
    now: datetime,
    max_age_hours: int,
    backup_dir: Path,
    backup_max_age_hours: int = DEFAULT_BACKUP_MAX_AGE_HOURS,
    min_free_mb: int = DEFAULT_MIN_FREE_MB,
) -> list[Problem]:
    """Every problem found, most urgent first. Empty means healthy."""
    con = _connect(path)
    if con is None:
        return [
            Problem(
                f"fare tracker: no run in {max_age_hours}h",
                f"no runs recorded in {path}",
                "check the cron lines are installed (crontab -l) and ops/poll.log",
            )
        ]
    try:
        problems = _runs(con, now, max_age_hours)
        problems += _calls(con, now, max_age_hours)
        problems += _undelivered(con, now, max_age_hours)
        first_run = con.execute(
            "SELECT MIN(started_at) FROM runs WHERE \"trigger\" != 'seed'"
        ).fetchone()[0]
    finally:
        con.close()
    if first_run is not None:
        problems += _backups(
            backup_dir, now, backup_max_age_hours, datetime.fromisoformat(first_run)
        )
    problems += _disk(path, min_free_mb)
    return problems


def _runs(con: sqlite3.Connection, now: datetime, max_age_hours: int) -> list[Problem]:
    real = "\"trigger\" != 'seed'"
    latest = con.execute(
        f"SELECT * FROM runs WHERE {real} ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    if latest is None:
        return [
            Problem(
                f"fare tracker: no run in {max_age_hours}h",
                "no runs recorded",
                "check the cron lines are installed (crontab -l) and ops/poll.log",
            )
        ]
    problems: list[Problem] = []
    started = datetime.fromisoformat(latest["started_at"])
    if now - started > timedelta(hours=max_age_hours):
        problems.append(
            Problem(
                f"fare tracker: no run in {max_age_hours}h",
                f"last run started {started:%Y-%m-%d %H:%M} UTC",
                "host down, cron removed or `fares poll` failing before it starts: "
                "check crontab -l and ops/poll.log",
            )
        )
    if latest["status"] == "error":
        problems.append(
            Problem(
                "fare tracker: last poll failed",
                _clip(latest["error"]) or "no error recorded",
                "see ops/poll.log for the traceback; run `fares poll` by hand to reproduce",
            )
        )
    stuck = con.execute(
        f"SELECT COUNT(*) FROM runs WHERE {real} AND status = 'running' AND started_at < ?",
        (_ts(now - timedelta(hours=STUCK_RUN_HOURS)),),
    ).fetchone()[0]
    if stuck:
        problems.append(
            Problem(
                "fare tracker: poll killed mid-run",
                f"{stuck} run(s) still 'running' after {STUCK_RUN_HOURS}h",
                "the process died before finishing (reboot, out of memory, kill): "
                "check ops/poll.log and the host's uptime / dmesg",
            )
        )
    return problems


def _calls(con: sqlite3.Connection, now: datetime, max_age_hours: int) -> list[Problem]:
    """Runs happen but no fares come back: every provider call failing, or empty results."""
    since = _ts(now - timedelta(hours=max_age_hours))
    rows = con.execute(
        "SELECT a.status, COUNT(*) AS n FROM api_calls a JOIN runs r USING (run_id) "
        "WHERE r.\"trigger\" != 'seed' AND a.called_at >= ? GROUP BY a.status",
        (since,),
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    ok, errors = by_status.get("ok", 0), by_status.get("error", 0)
    if errors and not ok:
        last = con.execute(
            "SELECT provider, error FROM api_calls WHERE status = 'error' AND called_at >= ? "
            "ORDER BY called_at DESC LIMIT 1",
            (since,),
        ).fetchone()
        return [
            Problem(
                f"fare tracker: every API call failed in {max_age_hours}h",
                f"{errors} failed, latest {last['provider']}: {_clip(last['error'])}",
                "HTTP 401/403: fix the key in .env; 429 or 'run out of searches': quota spent, "
                "wait for the reset or upgrade the plan; anything else: see ops/poll.log",
            )
        ]
    if ok:
        n_obs = con.execute(
            "SELECT COUNT(*) FROM observations o JOIN runs r USING (run_id) "
            "WHERE r.\"trigger\" != 'seed' AND o.observed_at >= ?",
            (since,),
        ).fetchone()[0]
        if n_obs == 0:
            return [
                Problem(
                    f"fare tracker: no fares stored in {max_age_hours}h",
                    f"{ok} API call(s) succeeded but returned no itineraries",
                    "constraints too strict or a route/date with no service: check "
                    "config/watchlists.yaml and the newest file under data/raw/",
                )
            ]
    return []


def _undelivered(con: sqlite3.Connection, now: datetime, max_age_hours: int) -> list[Problem]:
    rows = con.execute(
        "SELECT message FROM alerts WHERE delivered_at IS NULL AND created_at >= ? "
        "ORDER BY created_at DESC",
        (_ts(now - timedelta(hours=max_age_hours)),),
    ).fetchall()
    if not rows:
        return []
    titles = [r["message"].split("\n", 1)[0] for r in rows[:_MAX_TITLES]]
    more = f" (+{len(rows) - _MAX_TITLES} more)" if len(rows) > _MAX_TITLES else ""
    return [
        Problem(
            f"fare tracker: {len(rows)} alert(s) not delivered",
            "; ".join(titles) + more,
            "NTFY_TOPIC unset or ntfy unreachable when they fired (retried for 24h only): "
            "check .env and ops/poll.log, then look at the alerts table",
        )
    ]


def _backups(
    backup_dir: Path, now: datetime, max_age_hours: int, first_run: datetime
) -> list[Problem]:
    if now - first_run <= timedelta(hours=max_age_hours):
        return []  # too early for a nightly backup to be owed
    newest = max(backup_dir.glob("fares-*.db"), key=lambda p: p.stat().st_mtime, default=None)
    if newest is not None:
        age = now - datetime.fromtimestamp(newest.stat().st_mtime, tz=now.tzinfo)
        if age <= timedelta(hours=max_age_hours):
            return []
        detail = f"newest backup {newest.name} is {age.total_seconds() / 3600:.0f}h old"
    else:
        detail = f"no backups in {backup_dir}"
    return [
        Problem(
            "fare tracker: database backup is stale",
            detail,
            "run scripts/backup_db.sh by hand and read ops/backup.log (disk full? DB path?)",
        )
    ]


def _disk(path: Path, min_free_mb: int) -> list[Problem]:
    try:
        free_mb = shutil.disk_usage(path.parent if path.parent.exists() else Path(".")).free
    except OSError:
        return []
    free_mb //= 1024 * 1024  # bytes -> MB
    if free_mb >= min_free_mb:
        return []
    return [
        Problem(
            "fare tracker: disk almost full",
            f"{free_mb} MB free next to {path}",
            "polls and backups will start failing: prune data/raw/ and data/backups/",
        )
    ]
