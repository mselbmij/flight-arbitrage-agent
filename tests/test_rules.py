"""Phase 5: src/engine/rules.py (ARCHITECTURE.md sections 3, 7a; ops/phase-5-brief.md).

Pure module: every test passes an explicit `now`; there is no clock, DB or I/O under test.
Money is integer cents. Regimes are reached by choosing `depart` relative to NOW (DTD counts
from the UTC date, ARCHITECTURE section 7 "Clocks").
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.engine.rules import TargetState, evaluate, fmt_money, strike_reference
from src.models import (
    AlertKind,
    BenchmarkBand,
    FareBrand,
    FareObservation,
    Provider,
    Regime,
    ResolvedWatchlist,
    RiskVerdict,
    SearchTarget,
    StrikeRule,
    WatchlistFile,
)
from tests.conftest import make_band, make_itinerary, make_obs

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
RID = "rules-test"
PKEY = make_itinerary(flight="327").itinerary_key  # the itinerary that was "bought"
PRICE_DRIVEN = {AlertKind.BUY, AlertKind.EXTREME_LOW}


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #


def depart_in(dtd: int, now: datetime = NOW) -> date:
    return now.date() + timedelta(days=dtd)


def strike_all(reference: str, mult: float) -> dict:
    rule = {"reference": reference, "multiplier": mult}
    return {"monitor": rule, "prime": rule, "fence": rule, "late": rule}


def make_wl(
    depart: date,
    *,
    ceiling: int = 20000,
    strike: dict | None = None,
    purchase: dict | None = None,
    post_purchase: dict | None = None,
) -> tuple[ResolvedWatchlist, SearchTarget]:
    """One round-trip SEA->SJC target departing `depart`, returning 4 days later."""
    ret = depart + timedelta(days=4)
    raw: dict = {
        "id": RID,
        "origin": "SEA",
        "destination": "SJC",
        "depart": depart,
        "return": ret,
        "ceiling_cents": ceiling,
    }
    if strike is not None:
        raw["strike"] = strike
    if post_purchase is not None:
        raw["post_purchase"] = post_purchase
    if purchase is not None:
        raw["status"] = "purchased"
        raw["purchase"] = {
            "target_id": f"{RID}:SEASJC:{depart.isoformat()}:{ret.isoformat()}",
            "itinerary_key": PKEY,
            "carrier": "AS",
            "price_cents": 41800,
            "changeable": True,
            **purchase,
        }
    wl = WatchlistFile.model_validate({"version": 1, "watchlists": [raw]}).resolved()[0]
    return wl, wl.targets[0]


def verdict(
    *, passed: bool = True, brand_unverified: bool = False, inbound_unverified: bool = False
) -> RiskVerdict:
    return RiskVerdict(
        passed=passed,
        rejections=[] if passed else ["basic_economy"],
        brand_unverified=brand_unverified,
        inbound_unverified=inbound_unverified,
        config_hash="h1",
    )


def mk(
    t: SearchTarget,
    price: int,
    *,
    oid: int = 1,
    flight: str = "327",
    stops: int = 0,
    provider: Provider = Provider.SERPAPI,
    brand: FareBrand = FareBrand.MAIN_CABIN,
    raw_brand: str | None = "Main",
    observed_at: datetime = NOW,
    risk: RiskVerdict | None = None,
    changeable: bool | None = None,
) -> FareObservation:
    return make_obs(
        "run-1",
        price=price,
        id=oid,
        watchlist_id=t.watchlist_id,
        target_id=t.target_id,
        itinerary=make_itinerary(flight=flight, stops=stops),
        provider=provider,
        fare_brand=brand,
        fare_brand_raw=raw_brand,
        observed_at=observed_at,
        changeable=changeable,
        days_to_departure=t.days_to_departure(observed_at),
        risk=risk or verdict(),
    )


def band_for(t: SearchTarget, low: int | None = 31000, high: int | None = 40000):
    return make_band(
        watchlist_id=t.watchlist_id,
        target_id=t.target_id,
        typical_low_cents=low,
        typical_high_cents=high,
    )


def state(
    band: BenchmarkBand | None = None, lowest: int | None = None, alerted: tuple = ()
) -> TargetState:
    return TargetState(band=band, lowest_seen_cents=lowest, already_alerted=frozenset(alerted))


def ev(wl, t, observations, st=None, *, now: datetime = NOW):
    return evaluate(
        wl,
        t,
        observations,
        st if st is not None else state(),
        now=now,
        run_id="run-1",
        config_hash="h1",
    )


def priced(alerts):
    return [a for a in alerts if a.kind in PRICE_DRIVEN]


def only(alerts, kind: AlertKind):
    found = [a for a in alerts if a.kind is kind]
    assert len(found) == 1, f"expected one {kind}, got {[a.kind for a in alerts]}"
    return found[0]


def title(alert) -> str:
    return alert.message.split("\n", 1)[0]


# --------------------------------------------------------------------------- #
# fmt_money
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (28700, "$287"),
        (28750, "$287.50"),
        (28705, "$287.05"),
        (28701, "$287.01"),
        (100, "$1"),
        (5, "$0.05"),
        (0, "$0"),
        (99900, "$999"),
        (99999, "$999.99"),
    ],
)
def test_fmt_money_drops_cents_only_when_they_are_zero(cents: int, text: str):
    assert fmt_money(cents) == text


# --------------------------------------------------------------------------- #
# TargetState / strike_reference
# --------------------------------------------------------------------------- #


def test_target_state_is_frozen():
    st = state(lowest=100)
    with pytest.raises((ValidationError, TypeError)):
        st.lowest_seen_cents = 5  # type: ignore[misc]


@pytest.mark.parametrize(
    ("reference", "expected"),
    [("typical_low", 31000), ("typical_high", 40000), ("lowest_seen", 27500), ("ceiling", 20000)],
)
def test_strike_reference_reads_the_configured_source(reference: str, expected: int):
    wl, t = make_wl(depart_in(40), strike=strike_all(reference, 1.0))
    ref, mult, name = strike_reference(wl, t, Regime.PRIME, state(band_for(t), lowest=27500))
    assert ref == expected
    assert mult == 1.0
    assert name.replace("-", "_") == reference


@pytest.mark.parametrize(
    ("regime", "dtd", "mult", "reference", "expected"),
    [
        (Regime.MONITOR, 80, 0.90, "typical_low", 31000),
        (Regime.PRIME, 40, 1.00, "typical_low", 31000),
        (Regime.FENCE, 18, 1.15, "typical_low", 31000),
        (Regime.LATE, 10, 1.00, "ceiling", 20000),
    ],
)
def test_strike_reference_follows_the_default_strike_rule_per_regime(
    regime: Regime, dtd: int, mult: float, reference: str, expected: int
):
    wl, t = make_wl(depart_in(dtd))
    ref, got_mult, name = strike_reference(wl, t, regime, state(band_for(t)))
    assert (ref, got_mult) == (expected, mult)
    assert name.replace("-", "_") == reference


def test_strike_reference_is_none_without_a_band():
    wl, t = make_wl(depart_in(40))
    ref, mult, _ = strike_reference(wl, t, Regime.PRIME, state(None))
    assert ref is None
    assert mult == 1.0  # the multiplier is still reported


def test_strike_reference_is_none_for_a_band_without_typical_low():
    wl, t = make_wl(depart_in(40))
    unusable = band_for(t, low=None, high=None)
    assert strike_reference(wl, t, Regime.PRIME, state(unusable))[0] is None


def test_strike_reference_typical_high_is_none_when_band_has_no_high():
    wl, t = make_wl(depart_in(40), strike=strike_all("typical_high", 1.0))
    assert strike_reference(wl, t, Regime.PRIME, state(band_for(t, high=None)))[0] is None


def test_strike_reference_lowest_seen_is_none_with_no_history():
    wl, t = make_wl(depart_in(40), strike=strike_all("lowest_seen", 1.0))
    assert strike_reference(wl, t, Regime.PRIME, state(band_for(t), lowest=None))[0] is None


@pytest.mark.parametrize("regime", [Regime.POST_PURCHASE, Regime.EXPIRED])
def test_strike_reference_has_no_reference_outside_the_strike_regimes(regime: Regime):
    wl, t = make_wl(depart_in(40))
    assert strike_reference(wl, t, regime, state(band_for(t), lowest=1)) == (None, 0.0, "-")


# --------------------------------------------------------------------------- #
# regime routing (ARCHITECTURE section 3 table)
# --------------------------------------------------------------------------- #


def test_expired_target_never_alerts_even_for_a_fare_under_the_ceiling():
    wl, t = make_wl(depart_in(-1))
    assert ev(wl, t, [mk(t, 5000)], state(band_for(t))) == []


@pytest.mark.parametrize(
    ("dtd", "regime"),
    [
        (66, Regime.MONITOR),
        (65, Regime.PRIME),
        (22, Regime.PRIME),
        (21, Regime.FENCE),
        (15, Regime.FENCE),
        (14, Regime.LATE),
        (0, Regime.LATE),
    ],
)
def test_ceiling_strike_carries_the_regime_for_that_dtd(dtd: int, regime: Regime):
    wl, t = make_wl(depart_in(dtd), ceiling=20000)
    [alert] = priced(
        ev(wl, t, [mk(t, 19000)], state(band_for(t), alerted=[AlertKind.FENCE_WARNING]))
    )
    assert alert.regime is regime
    assert alert.kind is (AlertKind.EXTREME_LOW if regime is Regime.MONITOR else AlertKind.BUY)


def test_monitor_strikes_at_typical_low_times_0_90_and_is_an_anomaly():
    wl, t = make_wl(depart_in(80), ceiling=10000)
    st = state(band_for(t, low=30000))
    assert only(ev(wl, t, [mk(t, 27000)], st), AlertKind.EXTREME_LOW).price_cents == 27000
    assert ev(wl, t, [mk(t, 27001)], st) == []


def test_prime_strikes_at_typical_low_times_1_00():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    st = state(band_for(t, low=30000))
    assert only(ev(wl, t, [mk(t, 30000)], st), AlertKind.BUY).price_cents == 30000
    assert ev(wl, t, [mk(t, 30001)], st) == []


def test_fence_strikes_at_typical_low_times_1_15():
    wl, t = make_wl(depart_in(18), ceiling=10000)
    st = state(band_for(t, low=30000), alerted=[AlertKind.FENCE_WARNING])
    assert only(ev(wl, t, [mk(t, 34500)], st), AlertKind.BUY).price_cents == 34500
    assert ev(wl, t, [mk(t, 34501)], st) == []


def test_late_ignores_the_band_and_strikes_only_at_the_ceiling():
    wl, t = make_wl(depart_in(10), ceiling=20000)
    st = state(band_for(t, low=30000))
    assert ev(wl, t, [mk(t, 25000)], st) == []  # well under the band, over the ceiling
    assert only(ev(wl, t, [mk(t, 20000)], st), AlertKind.BUY).price_cents == 20000
    assert ev(wl, t, [mk(t, 20001)], st) == []


# --------------------------------------------------------------------------- #
# strike arithmetic: integer compare, no float money
# --------------------------------------------------------------------------- #


def test_multiplier_1_15_on_31000_qualifies_at_35650_and_not_at_35651():
    # int(1.15 * 100) == 114 in IEEE-754: a naive int() truncation silently gives 35340 here
    wl, t = make_wl(depart_in(40), ceiling=10000, strike=strike_all("typical_low", 1.15))
    st = state(band_for(t, low=31000, high=40000))
    assert only(ev(wl, t, [mk(t, 35650)], st), AlertKind.BUY).price_cents == 35650
    assert ev(wl, t, [mk(t, 35651)], st) == []


def test_multiplier_0_9_on_30001_qualifies_at_27000_and_not_at_27001():
    # 30001 * 90 = 2700090; price * 100 <= 2700090  <=>  price <= 27000
    wl, t = make_wl(depart_in(80), ceiling=10000, strike=strike_all("typical_low", 0.9))
    st = state(band_for(t, low=30001, high=40000))
    assert only(ev(wl, t, [mk(t, 27000)], st), AlertKind.EXTREME_LOW).price_cents == 27000
    assert ev(wl, t, [mk(t, 27001)], st) == []


@pytest.mark.parametrize("mult", [0.29, 0.58, 0.875, 1.13, 1.15, 0.95, 1.05, 1.1, 1.2])
@pytest.mark.parametrize("ref", [31000, 30001, 12345])
def test_exact_boundary_for_float_unfriendly_multipliers(ref: int, mult: float):
    # expected boundary from exact decimal arithmetic, independent of the implementation
    limit = int((Decimal(ref) * Decimal(str(mult))).to_integral_value(ROUND_FLOOR))
    wl, t = make_wl(depart_in(40), ceiling=1, strike=strike_all("typical_low", mult))
    st = state(band_for(t, low=ref, high=ref + 1000))
    assert priced(ev(wl, t, [mk(t, limit)], st)) != []
    assert ev(wl, t, [mk(t, limit + 1)], st) == []


def test_three_decimal_multiplier_0_875_on_31000_qualifies_at_27125_and_not_at_27126():
    # 31000 * 0.875 = 27125 exactly; round(mult * 100) would have turned 0.875 into 0.88
    wl, t = make_wl(depart_in(40), ceiling=1, strike=strike_all("typical_low", 0.875))
    st = state(band_for(t, low=31000, high=40000))
    assert only(ev(wl, t, [mk(t, 27125)], st), AlertKind.BUY).price_cents == 27125
    assert ev(wl, t, [mk(t, 27126)], st) == []


@pytest.mark.parametrize("mult", [0.1, 0.5, 0.875, 1.15, 1.999, 2.0, 1])
def test_strike_rule_accepts_up_to_three_decimals(mult: float):
    assert StrikeRule(reference="typical_low", multiplier=mult).multiplier == mult


@pytest.mark.parametrize("mult", [0.8751, 1.0001, 0.12345, 1.15001])
def test_strike_rule_rejects_more_than_three_decimals(mult: float):
    with pytest.raises(ValidationError, match="3 decimal"):
        StrikeRule(reference="typical_low", multiplier=mult)


def test_lowest_seen_reference_strikes_at_or_below_history():
    wl, t = make_wl(depart_in(40), ceiling=1, strike=strike_all("lowest_seen", 1.0))
    st = state(lowest=28000)
    assert only(ev(wl, t, [mk(t, 28000)], st), AlertKind.BUY).reference_cents == 28000
    assert ev(wl, t, [mk(t, 28001)], st) == []


def test_lowest_seen_with_no_history_cannot_strike_but_the_ceiling_still_does():
    wl, t = make_wl(depart_in(40), ceiling=25000, strike=strike_all("lowest_seen", 1.0))
    assert ev(wl, t, [mk(t, 26000)], state(lowest=None)) == []
    [alert] = ev(wl, t, [mk(t, 25000)], state(lowest=None))
    assert alert.kind is AlertKind.BUY
    assert alert.reference_cents == 25000


def test_typical_high_reference_strikes_against_the_band_high():
    wl, t = make_wl(depart_in(40), ceiling=1, strike=strike_all("typical_high", 1.0))
    st = state(band_for(t, low=30000, high=40000))
    assert priced(ev(wl, t, [mk(t, 40000)], st)) != []
    assert ev(wl, t, [mk(t, 40001)], st) == []


# --------------------------------------------------------------------------- #
# ceiling
# --------------------------------------------------------------------------- #


def test_ceiling_strikes_in_monitor_with_no_band_at_all():
    wl, t = make_wl(depart_in(90), ceiling=20000)
    [alert] = ev(wl, t, [mk(t, 20000)], state(None))
    assert alert.kind is AlertKind.EXTREME_LOW
    assert alert.reference_cents == 20000


def test_one_cent_over_the_ceiling_with_no_band_does_not_alert():
    wl, t = make_wl(depart_in(40), ceiling=20000)
    assert ev(wl, t, [mk(t, 20001)], state(None)) == []


def test_no_band_and_no_ceiling_hit_yields_nothing():
    wl, t = make_wl(depart_in(40), ceiling=20000)
    assert ev(wl, t, [mk(t, 25000), mk(t, 31000, oid=2, flight="999")], state(None)) == []


def test_unusable_band_and_no_ceiling_hit_yields_nothing():
    wl, t = make_wl(depart_in(40), ceiling=20000)
    assert ev(wl, t, [mk(t, 25000)], state(band_for(t, low=None, high=None))) == []


def test_no_observations_yields_nothing_outside_fence():
    wl, t = make_wl(depart_in(40))
    assert ev(wl, t, [], state(band_for(t))) == []


def test_reference_reported_is_the_band_when_both_tests_hold():
    wl, t = make_wl(depart_in(40), ceiling=32000)
    alert = only(ev(wl, t, [mk(t, 29000)], state(band_for(t, low=30000))), AlertKind.BUY)
    assert alert.reference_cents == 30000


def test_reference_reported_is_the_ceiling_when_only_the_ceiling_holds():
    wl, t = make_wl(depart_in(40), ceiling=32000)
    alert = only(ev(wl, t, [mk(t, 31000)], state(band_for(t, low=30000))), AlertKind.BUY)
    assert alert.reference_cents == 32000


# --------------------------------------------------------------------------- #
# candidate selection
# --------------------------------------------------------------------------- #


def test_cheapest_of_three_qualifying_fares_wins_and_only_one_alert_is_returned():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    obs = [
        mk(t, 28000, oid=11, flight="101"),
        mk(t, 26000, oid=12, flight="102"),
        mk(t, 29000, oid=13, flight="103"),
    ]
    alerts = priced(ev(wl, t, obs, state(band_for(t, low=30000))))
    assert len(alerts) == 1
    assert alerts[0].price_cents == 26000
    assert alerts[0].observation_id == 12
    assert alerts[0].itinerary_key == obs[1].itinerary_key


def test_a_rejected_fare_never_wins_even_when_it_is_the_cheapest():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    trap = mk(
        t, 19900, oid=1, flight="555", brand=FareBrand.BASIC_ECONOMY, risk=verdict(passed=False)
    )
    clean = mk(t, 28000, oid=2, flight="102")
    [alert] = priced(ev(wl, t, [trap, clean], state(band_for(t, low=30000))))
    assert alert.price_cents == 28000
    assert alert.observation_id == 2


def test_only_rejected_fares_in_the_strike_zone_yield_nothing():
    wl, t = make_wl(depart_in(40), ceiling=30000)
    trap = mk(t, 10000, risk=verdict(passed=False))
    assert ev(wl, t, [trap], state(band_for(t))) == []


def test_two_providers_for_the_same_itinerary_cheapest_row_wins_and_names_its_provider():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    serp = mk(t, 29000, oid=1, provider=Provider.SERPAPI)
    amad = mk(t, 28000, oid=2, provider=Provider.AMADEUS)
    assert serp.itinerary_key == amad.itinerary_key
    [alert] = priced(ev(wl, t, [serp, amad], state(band_for(t, low=30000))))
    assert alert.price_cents == 28000
    assert alert.observation_id == 2
    assert "amadeus" in alert.message
    assert "serpapi" not in alert.message


def test_unverified_brand_with_a_raw_label_buys_with_a_prefix():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    fare = mk(
        t,
        28000,
        brand=FareBrand.UNKNOWN,
        raw_brand="Saver?",
        risk=verdict(brand_unverified=True),
    )
    alert = only(ev(wl, t, [fare], state(band_for(t, low=30000))), AlertKind.BUY)
    assert alert.message.startswith("[brand unverified] BUY ")


def test_unverified_brand_without_a_raw_label_never_alerts_even_under_the_ceiling():
    wl, t = make_wl(depart_in(40), ceiling=30000)
    fare = mk(
        t, 20000, brand=FareBrand.UNKNOWN, raw_brand=None, risk=verdict(brand_unverified=True)
    )
    assert ev(wl, t, [fare], state(band_for(t, low=30000))) == []


def test_unverified_brand_without_raw_is_skipped_in_favour_of_a_dearer_verified_fare():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    ghost = mk(
        t,
        20000,
        oid=1,
        flight="555",
        brand=FareBrand.UNKNOWN,
        raw_brand=None,
        risk=verdict(brand_unverified=True),
    )
    solid = mk(t, 29000, oid=2, flight="102")
    [alert] = priced(ev(wl, t, [ghost, solid], state(band_for(t, low=30000))))
    assert alert.observation_id == 2
    assert not alert.message.startswith("[")


def unverified(t, price, *, oid=1, flight="555", raw="Light"):
    return mk(
        t,
        price,
        oid=oid,
        flight=flight,
        brand=FareBrand.UNKNOWN,
        raw_brand=raw,
        risk=verdict(brand_unverified=True),
    )


def test_a_verified_fare_beats_a_cheaper_unverified_one_that_also_qualifies():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    solid = mk(t, 29000, oid=2, flight="102")
    [alert] = priced(ev(wl, t, [unverified(t, 25000), solid], state(band_for(t, low=30000))))
    assert alert.observation_id == 2
    assert alert.price_cents == 29000
    assert not alert.message.startswith("[")


def test_the_cheaper_unverified_fare_is_named_in_an_also_seen_body_line():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    solid = mk(t, 29000, oid=2, flight="102")
    weird = unverified(t, 19000, raw="Weird")
    [alert] = priced(ev(wl, t, [solid, weird], state(band_for(t, low=30000))))
    lines = alert.message.split("\n")
    assert alert.observation_id == 2
    assert len(lines) == 4
    assert lines[3] == 'also seen: $190 "Weird" (brand unverified)'


def test_also_seen_names_the_cheapest_of_several_cheaper_unverified_fares():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    solid = mk(t, 29000, oid=2, flight="102")
    obs = [unverified(t, 21000, oid=3, flight="556", raw="Mid"), solid]
    obs.append(unverified(t, 19000, oid=4, flight="557", raw="Weird"))
    [alert] = priced(ev(wl, t, obs, state(band_for(t, low=30000))))
    assert alert.message.split("\n")[3] == 'also seen: $190 "Weird" (brand unverified)'


def test_no_also_seen_line_when_the_unverified_fare_is_dearer_than_the_verified_one():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    solid = mk(t, 27000, oid=2, flight="102")
    [alert] = priced(ev(wl, t, [solid, unverified(t, 29000)], state(band_for(t, low=30000))))
    assert alert.observation_id == 2
    assert "also seen" not in alert.message
    assert len(alert.message.split("\n")) == 3


def test_no_also_seen_line_when_the_cheaper_unverified_fare_does_not_qualify():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    solid = mk(t, 29000, oid=2, flight="102")
    ghost = mk(
        t, 100, oid=3, flight="556", brand=FareBrand.UNKNOWN, raw_brand=None,
        risk=verdict(brand_unverified=True),
    )  # fmt: skip
    [alert] = priced(ev(wl, t, [solid, ghost], state(band_for(t, low=30000))))
    assert "also seen" not in alert.message


def test_with_no_verified_candidate_the_cheapest_unverified_with_raw_wins_with_the_prefix():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    obs = [
        unverified(t, 27000, oid=1, flight="555", raw="Light"),
        unverified(t, 25000, oid=2, flight="556", raw="Lighter"),
        mk(t, 99000, oid=3, flight="102"),  # verified but nowhere near the strike
    ]
    [alert] = priced(ev(wl, t, obs, state(band_for(t, low=30000))))
    assert alert.observation_id == 2
    assert alert.message.startswith("[brand unverified] BUY ")
    assert "also seen" not in alert.message


def test_inbound_unverified_gets_the_return_not_checked_prefix():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    fare = mk(t, 28000, risk=verdict(inbound_unverified=True))
    alert = only(ev(wl, t, [fare], state(band_for(t, low=30000))), AlertKind.BUY)
    assert alert.message.startswith("[return not checked] BUY ")


def test_both_prefixes_stack_brand_first_then_return():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    fare = mk(
        t,
        28000,
        brand=FareBrand.UNKNOWN,
        raw_brand="Saver?",
        risk=verdict(brand_unverified=True, inbound_unverified=True),
    )
    alert = only(ev(wl, t, [fare], state(band_for(t, low=30000))), AlertKind.BUY)
    assert alert.message.startswith("[brand unverified] [return not checked] BUY ")


def test_prefix_applies_to_an_anomaly_too():
    wl, t = make_wl(depart_in(80), ceiling=10000)
    fare = mk(t, 20000, risk=verdict(inbound_unverified=True))
    alert = only(ev(wl, t, [fare], state(band_for(t, low=30000))), AlertKind.EXTREME_LOW)
    assert alert.message.startswith("[return not checked] Anomaly ")


def test_a_clean_fare_has_no_prefix():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    alert = only(ev(wl, t, [mk(t, 28000)], state(band_for(t, low=30000))), AlertKind.BUY)
    assert alert.message.startswith("BUY ")


# --------------------------------------------------------------------------- #
# FENCE_WARNING
# --------------------------------------------------------------------------- #


def test_fence_entry_warns_once_with_no_price():
    wl, t = make_wl(depart_in(18))
    [alert] = ev(wl, t, [], state(band_for(t)))
    assert alert.kind is AlertKind.FENCE_WARNING
    assert alert.regime is Regime.FENCE
    assert alert.price_cents is None
    assert alert.reference_cents is None
    assert alert.itinerary_key is None
    assert alert.observation_id is None


def test_fence_warning_is_suppressed_once_the_target_has_been_warned():
    wl, t = make_wl(depart_in(18))
    assert ev(wl, t, [], state(band_for(t), alerted=[AlertKind.FENCE_WARNING])) == []


def test_other_alert_kinds_in_history_do_not_suppress_the_fence_warning():
    wl, t = make_wl(depart_in(18))
    st = state(band_for(t), alerted=[AlertKind.BUY, AlertKind.DOT_24H])
    assert [a.kind for a in ev(wl, t, [], st)] == [AlertKind.FENCE_WARNING]


def test_fence_warning_accompanies_a_buy():
    wl, t = make_wl(depart_in(18), ceiling=10000)
    alerts = ev(wl, t, [mk(t, 30000)], state(band_for(t, low=30000)))
    assert sorted(a.kind for a in alerts) == sorted([AlertKind.BUY, AlertKind.FENCE_WARNING])


def test_a_buy_in_history_does_not_gate_a_new_buy():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    st = state(band_for(t, low=30000), alerted=[AlertKind.BUY, AlertKind.EXTREME_LOW])
    assert only(ev(wl, t, [mk(t, 28000)], st), AlertKind.BUY)  # dedup is the DB's job


@pytest.mark.parametrize("dtd", [80, 40, 10])
def test_no_fence_warning_outside_the_fence(dtd: int):
    wl, t = make_wl(depart_in(dtd))
    assert AlertKind.FENCE_WARNING not in [a.kind for a in ev(wl, t, [], state(band_for(t)))]


# --------------------------------------------------------------------------- #
# POST_PURCHASE
# --------------------------------------------------------------------------- #


def purchased(dtd: int = 40, *, ago: timedelta = timedelta(days=10), **purchase):
    return make_wl(
        depart_in(dtd),
        purchase={"purchased_at": NOW - ago, **purchase},
    )


def test_credit_drop_fires_at_exactly_the_threshold():
    wl, t = purchased()
    fare = mk(t, 41800 - 2500, oid=7)
    alert = only(ev(wl, t, [fare], state()), AlertKind.CREDIT_DROP)
    assert alert.regime is Regime.POST_PURCHASE
    assert alert.price_cents == 39300
    assert alert.reference_cents == 41800
    assert alert.itinerary_key == PKEY
    assert alert.observation_id == 7


def test_credit_drop_does_not_fire_one_cent_short_of_the_threshold():
    wl, t = purchased()
    assert ev(wl, t, [mk(t, 39301)], state()) == []


def test_credit_drop_uses_the_configured_threshold():
    wl, t = make_wl(
        depart_in(40),
        purchase={"purchased_at": NOW - timedelta(days=10)},
        post_purchase={"credit_threshold_cents": 5000},
    )
    assert only(ev(wl, t, [mk(t, 36800)], state()), AlertKind.CREDIT_DROP)
    assert ev(wl, t, [mk(t, 36801)], state()) == []


def test_credit_drop_needs_a_changeable_fare():
    wl, t = purchased(changeable=False)
    assert ev(wl, t, [mk(t, 30000)], state()) == []


def test_credit_drop_only_watches_the_purchased_itinerary():
    wl, t = purchased()
    other = mk(t, 20000, oid=1, flight="999")
    assert other.itinerary_key != PKEY
    assert ev(wl, t, [other], state()) == []


def test_credit_drop_ignores_a_risk_rejected_row_of_the_purchased_itinerary():
    wl, t = purchased()
    trap = mk(t, 30000, risk=verdict(passed=False))
    assert ev(wl, t, [trap], state()) == []


def test_credit_drop_ignores_an_unverified_brand_row_with_no_raw_label():
    wl, t = purchased()
    ghost = mk(
        t, 30000, brand=FareBrand.UNKNOWN, raw_brand=None, risk=verdict(brand_unverified=True)
    )
    assert ev(wl, t, [ghost], state()) == []


def test_credit_drop_accepts_an_unverified_brand_row_that_has_a_raw_label_with_the_prefix():
    wl, t = purchased()
    fare = mk(
        t, 30000, brand=FareBrand.UNKNOWN, raw_brand="Saver?", risk=verdict(brand_unverified=True)
    )
    alert = only(ev(wl, t, [fare], state()), AlertKind.CREDIT_DROP)
    assert alert.message.startswith("[brand unverified] Re-fare ")


def test_credit_drop_ignores_a_basic_economy_row_of_the_purchased_itinerary():
    wl, t = purchased()
    basic = mk(t, 30000, brand=FareBrand.BASIC_ECONOMY, raw_brand="Basic")
    assert ev(wl, t, [basic], state()) == []


def test_credit_drop_ignores_a_row_that_is_not_changeable():
    wl, t = purchased()  # the purchase itself IS changeable; the cheaper re-fare is not
    assert ev(wl, t, [mk(t, 30000, changeable=False)], state()) == []


def test_credit_drop_accepts_a_row_whose_changeability_is_unknown_or_true():
    wl, t = purchased()
    assert only(ev(wl, t, [mk(t, 30000, changeable=None)], state()), AlertKind.CREDIT_DROP)
    assert only(ev(wl, t, [mk(t, 30000, changeable=True)], state()), AlertKind.CREDIT_DROP)


def test_credit_drop_skips_excluded_rows_in_favour_of_a_dearer_eligible_one():
    wl, t = purchased()
    obs = [
        mk(t, 20000, oid=1, brand=FareBrand.BASIC_ECONOMY, raw_brand="Basic"),
        mk(t, 21000, oid=2, changeable=False),
        mk(t, 38000, oid=3),
    ]
    assert only(ev(wl, t, obs, state()), AlertKind.CREDIT_DROP).observation_id == 3


def test_credit_drop_compares_the_cheapest_row_of_the_purchased_itinerary_across_providers():
    wl, t = purchased()
    obs = [
        mk(t, 40500, oid=1, provider=Provider.SERPAPI),  # above the threshold line
        mk(t, 38000, oid=2, provider=Provider.AMADEUS),
        mk(t, 20000, oid=3, flight="999"),  # a different itinerary, cheaper still
    ]
    alert = only(ev(wl, t, obs, state()), AlertKind.CREDIT_DROP)
    assert alert.price_cents == 38000
    assert alert.observation_id == 2


def test_credit_drop_when_the_fare_is_the_same_or_higher_is_silent():
    wl, t = purchased()
    assert ev(wl, t, [mk(t, 41800), mk(t, 45000, oid=2, provider=Provider.AMADEUS)], state()) == []


def test_post_purchase_never_buys_or_flags_anomalies_even_far_under_the_ceiling():
    wl, t = make_wl(
        depart_in(40),
        ceiling=30000,
        purchase={"purchased_at": NOW - timedelta(days=10), "price_cents": 12000},
    )
    obs = [mk(t, 5000, oid=1, flight="999"), mk(t, 11000, oid=2)]
    assert ev(wl, t, obs, state(band_for(t, low=30000))) == []


def test_post_purchase_does_not_warn_about_the_fence():
    wl, t = purchased(dtd=18)
    assert ev(wl, t, [], state()) == []


def test_post_purchase_with_no_observations_of_the_itinerary_is_silent():
    wl, t = purchased()
    assert ev(wl, t, [], state()) == []


# -- DOT 24h window ---------------------------------------------------------- #


def test_dot_24h_fires_inside_the_window_without_any_price_data():
    wl, t = purchased(ago=timedelta(hours=23, minutes=59))
    alert = only(ev(wl, t, [], state()), AlertKind.DOT_24H)
    assert alert.regime is Regime.POST_PURCHASE
    assert alert.price_cents is None
    assert alert.target_id == t.target_id
    assert title(alert).startswith("DOT 24h window on SEA→SJC closes ")


def test_dot_24h_does_not_fire_at_exactly_the_window_length():
    wl, t = purchased(ago=timedelta(hours=24))
    assert ev(wl, t, [], state()) == []


def test_dot_24h_does_not_fire_after_the_window():
    wl, t = purchased(ago=timedelta(hours=24, minutes=1))
    assert ev(wl, t, [], state()) == []


def test_dot_24h_honours_a_longer_configured_window():
    wl, t = make_wl(
        depart_in(40),
        purchase={"purchased_at": NOW - timedelta(hours=47)},
        post_purchase={"dot_window_hours": 48},
    )
    assert only(ev(wl, t, [], state()), AlertKind.DOT_24H)
    wl2, t2 = make_wl(
        depart_in(40),
        purchase={"purchased_at": NOW - timedelta(hours=48)},
        post_purchase={"dot_window_hours": 48},
    )
    assert ev(wl2, t2, [], state()) == []


def test_dot_24h_needs_a_purchase_made_at_least_seven_days_before_departure():
    bought = NOW - timedelta(hours=1)
    ok_wl, ok_t = make_wl(bought.date() + timedelta(days=7), purchase={"purchased_at": bought})
    assert only(ev(ok_wl, ok_t, [], state()), AlertKind.DOT_24H)
    late_wl, late_t = make_wl(bought.date() + timedelta(days=6), purchase={"purchased_at": bought})
    assert ev(late_wl, late_t, [], state()) == []


def test_dot_24h_is_gated_by_the_target_history():
    wl, t = purchased(ago=timedelta(hours=2))
    assert ev(wl, t, [], state(alerted=[AlertKind.DOT_24H])) == []


def test_dot_24h_applies_even_to_an_unchangeable_fare():
    wl, t = purchased(ago=timedelta(hours=2), changeable=False)
    assert only(ev(wl, t, [], state()), AlertKind.DOT_24H)


def test_dot_24h_accompanies_a_credit_drop():
    wl, t = purchased(ago=timedelta(hours=2))
    alerts = ev(wl, t, [mk(t, 30000)], state())
    assert sorted(a.kind for a in alerts) == sorted([AlertKind.CREDIT_DROP, AlertKind.DOT_24H])


def test_credit_drop_still_fires_after_dot_has_been_alerted():
    wl, t = purchased(ago=timedelta(hours=2))
    alerts = ev(wl, t, [mk(t, 30000)], state(alerted=[AlertKind.DOT_24H]))
    assert [a.kind for a in alerts] == [AlertKind.CREDIT_DROP]


PT = ZoneInfo("America/Los_Angeles")


def test_dot_seven_day_rule_counts_days_in_the_purchasers_own_zone():
    # bought 20:00 PDT on 10-10 (= 03:00 UTC on 10-11) for a 10-17 departure: 7 days in
    # Seattle, 6 by the UTC date, and the DOT rule is about the purchaser's calendar
    bought = datetime(2026, 10, 10, 20, 0, tzinfo=PT)
    wl, t = make_wl(date(2026, 10, 17), purchase={"purchased_at": bought})
    now = bought + timedelta(hours=1)
    assert only(ev(wl, t, [], state(), now=now), AlertKind.DOT_24H)


def test_dot_seven_day_rule_still_rejects_six_days_in_the_purchasers_own_zone():
    bought = datetime(2026, 10, 10, 20, 0, tzinfo=PT)
    wl, t = make_wl(date(2026, 10, 16), purchase={"purchased_at": bought})
    assert ev(wl, t, [], state(), now=bought + timedelta(hours=1)) == []


def test_dot_title_renders_the_close_time_in_the_purchase_zone_with_its_label():
    bought = datetime(2026, 10, 10, 20, 0, tzinfo=PT)
    wl, t = make_wl(date(2026, 10, 24), purchase={"purchased_at": bought})
    [alert] = ev(wl, t, [], state(), now=bought + timedelta(hours=1))
    assert title(alert) == "DOT 24h window on SEA→SJC closes 20:00 tomorrow (PDT)"
    assert "Oct 11 20:00 PDT" in alert.message


def test_dot_title_for_a_utc_purchase_is_labelled_utc():
    wl, t = purchased(ago=timedelta(hours=2))
    assert title(only(ev(wl, t, [], state()), AlertKind.DOT_24H)).endswith("(UTC)")


def test_dot_close_time_is_absolute_across_a_dst_change():
    # clocks fall back at 02:00 on 2026-11-01: 24 real hours after 00:30 PDT is 23:30 PST
    bought = datetime(2026, 11, 1, 0, 30, tzinfo=PT)
    wl, t = make_wl(date(2026, 11, 20), purchase={"purchased_at": bought})
    [alert] = ev(wl, t, [], state(), now=bought + timedelta(hours=1))
    assert title(alert) == "DOT 24h window on SEA→SJC closes 23:30 today (PST)"


class _NamelessZone(tzinfo):
    """UTC+05:30 with no abbreviation: strftime('%Z') is ''."""

    def utcoffset(self, dt):
        return timedelta(hours=5, minutes=30)

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return None


def test_dot_title_falls_back_to_the_numeric_offset_when_the_zone_has_no_name():
    bought = datetime(2026, 10, 10, 20, 0, tzinfo=_NamelessZone())
    assert bought.strftime("%Z") == ""
    wl, t = make_wl(date(2026, 10, 24), purchase={"purchased_at": bought})
    [alert] = ev(wl, t, [], state(), now=bought + timedelta(hours=1))
    assert title(alert).endswith("closes 20:00 tomorrow (+05:30)")


def test_dot_title_for_a_fixed_offset_zone_uses_its_name():
    bought = datetime(2026, 10, 10, 20, 0, tzinfo=timezone(timedelta(hours=-7)))
    wl, t = make_wl(date(2026, 10, 24), purchase={"purchased_at": bought})
    [alert] = ev(wl, t, [], state(), now=bought + timedelta(hours=1))
    assert title(alert).startswith("DOT 24h window on SEA→SJC closes 20:00 tomorrow (")


# --------------------------------------------------------------------------- #
# Alert fields and alert_key (ARCHITECTURE section 3)
# --------------------------------------------------------------------------- #


def test_buy_alert_carries_identity_and_is_undelivered():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    fare = mk(t, 28750, oid=42)
    when = NOW + timedelta(minutes=5)
    [alert] = evaluate(
        wl,
        t,
        [fare],
        state(band_for(t, low=30000)),
        now=when,
        run_id="run-9",
        config_hash="abc123",
    )
    assert alert.id is None
    assert alert.created_at == when
    assert alert.delivered_at is None
    assert alert.channel is None
    assert alert.watchlist_id == RID
    assert alert.target_id == t.target_id
    assert alert.kind is AlertKind.BUY
    assert alert.regime is Regime.PRIME
    assert alert.observation_id == 42
    assert alert.itinerary_key == fare.itinerary_key
    assert alert.price_cents == 28750
    assert alert.reference_cents == 30000


def test_buy_alert_key_is_target_kind_itinerary_and_thousand_cent_bucket():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    fare = mk(t, 28750)
    [alert] = ev(wl, t, [fare], state(band_for(t, low=30000)))
    assert alert.alert_key == f"{t.target_id}:buy:{fare.itinerary_key}:28"


def test_extreme_low_alert_key_uses_the_kind_value():
    wl, t = make_wl(depart_in(80), ceiling=10000)
    fare = mk(t, 21200)
    [alert] = ev(wl, t, [fare], state(band_for(t, low=25000)))
    assert alert.alert_key == f"{t.target_id}:extreme_low:{fare.itinerary_key}:21"


def test_alert_key_bucket_edges_999_cents_apart_are_the_same_bucket_and_1000_apart_are_not():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    st = state(band_for(t, low=30000))
    keys = {
        price: only(ev(wl, t, [mk(t, price)], st), AlertKind.BUY).alert_key
        for price in (28000, 28999, 29000)
    }
    assert keys[28000] == keys[28999]
    assert keys[28999] != keys[29000]
    assert keys[29000].endswith(":29")


def test_fence_warning_alert_key_has_no_itinerary_and_bucket_zero():
    wl, t = make_wl(depart_in(18))
    [alert] = ev(wl, t, [], state(band_for(t)))
    assert alert.alert_key == f"{t.target_id}:fence_warning:-:0"


def test_credit_drop_alert_key_uses_the_purchased_itinerary_and_the_current_price_bucket():
    wl, t = purchased()
    alert = only(ev(wl, t, [mk(t, 39300)], state()), AlertKind.CREDIT_DROP)
    assert alert.alert_key == f"{t.target_id}:credit_drop:{PKEY}:39"


def test_dot_alert_key_follows_the_same_format_and_has_bucket_zero():
    wl, t = purchased(ago=timedelta(hours=2))
    alert = only(ev(wl, t, [], state()), AlertKind.DOT_24H)
    assert alert.alert_key == f"{t.target_id}:dot_24h:{alert.itinerary_key or '-'}:0"


def test_every_returned_alert_key_matches_its_own_fields():
    wl, t = purchased(ago=timedelta(hours=2))
    wl2, t2 = make_wl(depart_in(18), ceiling=10000)
    alerts = ev(wl, t, [mk(t, 30000)], state()) + ev(
        wl2, t2, [mk(t2, 30000)], state(band_for(t2, low=30000))
    )
    assert len(alerts) == 4
    for a in alerts:
        bucket = a.price_cents // 1000 if a.price_cents else 0
        assert a.alert_key == f"{a.target_id}:{a.kind.value}:{a.itinerary_key or '-'}:{bucket}"


def test_evaluate_does_not_mutate_its_inputs():
    wl, t = make_wl(depart_in(40), ceiling=10000)
    obs = [mk(t, 28000, oid=1), mk(t, 29000, oid=2, flight="102")]
    before = [o.model_dump() for o in obs]
    ev(wl, t, obs, state(band_for(t, low=30000)))
    assert [o.model_dump() for o in obs] == before


# --------------------------------------------------------------------------- #
# Message text (ARCHITECTURE section 7a). Title = first line.
# --------------------------------------------------------------------------- #

SEEN = datetime(2026, 10, 22, 6, 52, tzinfo=UTC)  # 34 days before 2026-11-25


def thanksgiving_wl(**kw):
    return make_wl(date(2026, 11, 25), ceiling=kw.pop("ceiling", 32000), **kw)


def test_buy_title_is_exact_and_message_has_three_lines():
    wl, t = thanksgiving_wl()
    fare = mk(t, 28700, observed_at=SEEN)
    [alert] = ev(wl, t, [fare], state(band_for(t, low=31000)), now=SEEN)
    lines = alert.message.split("\n")
    assert lines[0] == "BUY SEA→SJC $287 (DTD 34, prime)"
    assert len(lines) == 3


def test_buy_body_names_dates_flight_brand_times_reference_and_provider():
    wl, t = thanksgiving_wl()
    fare = mk(t, 28700, observed_at=SEEN)
    [alert] = ev(wl, t, [fare], state(band_for(t, low=31000)), now=SEEN)
    _, when_line, price_line = alert.message.split("\n")
    assert "Nov 25 – Nov 29" in when_line
    assert "AS 327" in when_line
    assert "nonstop" in when_line
    assert "Main Cabin" in when_line
    assert "7:00a" in when_line and "9:25a" in when_line
    assert "$287 vs typical-low $310 × 1.00" in price_line
    assert "ceiling $320" in price_line
    assert "serpapi 06:52 UTC" in price_line


def test_buy_body_for_a_one_stop_itinerary_says_via_the_connection():
    wl, t = thanksgiving_wl()
    fare = mk(t, 28700, stops=1, observed_at=SEEN)
    [alert] = ev(wl, t, [fare], state(band_for(t, low=31000)), now=SEEN)
    assert "1 stop via SLC" in alert.message


def test_seen_time_is_converted_to_utc_before_it_is_labelled_utc():
    wl, t = thanksgiving_wl()
    seen_pt = SEEN.astimezone(PT)  # 23:52 on 10-21 local; the same instant as 06:52 UTC
    assert seen_pt.hour == 23
    # the model insists on UTC, so bypass validation to hand the formatter a non-UTC stamp
    fare = mk(t, 28700, observed_at=SEEN).model_copy(update={"observed_at": seen_pt})
    [alert] = ev(wl, t, [fare], state(band_for(t, low=31000)), now=SEEN)
    assert "serpapi 06:52 UTC" in alert.message
    assert "23:52" not in alert.message


def test_buy_with_cents_in_the_price_shows_them_in_the_title():
    wl, t = thanksgiving_wl()
    [alert] = ev(wl, t, [mk(t, 28750, observed_at=SEEN)], state(band_for(t, low=31000)), now=SEEN)
    assert title(alert) == "BUY SEA→SJC $287.50 (DTD 34, prime)"


def test_extreme_low_title_is_exact():
    wl, t = make_wl(depart_in(88), ceiling=10000)
    [alert] = ev(wl, t, [mk(t, 21200)], state(band_for(t, low=25000)))
    assert title(alert) == "Anomaly SEA→SJC $212 (DTD 88, monitor)"
    assert "typical-low $250 × 0.90" in alert.message


def test_credit_drop_title_is_exact_and_body_has_both_prices():
    wl, t = purchased()
    [alert] = ev(wl, t, [mk(t, 36800)], state())
    assert title(alert) == "Re-fare SEA→SJC: now $368, paid $418"
    assert "AS 327" in alert.message
    assert "serpapi" in alert.message
    assert "$368" in alert.message and "$418" in alert.message


def test_fence_warning_title_is_exact():
    wl, t = make_wl(depart_in(18))
    [alert] = ev(wl, t, [], state(band_for(t)))
    assert title(alert) == "21-day fence SEA→SJC — buy window closing"


def test_ceiling_only_strike_names_the_ceiling_in_the_body():
    wl, t = thanksgiving_wl(ceiling=32000)
    [alert] = ev(wl, t, [mk(t, 31000, observed_at=SEEN)], state(None), now=SEEN)
    assert "ceiling $320" in alert.message


def test_messages_fit_the_ntfy_body_budget():
    wl, t = thanksgiving_wl()
    [alert] = ev(wl, t, [mk(t, 28700, stops=1, observed_at=SEEN)], state(band_for(t)), now=SEEN)
    assert len(alert.message) <= 500
