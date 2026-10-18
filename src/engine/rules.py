"""ARCHITECTURE.md sections 3 and 7a: regimes, strikes, post-purchase rules, alert text.

Pure functions: no DB, no clock reads, no logging. The runner reads `TargetState` from the
database, passes THIS run's risk-verdicted observations and an explicit `now`, and persists
whatever comes back. Money is integer cents end to end; the strike compare is exact `Decimal`
arithmetic on the multiplier's decimal text (`Decimal(str(mult))`), never `int(float)`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from src.models import (
    Alert,
    AlertKind,
    BenchmarkBand,
    FareBrand,
    FareObservation,
    Regime,
    ResolvedWatchlist,
    SearchTarget,
    Slice,
)

MAX_MESSAGE_CHARS = 500
_PRICE_DRIVEN_REGIMES = (Regime.MONITOR, Regime.PRIME, Regime.FENCE, Regime.LATE)


class TargetState(BaseModel):
    """What the runner reads from the DB for one target before evaluating it."""

    model_config = ConfigDict(frozen=True)

    band: BenchmarkBand | None
    lowest_seen_cents: int | None  # BEFORE this run's rows were inserted
    already_alerted: frozenset[AlertKind]  # kinds ever inserted for this target


# --------------------------------------------------------------------------- #
# formatting helpers (section 7a)
# --------------------------------------------------------------------------- #


def fmt_money(cents: int) -> str:
    dollars, rem = divmod(cents, 100)
    return f"${dollars}" if rem == 0 else f"${dollars}.{rem:02d}"


def fmt_route(target: SearchTarget) -> str:
    return f"{target.origin}→{target.destination}"


def _fmt_day(d) -> str:
    return f"{d:%b} {d.day}"


def _fmt_dates(target: SearchTarget) -> str:
    if target.return_ is None:
        return _fmt_day(target.depart)
    return f"{_fmt_day(target.depart)} – {_fmt_day(target.return_)}"


def _fmt_clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'a' if dt.hour < 12 else 'p'}"


def _fmt_stops(sl: Slice) -> str:
    if sl.stops == 0:
        return "nonstop"
    via = ", ".join(seg.destination for seg in sl.segments[:-1])
    return f"{sl.stops} stop{'s' if sl.stops > 1 else ''} via {via}"


def fmt_itin(sl: Slice) -> str:
    flights = " + ".join(f"{seg.carrier} {seg.flight_number}" for seg in sl.segments)
    return f"{flights} nonstop" if sl.stops == 0 else f"{flights}, {_fmt_stops(sl)}"


def _fmt_brand(obs: FareObservation) -> str:
    if obs.fare_brand.value == "unknown":
        return obs.fare_brand_raw or "unknown brand"
    return obs.fare_brand.value.replace("_", " ").title()


def _fmt_seen(obs: FareObservation) -> str:
    return f"{obs.provider.value} {obs.observed_at.astimezone(UTC):%H:%M} UTC"


def _fmt_offset(dt: datetime) -> str:
    off = dt.utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _brand_unverified(obs: FareObservation) -> bool:
    return obs.risk is not None and obs.risk.brand_unverified


def _prefixes(obs: FareObservation) -> str:
    out: list[str] = []
    if obs.risk is not None and obs.risk.brand_unverified:
        out.append("[brand unverified]")
    if obs.risk is not None and obs.risk.inbound_unverified:
        out.append("[return not checked]")
    return "".join(f"{p} " for p in out)


def _clip(message: str) -> str:
    return message if len(message) <= MAX_MESSAGE_CHARS else message[: MAX_MESSAGE_CHARS - 1] + "…"


def _fare_line(target: SearchTarget, obs: FareObservation) -> str:
    sl = obs.itinerary.outbound
    return " · ".join(
        [
            _fmt_dates(target),
            fmt_itin(sl),
            _fmt_brand(obs),
            f"{_fmt_clock(sl.departure_local)}–{_fmt_clock(sl.arrival_local)}",
        ]
    )


# --------------------------------------------------------------------------- #
# strike (section 3)
# --------------------------------------------------------------------------- #


def strike_reference(
    wl: ResolvedWatchlist, target: SearchTarget, regime: Regime, state: TargetState
) -> tuple[int | None, float, str]:
    """(reference_cents, multiplier, name) for this regime's strike rule."""
    rule = wl.strike_rule(regime)
    if rule is None:
        return None, 0.0, "-"
    band = state.band
    ref: int | None
    if rule.reference == "typical_low":
        ref = None if band is None else band.typical_low_cents
    elif rule.reference == "typical_high":
        ref = None if band is None else band.typical_high_cents
    elif rule.reference == "lowest_seen":
        ref = state.lowest_seen_cents
    else:
        ref = wl.ceiling_cents
    return ref, rule.multiplier, rule.reference


