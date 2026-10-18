"""ARCHITECTURE.md section 4: every risk-filter row, constructed so only that rule fires.

Baseline: SEA->SJC AS nonstop 08:00-10:30 PT, MAIN_CABIN, default Constraints. Each test
changes exactly one thing and asserts the *entire* rejection list, so a rule that
over-fires or a missing mitigation both fail.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.engine.risk_filters import apply, risk_verdict
from src.models import (
    Constraints,
    FareBrand,
    Itinerary,
    ResolvedWatchlist,
    Segment,
    Slice,
)
from tests.conftest import make_obs, thanksgiving

HASH = "cfg-abc123"
ZONES = {
    "SEA": "America/Los_Angeles", "SJC": "America/Los_Angeles", "SFO": "America/Los_Angeles",
    "SLC": "America/Denver", "DEN": "America/Denver",
    "ORD": "America/Chicago",
    "EWR": "America/New_York", "JFK": "America/New_York", "BOS": "America/New_York",
}  # fmt: skip


def at(airport: str, day: int, hour: int, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo(ZONES[airport]))


def seg(carrier, num, origin, dest, dep, arr, operating=None) -> Segment:
    return Segment(carrier=carrier, operating_carrier=operating, flight_number=num,
                   origin=origin, destination=dest, departure_local=dep, arrival_local=arr)  # fmt: skip


def nonstop(dep=(25, 8, 0), arr=(25, 10, 30), carrier="AS") -> Slice:
    return Slice(segments=[seg(carrier, "327", "SEA", "SJC", at("SEA", *dep), at("SJC", *arr))])


def one_stop(layover_min: int, carrier="AS", second_carrier=None) -> Slice:
    """SEA->SLC 07:00-10:00 MT, then SLC->SJC departing `layover_min` after arrival."""
    arr1 = at("SLC", 25, 10, 0)
    dep2 = arr1 + timedelta(minutes=layover_min)
    return Slice(segments=[
        seg(carrier, "100", "SEA", "SLC", at("SEA", 25, 7, 0), arr1),
        seg(second_carrier or carrier, "200", "SLC", "SJC", dep2,
            (dep2 + timedelta(hours=2)).astimezone(ZoneInfo(ZONES["SJC"]))),
    ])  # fmt: skip


def back_slice() -> Slice:
    return Slice(
        segments=[seg("AS", "328", "SJC", "SEA", at("SJC", 29, 18), at("SEA", 29, 20, 30))]
    )


def obs(*slices: Slice, brand: FareBrand = FareBrand.MAIN_CABIN, **kw):
    itin = Itinerary(slices=list(slices) if slices else [nonstop(), back_slice()])
    return make_obs(itinerary=itin, fare_brand=brand, risk=None, **kw)


def wl_with(**constraints) -> ResolvedWatchlist:
    return thanksgiving().model_copy(update={"constraints": Constraints(**constraints)})


def verdict(o, wl=None):
    return risk_verdict(o, wl or thanksgiving(), config_hash=HASH)


# --------------------------------------------------------------------------- baseline


def test_clean_itinerary_passes_with_no_tags():
    v = verdict(obs(nonstop()))
    assert (v.passed, v.rejections, v.brand_unverified) == (True, [], False)


def test_verdict_carries_the_config_hash():
    assert verdict(obs(nonstop())).config_hash == HASH


def test_price_is_never_an_input():
    cheap = verdict(obs(nonstop(), price=1))
    pricey = verdict(obs(nonstop(), price=9_000_000))
    assert cheap == pricey


# --------------------------------------------------------------------------- basic economy


def test_rejects_basic_economy_by_default():
    v = verdict(obs(nonstop(), brand=FareBrand.BASIC_ECONOMY))
    assert (v.passed, v.rejections) == (False, ["basic_economy"])


def test_basic_economy_passes_when_watchlist_allows_it():
    v = verdict(obs(nonstop(), brand=FareBrand.BASIC_ECONOMY), wl_with(allow_basic_economy=True))
    assert (v.passed, v.rejections) == (True, [])


# --------------------------------------------------------------------------- stops


def test_rejects_two_stops_when_max_stops_is_one():
    three = Slice(segments=[
        seg("AS", "1", "SEA", "SLC", at("SEA", 25, 7), at("SLC", 25, 10)),
        seg("AS", "2", "SLC", "DEN", at("SLC", 25, 11), at("DEN", 25, 12, 30)),
        seg("AS", "3", "DEN", "SJC", at("DEN", 25, 13, 30), at("SJC", 25, 15)),
    ])  # fmt: skip
    assert verdict(obs(three)).rejections == ["stops>1"]


def test_one_stop_passes_when_max_stops_is_one():
    assert verdict(obs(one_stop(90))).rejections == []


def test_tag_uses_configured_max_stops():
    v = verdict(obs(one_stop(90)), wl_with(max_stops=0))
    assert v.rejections == ["stops>0"]


# --------------------------------------------------------------------------- layover


def test_layover_of_exactly_max_passes():
    assert verdict(obs(one_stop(180))).rejections == []


def test_layover_one_minute_over_max_fails():
    v = verdict(obs(one_stop(181)))
    assert (v.passed, v.rejections) == (False, ["layover>180"])


def test_layover_tag_uses_configured_max():
    v = verdict(obs(one_stop(241)), wl_with(max_layover_minutes=240))
    assert v.rejections == ["layover>240"]
    assert verdict(obs(one_stop(240)), wl_with(max_layover_minutes=240)).rejections == []


# --------------------------------------------------------------------------- overnight


def test_ninety_minute_layover_across_midnight_fails_overnight_only():
    s = Slice(segments=[
        seg("AS", "100", "SEA", "SLC", at("SEA", 25, 20, 30), at("SLC", 25, 22, 45)),
        seg("AS", "200", "SLC", "SJC", at("SLC", 26, 0, 15), at("SJC", 25, 23, 0)),
    ])  # fmt: skip
    assert s.layovers_minutes == [90]  # fixture sanity: short layover, different local date
    v = verdict(obs(s))
    assert v.rejections == ["overnight_layover"]


def test_overnight_layover_passes_when_rule_disabled():
    s = Slice(segments=[
        seg("AS", "100", "SEA", "SLC", at("SEA", 25, 20, 30), at("SLC", 25, 22, 45)),
        seg("AS", "200", "SLC", "SJC", at("SLC", 26, 0, 15), at("SJC", 25, 23, 0)),
    ])  # fmt: skip
    assert verdict(obs(s), wl_with(reject_overnight_layover=False)).rejections == []


# --------------------------------------------------------------------------- co-terminal


def coterminal_slice() -> Slice:
    return Slice(segments=[
        seg("DL", "1", "SEA", "EWR", at("SEA", 7, 10, month=12), at("EWR", 7, 15, 35, month=12)),
        seg("DL", "2", "JFK", "BOS", at("JFK", 7, 17, 30, month=12), at("BOS", 7, 18, 40, month=12)),
    ])  # fmt: skip


def test_ewr_to_jfk_swap_fails():
    assert verdict(obs(coterminal_slice())).rejections == ["coterminal_swap"]


def test_coterminal_swap_passes_when_allowed():
    v = verdict(obs(coterminal_slice()), wl_with(allow_coterminal_swap=True))
    assert v.rejections == []


def test_same_airport_connection_at_ord_passes():
    s = Slice(segments=[
        seg("UA", "1", "SEA", "ORD", at("SEA", 25, 7), at("ORD", 25, 13)),
        seg("UA", "2", "ORD", "JFK", at("ORD", 25, 14, 30), at("JFK", 25, 17, 45)),
    ])  # fmt: skip
    assert verdict(obs(s)).rejections == []


# --------------------------------------------------------------------------- time of day


def test_departing_at_exactly_earliest_local_passes():
    assert verdict(obs(nonstop(dep=(25, 6, 0), arr=(25, 8, 30)))).rejections == []


def test_departing_one_minute_before_earliest_local_fails():
    v = verdict(obs(nonstop(dep=(25, 5, 59), arr=(25, 8, 30))))
    assert v.rejections == ["departs_before_06:00"]


def test_departure_tag_uses_configured_time():
    v = verdict(obs(nonstop(dep=(25, 6, 30))), wl_with(earliest_departure_local=time(7, 0)))
    assert v.rejections == ["departs_before_07:00"]


def test_arriving_at_exactly_latest_local_passes():
    assert verdict(obs(nonstop(dep=(25, 21, 0), arr=(25, 23, 30)))).rejections == []


def test_arriving_one_minute_after_latest_local_fails():
    v = verdict(obs(nonstop(dep=(25, 21, 0), arr=(25, 23, 31))))
    assert v.rejections == ["arrives_after_23:30"]


def test_arrival_tag_uses_configured_time():
    v = verdict(obs(nonstop(arr=(25, 22, 15))), wl_with(latest_arrival_local=time(22, 0)))
    assert v.rejections == ["arrives_after_22:00"]


# --------------------------------------------------------------------------- carriers


def test_allowed_carriers_rejects_foreign_carrier_on_any_segment():
    v = verdict(
        obs(one_stop(90, carrier="AS", second_carrier="DL")), wl_with(allowed_carriers=["AS"])
    )
    assert (v.passed, v.rejections) == (False, ["carrier_DL"])


def test_allowed_carriers_emits_one_sorted_tag_per_offending_carrier():
    s = Slice(segments=[
        seg("UA", "1", "SEA", "SLC", at("SEA", 25, 7), at("SLC", 25, 10)),
        seg("DL", "2", "SLC", "DEN", at("SLC", 25, 11), at("DEN", 25, 12, 30)),
        seg("AS", "3", "DEN", "SJC", at("DEN", 25, 13, 30), at("SJC", 25, 15)),
    ])  # fmt: skip
    v = verdict(obs(s), wl_with(allowed_carriers=["AS"], max_stops=2))
    assert v.rejections == ["carrier_DL", "carrier_UA"]


def test_allowed_carriers_accepts_listed_carriers_in_either_slice():
    v = verdict(obs(nonstop(), back_slice()), wl_with(allowed_carriers=["AS", "UA"]))
    assert v.rejections == []


def test_allowed_carriers_judges_marketing_carrier_not_operating():
    s = Slice(
        segments=[
            seg("AS", "327", "SEA", "SJC", at("SEA", 25, 8), at("SJC", 25, 10, 30), operating="QX")
        ]
    )
    assert verdict(obs(s), wl_with(allowed_carriers=["AS"])).rejections == []


def test_carrier_in_inbound_slice_is_checked():
    back = Slice(segments=[seg("DL", "9", "SJC", "SEA", at("SJC", 29, 18), at("SEA", 29, 20, 30))])
    v = verdict(obs(nonstop(), back), wl_with(allowed_carriers=["AS"]))
    assert v.rejections == ["carrier_DL"]


def test_empty_allowed_carriers_means_any_carrier():
    assert verdict(obs(nonstop(carrier="F9"))).rejections == []


def test_excluded_carrier_is_rejected():
    v = verdict(obs(nonstop(carrier="NK")), wl_with(excluded_carriers=["NK"]))
    assert (v.passed, v.rejections) == (False, ["carrier_NK"])


def test_excluded_carriers_leave_other_carriers_alone():
    assert verdict(obs(nonstop(carrier="AS")), wl_with(excluded_carriers=["NK"])).rejections == []


# --------------------------------------------------------------------------- unknown brand


def test_unknown_brand_passes_flagged_unverified():
    v = verdict(obs(nonstop(), brand=FareBrand.UNKNOWN))
    assert (v.passed, v.rejections, v.brand_unverified) == (True, [], True)
    assert v.config_hash == HASH


def test_unknown_brand_still_fails_on_other_rules_and_stays_flagged():
    v = verdict(obs(one_stop(240), brand=FareBrand.UNKNOWN))
    assert (v.passed, v.rejections, v.brand_unverified) == (False, ["layover>180"], True)


@pytest.mark.parametrize("brand", [b for b in FareBrand if b is not FareBrand.UNKNOWN])
def test_known_brands_are_not_flagged_unverified(brand):
    wl = wl_with(allow_basic_economy=True)
    assert verdict(obs(nonstop(), brand=brand), wl).brand_unverified is False


# --------------------------------------------------------------------------- multi-tag, ordering


def test_basic_economy_and_long_layover_both_fire_in_table_order():
    v = verdict(obs(one_stop(200), brand=FareBrand.BASIC_ECONOMY))
    assert v.rejections == ["basic_economy", "layover>180"]


def test_every_rule_fires_and_tags_follow_table_order():
    """SEA 05:30 -> EWR 22:00; JFK 01:30 (+1) -> BOS 23:45 (+1); DL; BE; max_stops=0; allowed=[AS]."""
    s = Slice(segments=[
        seg("DL", "1", "SEA", "EWR", at("SEA", 25, 5, 30), at("EWR", 25, 22, 0)),
        seg("DL", "2", "JFK", "BOS", at("JFK", 26, 1, 30), at("BOS", 26, 23, 45)),
    ])  # fmt: skip
    assert s.layovers_minutes == [210]
    wl = wl_with(max_stops=0, allowed_carriers=["AS"])
    v = verdict(obs(s, brand=FareBrand.BASIC_ECONOMY), wl)
    assert v.rejections == [
        "basic_economy",
        "stops>0",
        "layover>180",
        "coterminal_swap",
        "overnight_layover",
        "departs_before_06:00",
        "arrives_after_23:30",
        "carrier_DL",
    ]
    assert v.passed is False


def test_same_rule_in_both_slices_yields_one_tag():
    inbound = Slice(segments=[
        seg("AS", "1", "SJC", "SLC", at("SJC", 29, 7), at("SLC", 29, 10)),
        seg("AS", "2", "SLC", "SEA", at("SLC", 29, 13, 30), at("SEA", 29, 14, 30)),
    ])  # fmt: skip
    v = verdict(obs(one_stop(240), inbound))
    assert v.rejections == ["layover>180"]


def test_round_trip_fails_when_only_the_inbound_slice_violates():
    inbound = Slice(segments=[
        seg("AS", "1", "SJC", "SLC", at("SJC", 29, 7), at("SLC", 29, 10)),
        seg("AS", "2", "SLC", "SEA", at("SLC", 29, 14), at("SEA", 29, 15)),
    ])  # fmt: skip
    outbound_only = verdict(obs(nonstop()))
    assert outbound_only.passed is True
    v = verdict(obs(nonstop(), inbound))
    assert (v.passed, v.rejections) == (False, ["layover>180"])


def test_round_trip_inbound_early_departure_fails():
    inbound = Slice(
        segments=[seg("AS", "328", "SJC", "SEA", at("SJC", 29, 5, 15), at("SEA", 29, 7, 40))]
    )
    assert verdict(obs(nonstop(), inbound)).rejections == ["departs_before_06:00"]


# --------------------------------------------------------------------------- apply()


def test_apply_sets_risk_on_every_observation_in_order():
    good, bad = obs(nonstop()), obs(nonstop(), brand=FareBrand.BASIC_ECONOMY)
    out = apply([good, bad], thanksgiving(), config_hash=HASH)
    assert [o.risk.passed for o in out] == [True, False]
    assert out[1].risk.rejections == ["basic_economy"]
    assert all(o.risk.config_hash == HASH for o in out)


def test_apply_returns_copies_and_does_not_mutate_input():
    original = obs(nonstop())
    out = apply([original], thanksgiving(), config_hash=HASH)
    assert out[0] is not original
    assert original.risk is None
    assert out[0].risk is not None


def test_apply_preserves_all_other_fields():
    original = obs(nonstop(), price=21400)
    (out,) = apply([original], thanksgiving(), config_hash=HASH)
    assert out.model_dump(exclude={"risk"}) == original.model_dump(exclude={"risk"})


def test_apply_keeps_rejected_observations():
    out = apply([obs(nonstop(), brand=FareBrand.BASIC_ECONOMY)], thanksgiving(), config_hash=HASH)
    assert len(out) == 1 and out[0].risk.passed is False


def test_apply_on_empty_list_returns_empty_list():
    assert apply([], thanksgiving(), config_hash=HASH) == []


def test_apply_matches_risk_verdict():
    o = obs(one_stop(200), brand=FareBrand.UNKNOWN)
    (out,) = apply([o], thanksgiving(), config_hash=HASH)
    assert out.risk == risk_verdict(o, thanksgiving(), config_hash=HASH)
