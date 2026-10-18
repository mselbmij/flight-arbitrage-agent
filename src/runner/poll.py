"""ARCHITECTURE.md sections 6, 7, 7a: one `Run` over every pollable target.

`rules.py` is pure; everything with a side effect happens here: the DB, the providers, raw
payload files and the notifier hook. Per target the order is fixed: read `lowest_seen` (before
any of this run's rows exist), call every provider of the watchlist, store what came back, then
evaluate ONCE over all of the target's rows.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from src.db import BudgetLimits, BudgetTripped, Database
from src.engine import risk_filters, rules
from src.ingestion.base import FlightProvider, ProviderError
from src.models import (
    Alert,
    AlertKind,
    ApiCall,
    ApiCallStatus,
    BenchmarkBand,
    FareObservation,
    Provider,
    Regime,
    ResolvedWatchlist,
    Run,
    SearchTarget,
    WatchlistFile,
    WatchStatus,
)

log = logging.getLogger(__name__)

DEDUP_WINDOW_HOURS = 24
_EPOCH = datetime.min.replace(tzinfo=UTC)
_MAX_ERROR_CHARS = 200


class Notifier(Protocol):
    def send(self, alert: Alert) -> str | None:
        """Deliver the alert. Returns the channel name on success, None on failure or no-op."""
        ...


class NullNotifier:
    """Phase 5 default: logs the title and reports no channel, so the alert stays undelivered
    and the Phase 6 notifier can retry it."""

    def send(self, alert: Alert) -> str | None:
        log.info("alert (not delivered): %s", alert.message.split("\n", 1)[0])
        return None


class ProviderRegistry:
    """Provider name -> client. Built from the environment by the CLI; injectable in tests."""

    def __init__(self, providers: Mapping[Provider, FlightProvider]) -> None:
        self._providers = dict(providers)

    def is_empty(self) -> bool:
        return not self._providers

    def get(self, name: Provider) -> FlightProvider | None:
        return self._providers.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._providers


# --------------------------------------------------------------------------- #
# target selection (section 7)
# --------------------------------------------------------------------------- #


def select_targets(
    wl: ResolvedWatchlist, last_polled: dict[str, datetime | None], *, now: datetime
) -> list[SearchTarget]:
    """Which targets of `wl` this run may spend budget on."""
    if wl.status is WatchStatus.PURCHASED and wl.purchase is not None:
        # pinned to the purchased target every run; once it has departed there is nothing to watch
        return [
            t for t in wl.targets if t.target_id == wl.purchase.target_id and t.depart >= now.date()
        ]
    if wl.status is not WatchStatus.WATCHING:
        return []
    if wl.poll.scan_provider is not None:
        log.warning(
            "%s: scan_provider=%s ignored, scan tier not implemented",
            wl.id,
            wl.poll.scan_provider.value,
        )
    live = [t for t in wl.targets if t.regime(now) is not Regime.EXPIRED]

    def order(t: SearchTarget) -> tuple[bool, datetime, int]:
        polled = last_polled.get(t.target_id)
        return (polled is not None, polled or _EPOCH, t.days_to_departure(now))

    return sorted(live, key=order)[: wl.poll.targets_per_run]


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


def run_poll(
    db: Database,
    wf: WatchlistFile,
    providers: ProviderRegistry,
    *,
    limits: BudgetLimits,
    notifier: Notifier,
    raw_dir: Path,
    trigger: Literal["cron", "manual"],
    now: Callable[[], datetime],
    run_id: str | None = None,
) -> Run:
    run_id = run_id or uuid4().hex
    db.migrate()
    db.start_run(Run(run_id=run_id, started_at=now(), trigger=trigger))
    poller = _Poller(db, providers, limits, notifier, raw_dir, now, run_id, wf.config_hash())
    try:
        status = poller.run(wf)
    except Exception as e:
        db.finish_run(run_id, "error", repr(e))
        raise
    db.finish_run(run_id, status)
    run = db.get_run(run_id)
    assert run is not None
    return run


class _Poller:
    def __init__(
        self,
        db: Database,
        providers: ProviderRegistry,
        limits: BudgetLimits,
        notifier: Notifier,
        raw_dir: Path,
        now: Callable[[], datetime],
        run_id: str,
        config_hash: str,
    ) -> None:
        self.db, self.providers, self.limits, self.notifier = db, providers, limits, notifier
        self.raw_dir, self.now, self.run_id, self.config_hash = raw_dir, now, run_id, config_hash

    def run(self, wf: WatchlistFile) -> Literal["ok", "budget_tripped"]:
        # purchased watchlists first: post-purchase tracking (credit drops, the DOT window) is
        # time-limited and must not be starved by a per-run limit spent on watching grids
        resolved = sorted(wf.resolved(), key=lambda w: w.status is not WatchStatus.PURCHASED)
        for wl in resolved:
            if wl.status not in (WatchStatus.WATCHING, WatchStatus.PURCHASED):
                continue
            done = self.db.runs_today_for_watchlist(wl.id)
            if done >= wl.poll.times_per_day:
                log.info(
                    "%s: already polled in %d run(s) today (times_per_day=%d), skipping",
                    wl.id,
                    done,
                    wl.poll.times_per_day,
                )
                continue
            last = self.db.last_polled([t.target_id for t in wl.targets])
            for target in select_targets(wl, last, now=self.now()):
                if not self._poll_target(wl, target):
                    return "budget_tripped"
        return "ok"

    # -- one target: every provider, then one evaluation --------------------------------- #

    def _poll_target(self, wl: ResolvedWatchlist, target: SearchTarget) -> bool:
        """False when the breaker tripped (the caller stops the whole run)."""
        db = self.db
        lowest_before = db.lowest_seen(target.target_id)  # before this run's rows exist
        collected: list[FareObservation] = []
        band: BenchmarkBand | None = None
        first_call_at: datetime | None = None

        for provider_name in wl.poll.providers:
            provider = self.providers.get(provider_name)
            if provider is None:
                log.warning(
                    "%s: provider %s is not configured, skipping %s",
                    wl.id,
                    provider_name.value,
                    target.target_id,
                )
                continue

            call = ApiCall(
                run_id=self.run_id,
                called_at=self.now(),
                provider=provider_name,
                endpoint=f"search:{target.origin}{target.destination}",
                watchlist_id=wl.id,
                target_id=target.target_id,
                status=ApiCallStatus.OK,
                cost_units=1,
            )
            try:
                call_id = db.reserve_api_call(call, self.limits)
            except BudgetTripped as e:
                # rows already paid for in this target must still be judged: a BUY must not
                # be lost because the NEXT provider call tripped the breaker
                if collected and first_call_at is not None:
                    self._evaluate(wl, target, collected, band, lowest_before, first_call_at)
                self._budget_tripped(wl, target, e)
                return False

            t0 = self.now()
            first_call_at = first_call_at or t0
            started = time.perf_counter()
            try:
                result = provider.search(target, wl, self.run_id, observed_at=t0)
            except ProviderError as e:
                db.complete_api_call(call_id, ApiCallStatus.ERROR, _ms(started), error=str(e))
                self._provider_error(wl, target, provider_name, e)
                continue
            except Exception as e:
                db.complete_api_call(call_id, ApiCallStatus.ERROR, _ms(started), error=repr(e))
                raise
            db.complete_api_call(call_id, ApiCallStatus.OK, _ms(started))

            ref = self._write_raw(provider_name, target, result.raw)
            stamped = [o.model_copy(update={"raw_payload_ref": ref}) for o in result.observations]
            verdicted = risk_filters.apply(stamped, wl, config_hash=self.config_hash)
            ids = db.insert_observations(verdicted)
            collected += [
                o.model_copy(update={"id": i}) for o, i in zip(verdicted, ids, strict=True)
            ]
            if result.band is not None:
                db.insert_band(result.band)
                band = result.band

        if first_call_at is not None:
            self._evaluate(wl, target, collected, band, lowest_before, first_call_at)
        return True

    def _evaluate(
        self,
        wl: ResolvedWatchlist,
        target: SearchTarget,
        observations: list[FareObservation],
        band: BenchmarkBand | None,
        lowest_before: int | None,
        at: datetime,
    ) -> None:
        state = rules.TargetState(
            band=band if band is not None else self.db.latest_band(target.target_id),
            lowest_seen_cents=lowest_before,
            already_alerted=frozenset(self.db.alert_kinds_for(target.target_id)),
        )
        alerts = rules.evaluate(
            wl,
            target,
            observations,
            state,
            now=at,
            run_id=self.run_id,
            config_hash=self.config_hash,
        )
        for alert in alerts:
            self._store_and_notify(alert)

    # -- side effects ---------------------------------------------------------------------- #

    def _write_raw(self, provider: Provider, target: SearchTarget, raw: dict) -> str:
        rel = Path(self.run_id) / f"{provider.value}-{target.target_id.replace(':', '_')}.json"
        path = self.raw_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            json.dump(raw, f)
        return str(rel)

    def _store_and_notify(self, alert: Alert) -> None:
        alert_id = self.db.insert_alert(alert, dedup_window_hours=DEDUP_WINDOW_HOURS)
        if alert_id is None:
            return  # same alert_key inside the window
        channel = self.notifier.send(alert.model_copy(update={"id": alert_id}))
        if channel:
            self.db.mark_delivered(alert_id, channel)

    def _budget_tripped(
        self, wl: ResolvedWatchlist, target: SearchTarget, e: BudgetTripped
    ) -> None:
        now = self.now()
        self._store_and_notify(
            Alert(
                created_at=now,
                watchlist_id=wl.id,
                target_id=None,
                kind=AlertKind.BUDGET_TRIPPED,
                regime=wl.regime_for(target, now),
                message=f"API budget tripped ({e.used}/{e.limit} {e.gate})",
                alert_key=f"{wl.id}:budget_tripped:-:0",
            )
        )

    def _provider_error(
        self, wl: ResolvedWatchlist, target: SearchTarget, provider: Provider, e: ProviderError
    ) -> None:
        now = self.now()
        detail = str(e)[:_MAX_ERROR_CHARS]
        self._store_and_notify(
            Alert(
                created_at=now,
                watchlist_id=wl.id,
                target_id=None,
                kind=AlertKind.PROVIDER_ERROR,
                regime=wl.regime_for(target, now),
                message=f"{provider.value} failed for {wl.id}\n{target.target_id}: {detail}",
                alert_key=f"{wl.id}:provider_error:{target.target_id}:{provider.value}",
            )
        )


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