def _within(price_cents: int, ref: int | None, mult: float) -> bool:
    """price <= ref * mult, exactly. The multiplier is read from its shortest decimal repr
    (StrikeRule allows at most 3 decimal places), so 1.15 means 1.15 and not its float neighbour."""
    return ref is not None and Decimal(price_cents) <= Decimal(ref) * Decimal(str(mult))


# --------------------------------------------------------------------------- #
# alert construction
# --------------------------------------------------------------------------- #


def alert_key(
    target_id: str, kind: AlertKind, itinerary_key: str | None, price_cents: int | None
) -> str:
    bucket = price_cents // 1000 if price_cents else 0
    return f"{target_id}:{kind.value}:{itinerary_key or '-'}:{bucket}"


def _alert(
    target: SearchTarget,
    kind: AlertKind,
    regime: Regime,
    message: str,
    *,
    now: datetime,
    obs: FareObservation | None = None,
    itinerary_key: str | None = None,
    price_cents: int | None = None,
    reference_cents: int | None = None,
) -> Alert:
    if obs is not None:
        itinerary_key = obs.itinerary_key
        price_cents = obs.price_cents
    return Alert(
        created_at=now,
        watchlist_id=target.watchlist_id,
        target_id=target.target_id,
        kind=kind,
        regime=regime,
        observation_id=None if obs is None else obs.id,
        itinerary_key=itinerary_key,
        price_cents=price_cents,
        reference_cents=reference_cents,
        message=_clip(message),
        alert_key=alert_key(target.target_id, kind, itinerary_key, price_cents),
    )


def _cheapest(candidates: list[FareObservation]) -> FareObservation | None:
    return min(candidates, key=lambda o: o.price_cents, default=None)


# --------------------------------------------------------------------------- #
# the three rule groups
# --------------------------------------------------------------------------- #


def _strike(
    wl: ResolvedWatchlist,
    target: SearchTarget,
    regime: Regime,
    observations: list[FareObservation],
    state: TargetState,
    now: datetime,
) -> Alert | None:
    ref, mult, name = strike_reference(wl, target, regime, state)
    candidates = [
        o
        for o in observations
        if o.risk is not None
        and o.risk.passed
        and not (o.risk.brand_unverified and o.fare_brand_raw is None)
        and (o.price_cents <= wl.ceiling_cents or _within(o.price_cents, ref, mult))
    ]
    # a verified fare beats a cheaper unverified one: the cheapest unverified is only the
    # winner when nothing verified qualified, and is otherwise mentioned in the body
    verified = [o for o in candidates if not _brand_unverified(o)]
    unverified = [o for o in candidates if _brand_unverified(o)]
    best = _cheapest(verified) or _cheapest(unverified)
    if best is None:
        return None
    cheaper_unverified = _cheapest([o for o in unverified if o.price_cents < best.price_cents])

    by_band = _within(best.price_cents, ref, mult)
    reference = ref if by_band else wl.ceiling_cents
    kind = AlertKind.EXTREME_LOW if regime is Regime.MONITOR else AlertKind.BUY
    label = "Anomaly" if kind is AlertKind.EXTREME_LOW else "BUY"
    dtd = target.days_to_departure(now)
    title = (
        f"{_prefixes(best)}{label} {fmt_route(target)} {fmt_money(best.price_cents)} "
        f"(DTD {dtd}, {regime.value})"
    )

    shown = fmt_money(best.price_cents)
    if by_band and name != "ceiling":
        assert ref is not None
        versus = f"{shown} vs {name.replace('_', '-')} {fmt_money(ref)} × {mult:.2f}"
        versus += f" (ceiling {fmt_money(wl.ceiling_cents)})"
    elif by_band:
        versus = f"{shown} vs ceiling {fmt_money(wl.ceiling_cents)} × {mult:.2f}"
    else:
        versus = f"{shown} vs ceiling {fmt_money(wl.ceiling_cents)}"
    lines = [title, _fare_line(target, best), f"{versus} · {_fmt_seen(best)}"]
    if cheaper_unverified is not None:
        lines.append(
            f"also seen: {fmt_money(cheaper_unverified.price_cents)} "
            f'"{cheaper_unverified.fare_brand_raw}" (brand unverified)'
        )
    message = "\n".join(lines)
    return _alert(target, kind, regime, message, now=now, obs=best, reference_cents=reference)


