"""Synthetic fare history (ARCHITECTURE.md section 7, `fares seed`). Pure: no DB, no wall clock.

One Run + one ApiCall + 2-3 observations + one BenchmarkBand per day, ending at `scenario.end`.
Prices follow a slow upward drift, a step at the 21-day fence (and half a step at 14), a
one-day dip (the "buy" day) and one Basic Economy trap, priced 22% under the cheapest clean fare of the whole history,
that the local risk rule rejects.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from src.models import (
    ApiCall,
    ApiCallStatus,
    BenchmarkBand,
    FareBrand,
    FareObservation,
    Itinerary,
    PriceLevel,
    Provider,
    ResolvedWatchlist,
    RiskVerdict,
    Run,
    SearchTarget,
    Segment,
    Slice,
)

SEED_CONFIG_HASH = "seed"
NOISE_PCT = 0.03
DIP_FACTOR = 0.78
TRAP_DISCOUNT = 0.22
ONE_STOP_DISCOUNT = (0.86, 0.92)  # 1-stop price as a fraction of the nonstop price
LAYOVER_MINUTES = 75


class SeedScenario(BaseModel):
    model_config = ConfigDict(frozen=True)

    target: SearchTarget
    days: int = 45  # observations: one per day, ending at `end`
    end: datetime  # UTC; last observation timestamp
    base_price_cents: int  # price at DTD=65
    typical_low_cents: int
    typical_high_cents: int
    fence_jump_pct: float = 0.18  # step up when DTD crosses 21 (and again at 14, half)
    dip_at_dtd: int | None = 34  # one-day dip to 0.78 x curve: the "buy" day
    basic_economy_trap_at_dtd: int | None = 30  # a BE fare 22% cheaper that must be rejected
    seed: int = 7  # deterministic noise


# --------------------------------------------------------------------------- #
# Fixed route tables
# --------------------------------------------------------------------------- #


class _Route(NamedTuple):
    nonstop_carrier: str
    onestop_carrier: str
    hub: str
    block_minutes: int  # nonstop block time


_DEFAULT_ROUTE = _Route("AS", "DL", "SLC", 180)
_ROUTES: dict[frozenset[str], _Route] = {
    frozenset({"SEA", "SJC"}): _Route("AS", "DL", "SLC", 145),
    frozenset({"SEA", "SFO"}): _Route("AS", "UA", "SLC", 135),
    frozenset({"SEA", "OAK"}): _Route("AS", "DL", "SLC", 145),
    frozenset({"SEA", "ORD"}): _Route("UA", "AS", "DEN", 240),
    frozenset({"SEA", "JFK"}): _Route("DL", "AA", "ORD", 330),
    frozenset({"SEA", "EWR"}): _Route("UA", "DL", "DEN", 320),
    frozenset({"SEA", "LAX"}): _Route("AS", "DL", "SLC", 175),
}
_TZ = {
    "SEA": "America/Los_Angeles",
    "PDX": "America/Los_Angeles",
    "SJC": "America/Los_Angeles",
    "SFO": "America/Los_Angeles",
    "OAK": "America/Los_Angeles",
    "LAX": "America/Los_Angeles",
    "SLC": "America/Denver",
    "DEN": "America/Denver",
    "ORD": "America/Chicago",
    "JFK": "America/New_York",
    "EWR": "America/New_York",
}

# (departure hour, minute) of each product, outbound / inbound, local to the origin of the slice
_NONSTOP_DEP = {"out": (7, 0), "back": (18, 0)}
_ONESTOP_DEP = {"out": (6, 30), "back": (16, 30)}
_BE_DEP = {"out": (11, 0), "back": (20, 0)}
_NONSTOP_NUMBER = {"out": "327", "back": "328"}
_BE_NUMBER = {"out": "333", "back": "334"}
_ONESTOP_NUMBERS = {"out": ("1807", "1809"), "back": ("1808", "1810")}


def _tz(airport: str) -> ZoneInfo:
    return ZoneInfo(_TZ.get(airport, "UTC"))


def _route(a: str, b: str) -> _Route:
    return _ROUTES.get(frozenset({a, b}), _DEFAULT_ROUTE)


def _segment(carrier: str, number: str, orig: str, dest: str, dep: datetime, minutes: int):
    arr = (dep.astimezone(UTC) + timedelta(minutes=minutes)).astimezone(_tz(dest))
    return Segment(
        carrier=carrier,
        flight_number=number,
        origin=orig,
        destination=dest,
        departure_local=dep,
        arrival_local=arr,
    )


def _slice(kind: str, product: str, orig: str, dest: str, day) -> Slice:
    route = _route(orig, dest)
    h, m = {"nonstop": _NONSTOP_DEP, "onestop": _ONESTOP_DEP, "be": _BE_DEP}[product][kind]
    dep = datetime(day.year, day.month, day.day, h, m, tzinfo=_tz(orig))
    if product == "nonstop":
        return Slice(
            segments=[
                _segment(
                    route.nonstop_carrier,
                    _NONSTOP_NUMBER[kind],
                    orig,
                    dest,
                    dep,
                    route.block_minutes,
                )
            ]
        )
    if product == "be":
        return Slice(
            segments=[
                _segment(
                    route.nonstop_carrier, _BE_NUMBER[kind], orig, dest, dep, route.block_minutes
                )
            ]
        )
    first_no, second_no = _ONESTOP_NUMBERS[kind]
    leg1 = _segment(
        route.onestop_carrier, first_no, orig, route.hub, dep, round(route.block_minutes * 0.6)
    )
    dep2 = (leg1.arrival_local.astimezone(UTC) + timedelta(minutes=LAYOVER_MINUTES)).astimezone(
        _tz(route.hub)
    )
    leg2 = _segment(
        route.onestop_carrier, second_no, route.hub, dest, dep2, round(route.block_minutes * 0.5)
    )
    return Slice(segments=[leg1, leg2])


def _itinerary(target: SearchTarget, product: str) -> Itinerary:
    slices = [_slice("out", product, target.origin, target.destination, target.depart)]
    if target.return_ is not None:
        slices.append(_slice("back", product, target.destination, target.origin, target.return_))
    return Itinerary(slices=slices)


# --------------------------------------------------------------------------- #
# Price model
# --------------------------------------------------------------------------- #


def _curve(sc: SeedScenario, dtd: int) -> float:
    price = sc.base_price_cents * (1 + 0.004 * max(0, 65 - dtd))
    if dtd <= 21:
        price *= 1 + sc.fence_jump_pct
    if dtd <= 14:
        price *= 1 + sc.fence_jump_pct / 2
    return price


def _rng(sc: SeedScenario, dtd: int, slot: str) -> random.Random:
    return random.Random(f"{sc.seed}|{sc.target.target_id}|{dtd}|{slot}")


def _local_risk(brand: FareBrand) -> RiskVerdict:
    """Minimal stand-in for risk_filters.py: Basic Economy is rejected, all else passes."""
    if brand is FareBrand.BASIC_ECONOMY:
        return RiskVerdict(passed=False, rejections=["basic_economy"], config_hash=SEED_CONFIG_HASH)
    return RiskVerdict(passed=True, config_hash=SEED_CONFIG_HASH)


def _price_level(sc: SeedScenario, lowest: int) -> PriceLevel:
    if lowest < sc.typical_low_cents:
        return PriceLevel.LOW
    if lowest > sc.typical_high_cents:
        return PriceLevel.HIGH
    return PriceLevel.TYPICAL


def generate(
    scenario: SeedScenario, *, watchlist: ResolvedWatchlist, run_id_prefix: str = "seed"
) -> tuple[list[Run], list[ApiCall], list[FareObservation], list[BenchmarkBand]]:
    target = scenario.target
    itineraries = {p: _itinerary(target, p) for p in ("nonstop", "onestop", "be")}
    runs: list[Run] = []
    calls: list[ApiCall] = []
    observations: list[FareObservation] = []
    bands: list[BenchmarkBand] = []

    # Pass 1: the clean fares for every day. The trap is priced off the cheapest of these, so it
    # always looks like a better deal than the real dip, which is the point of the trap.
    clean: list[tuple[int, int, int]] = []  # (dtd, nonstop_cents, one_stop_cents)
    for i in range(scenario.days):
        observed_at = scenario.end - timedelta(days=scenario.days - 1 - i)
        dtd = (target.depart - observed_at.date()).days
        nonstop = _curve(scenario, dtd) * (1 + _rng(scenario, dtd, "ns").uniform(-1, 1) * NOISE_PCT)
        if dtd == scenario.dip_at_dtd:
            nonstop *= DIP_FACTOR
        nonstop_cents = round(nonstop)
        lo, hi = ONE_STOP_DISCOUNT
        one_stop_cents = round(nonstop_cents * _rng(scenario, dtd, "1s").uniform(lo, hi))
        clean.append((dtd, nonstop_cents, one_stop_cents))
    cheapest_clean = min((min(ns, os_) for _, ns, os_ in clean), default=0)

    # Pass 2: assemble rows.
    for i, (dtd, nonstop_cents, one_stop_cents) in enumerate(clean):
        observed_at = scenario.end - timedelta(days=scenario.days - 1 - i)
        run_id = f"{run_id_prefix}-{i:03d}"

        fares = [
            ("nonstop", nonstop_cents, FareBrand.MAIN_CABIN, "Main"),
            ("onestop", one_stop_cents, FareBrand.MAIN_CABIN, "Main"),
        ]
        if dtd == scenario.basic_economy_trap_at_dtd:
            be_cents = round(cheapest_clean * (1 - TRAP_DISCOUNT))
            fares.append(("be", be_cents, FareBrand.BASIC_ECONOMY, "Basic"))

        day_obs = []
        for product, cents, brand, raw_brand in fares:
            is_be = brand is FareBrand.BASIC_ECONOMY
            day_obs.append(
                FareObservation(
                    watchlist_id=watchlist.id,
                    target_id=target.target_id,
                    observed_at=observed_at,
                    provider=Provider.MOCK,
                    run_id=run_id,
                    itinerary=itineraries[product],
                    price_cents=cents,
                    currency=watchlist.currency,
                    fare_brand=brand,
                    fare_brand_raw=raw_brand,
                    changeable=not is_be,
                    refundable=False,
                    carry_on_included=not is_be,
                    seats_remaining=_rng(scenario, dtd, f"seats-{product}").randint(2, 9),
                    days_to_departure=dtd,
                    risk=_local_risk(brand),
                )
            )
        observations.extend(day_obs)

        runs.append(
            Run(
                run_id=run_id,
                started_at=observed_at,
                finished_at=observed_at + timedelta(seconds=2),
                trigger="seed",
                calls_made=1,
                status="ok",
            )
        )
        calls.append(
            ApiCall(
                run_id=run_id,
                called_at=observed_at,
                provider=Provider.MOCK,
                endpoint="seed",
                watchlist_id=watchlist.id,
                target_id=target.target_id,
                status=ApiCallStatus.OK,
            )
        )
        lowest = min(o.price_cents for o in day_obs if o.risk and o.risk.passed)
        bands.append(
            BenchmarkBand(
                watchlist_id=watchlist.id,
                target_id=target.target_id,
                observed_at=observed_at,
                provider=Provider.MOCK,
                run_id=run_id,
                lowest_price_cents=lowest,
                typical_low_cents=scenario.typical_low_cents,
                typical_high_cents=scenario.typical_high_cents,
                price_level=_price_level(scenario, lowest),
                currency=watchlist.currency,
            )
        )
    return runs, calls, observations, bands