def _fence_warning(wl: ResolvedWatchlist, target: SearchTarget, now: datetime) -> Alert:
    siblings = sum(
        1
        for t in wl.targets
        if t.target_id != target.target_id and wl.regime_for(t, now) is Regime.PRIME
    )
    message = "\n".join(
        [
            f"21-day fence {fmt_route(target)} — buy window closing",
            f"{_fmt_dates(target)} · DTD {target.days_to_departure(now)}",
            f"{siblings} sibling target{'' if siblings == 1 else 's'} still in prime",
        ]
    )
    return _alert(target, AlertKind.FENCE_WARNING, Regime.FENCE, message, now=now)


def _post_purchase(
    wl: ResolvedWatchlist,
    target: SearchTarget,
    observations: list[FareObservation],
    state: TargetState,
    now: datetime,
) -> list[Alert]:
    purchase = wl.purchase
    assert purchase is not None
    pp = wl.post_purchase
    alerts: list[Alert] = []

    ours = [
        o
        for o in observations
        if o.itinerary_key == purchase.itinerary_key
        and o.risk is not None
        and o.risk.passed
        and not (o.risk.brand_unverified and o.fare_brand_raw is None)
        and o.fare_brand is not FareBrand.BASIC_ECONOMY  # a Basic fare has no credit to harvest
        and o.changeable is not False
    ]
    best = _cheapest(ours)
    if (
        best is not None
        and purchase.changeable
        and best.price_cents <= purchase.price_cents - pp.credit_threshold_cents
    ):
        route = fmt_route(target)
        message = "\n".join(
            [
                f"{_prefixes(best)}Re-fare {route}: now {fmt_money(best.price_cents)}, "
                f"paid {fmt_money(purchase.price_cents)}",
                _fare_line(target, best),
                f"{fmt_money(best.price_cents)} vs paid {fmt_money(purchase.price_cents)} "
                f"(−{fmt_money(purchase.price_cents - best.price_cents)}) · {_fmt_seen(best)}",
            ]
        )
        alerts.append(
            _alert(
                target,
                AlertKind.CREDIT_DROP,
                Regime.POST_PURCHASE,
                message,
                now=now,
                obs=best,
                reference_cents=purchase.price_cents,
            )
        )

    window = timedelta(hours=pp.dot_window_hours)
    bought_utc = purchase.purchased_at.astimezone(UTC)
    # the 7-day rule is about the purchaser's calendar day, in the zone the purchase was made in
    bought_days_ahead = (target.depart - purchase.purchased_at.date()).days
    if (
        now - bought_utc < window
        and bought_days_ahead >= 7
        and AlertKind.DOT_24H not in state.already_alerted
    ):
        closes = (bought_utc + window).astimezone(purchase.purchased_at.tzinfo)
        day = (closes.date() - now.astimezone(closes.tzinfo).date()).days
        when = "today" if day <= 0 else "tomorrow" if day == 1 else _fmt_day(closes)
        zone = closes.strftime("%Z") or _fmt_offset(closes)
        message = "\n".join(
            [
                f"DOT {pp.dot_window_hours}h window on {fmt_route(target)} closes "
                f"{closes:%H:%M} {when} ({zone})",
                f"{_fmt_dates(target)} · paid {fmt_money(purchase.price_cents)} · {purchase.carrier}",
                f"Free cancellation until {closes:%b} {closes.day} {closes:%H:%M %Z}".rstrip(),
            ]
        )
        alerts.append(
            _alert(
                target,
                AlertKind.DOT_24H,
                Regime.POST_PURCHASE,
                message,
                now=now,
                itinerary_key=purchase.itinerary_key,
            )
        )
    return alerts


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def evaluate(
    wl: ResolvedWatchlist,
    target: SearchTarget,
    observations: list[FareObservation],
    state: TargetState,
    *,
    now: datetime,
    run_id: str,  # part of the contract; alerts carry no run id today
    config_hash: str,  # observations already carry the verdict's config_hash
) -> list[Alert]:
    """Alerts for ONE target from THIS run's observations (at most one price-driven alert)."""
    regime = wl.regime_for(target, now)
    if regime is Regime.EXPIRED or target.depart < now.date():
        return []
    if regime is Regime.POST_PURCHASE:
        return _post_purchase(wl, target, observations, state, now)

    alerts: list[Alert] = []
    if regime in _PRICE_DRIVEN_REGIMES:
        strike = _strike(wl, target, regime, observations, state, now)
        if strike is not None:
            alerts.append(strike)
    if regime is Regime.FENCE and AlertKind.FENCE_WARNING not in state.already_alerted:
        alerts.append(_fence_warning(wl, target, now))
    return alerts
