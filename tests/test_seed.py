"""Phase 2: src/runner/seed.py (ARCHITECTURE.md section 7, `fares seed`; section 3, lowest_seen)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from freezegun import freeze_time

from src.models import ApiCallStatus, FareBrand, Provider
from src.runner.seed import SeedScenario, generate
from tests.conftest import TID, WL, thanksgiving

END = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)  # last day is DTD 10; first day is DTD 54
NOISE = 0.97 / 1.03  # worst-case ratio of two independent +/-3% noise draws


def scenario(**kw) -> SeedScenario:
    wl = thanksgiving()
    base = dict(target=wl.targets[0], end=END, base_price_cents=30000,
                typical_low_cents=27000, typical_high_cents=38000)  # fmt: skip
    return SeedScenario(**{**base, **kw})


def run(**kw):
    return generate(scenario(**kw), watchlist=thanksgiving())


@pytest.fixture(scope="module")
def data():
    return run()


def nonstop_main_by_dtd(obs):
    return {
        o.days_to_departure: o.price_cents
        for o in obs
        if o.fare_brand is FareBrand.MAIN_CABIN and o.itinerary.outbound.stops == 0
    }


def test_returns_four_lists_with_one_row_per_day(data):
    runs, calls, obs, bands = data
    assert len(runs) == 45 and len(calls) == 45 and len(bands) == 45
    assert 90 <= len(obs) <= 135


def test_days_parameter_controls_run_count():
    runs, calls, obs, bands = run(days=10)
    assert (len(runs), len(calls), len(bands)) == (10, 10, 10)


def test_last_observation_is_at_end_and_one_day_apart(data):
    _, _, obs, _ = data
    stamps = sorted({o.observed_at for o in obs})
    assert stamps[-1] == END
    assert len(stamps) == 45
    gaps = {(b - a).days for a, b in zip(stamps, stamps[1:], strict=False)}
    assert gaps == {1}


def test_dtd_range_matches_observed_dates(data):
    _, _, obs, _ = data
    dtds = {o.days_to_departure for o in obs}
    assert dtds == set(range(10, 55))


def test_every_datetime_is_timezone_aware(data):
    runs, calls, obs, bands = data
    stamps = [r.started_at for r in runs] + [c.called_at for c in calls]
    stamps += [o.observed_at for o in obs] + [b.observed_at for b in bands]
    for o in obs:
        for sl in o.itinerary.slices:
            for seg in sl.segments:
                stamps += [seg.departure_local, seg.arrival_local]
    assert all(s.tzinfo is not None for s in stamps)
    assert all(o.observed_at.utcoffset().total_seconds() == 0 for o in obs)


def test_runs_and_calls_are_wired_together(data):
    runs, calls, obs, bands = data
    run_ids = [r.run_id for r in runs]
    assert len(set(run_ids)) == 45
    assert all(r.trigger == "seed" and r.status == "ok" for r in runs)
    assert {c.run_id for c in calls} == set(run_ids)
    assert {o.run_id for o in obs} <= set(run_ids)
    assert {b.run_id for b in bands} == set(run_ids)


def test_api_calls_are_ok_mock_calls_for_the_target(data):
    _, calls, _, _ = data
    assert all(c.provider is Provider.MOCK and c.status is ApiCallStatus.OK for c in calls)
    assert {(c.watchlist_id, c.target_id) for c in calls} == {(WL, TID)}


def test_run_id_prefix_is_applied():
    runs, *_ = generate(scenario(days=3), watchlist=thanksgiving(), run_id_prefix="zz")
    assert all(r.run_id.startswith("zz") for r in runs)
    default_runs, *_ = run(days=3)
    assert all(r.run_id.startswith("seed") for r in default_runs)


def test_observations_belong_to_target_and_are_mock(data):
    _, _, obs, _ = data
    assert {(o.watchlist_id, o.target_id, o.provider) for o in obs} == {(WL, TID, Provider.MOCK)}
    assert {o.currency for o in obs} == {"USD"}


def test_itineraries_match_the_target_legs(data):
    _, _, obs, _ = data
    target = thanksgiving().targets[0]
    for o in obs:
        out = o.itinerary.outbound
        assert (out.origin, out.destination) == ("SEA", "SJC")
        assert out.departure_local.date() == target.depart
        assert o.itinerary.inbound is not None
        assert o.itinerary.inbound.departure_local.date() == target.return_
        for seg in o.itinerary.outbound.segments:
            assert len(seg.carrier) == 2


def test_one_stop_itineraries_have_sane_layovers(data):
    _, _, obs, _ = data
    one_stop = [o for o in obs if o.itinerary.outbound.stops == 1]
    assert one_stop
    for o in one_stop:
        sl = o.itinerary.outbound
        assert 0 < sl.max_layover_minutes <= 180
        assert not sl.has_overnight_layover and not sl.has_coterminal_swap


def test_exactly_one_basic_economy_row_and_it_is_rejected(data):
    _, _, obs, _ = data
    be = [o for o in obs if o.fare_brand is FareBrand.BASIC_ECONOMY]
    assert len(be) == 1
    assert be[0].risk is not None and be[0].risk.passed is False
    assert be[0].days_to_departure == 30


def test_every_other_observation_passes_with_seed_config_hash(data):
    _, _, obs, _ = data
    rest = [o for o in obs if o.fare_brand is not FareBrand.BASIC_ECONOMY]
    assert all(o.risk is not None and o.risk.passed for o in rest)
    assert all(o.risk.config_hash == "seed" for o in obs)


def test_basic_economy_trap_is_cheaper_than_every_passed_fare_that_day(data):
    _, _, obs, _ = data
    day = [o for o in obs if o.days_to_departure == 30]
    be = next(o for o in day if o.fare_brand is FareBrand.BASIC_ECONOMY)
    others = [o.price_cents for o in day if o is not be]
    assert others and be.price_cents < min(others)


def test_trap_disabled_yields_no_basic_economy_and_two_obs_per_day():
    _, _, obs, _ = run(basic_economy_trap_at_dtd=None)
    assert not [o for o in obs if o.fare_brand is FareBrand.BASIC_ECONOMY]
    assert len(obs) == 90


def test_every_day_has_a_nonstop_and_a_cheaper_one_stop(data):
    _, _, obs, _ = data
    for dtd in range(10, 55):
        day = [
            o for o in obs if o.days_to_departure == dtd and o.fare_brand is FareBrand.MAIN_CABIN
        ]
        nonstop = [o.price_cents for o in day if o.itinerary.outbound.stops == 0]
        one_stop = [o.price_cents for o in day if o.itinerary.outbound.stops == 1]
        assert len(nonstop) == 1 and len(one_stop) == 1
        assert one_stop[0] < nonstop[0], dtd


def test_lowest_seen_over_passed_lands_on_the_dip_day(data):
    _, _, obs, _ = data
    passed = [o for o in obs if o.risk and o.risk.passed]
    cheapest = min(passed, key=lambda o: o.price_cents)
    assert cheapest.days_to_departure == 34


def test_lowest_seen_ignores_the_cheaper_rejected_trap(data):
    _, _, obs, _ = data
    all_min = min(o.price_cents for o in obs)
    passed_min = min(o.price_cents for o in obs if o.risk and o.risk.passed)
    assert passed_min > all_min


def test_custom_dip_day_moves_the_minimum():
    _, _, obs, _ = run(dip_at_dtd=45)
    cheapest = min((o for o in obs if o.risk and o.risk.passed), key=lambda o: o.price_cents)
    assert cheapest.days_to_departure == 45


def test_price_steps_up_when_crossing_the_21_day_fence(data):
    _, _, obs, _ = data
    p = nonstop_main_by_dtd(obs)
    assert p[20] >= p[22] * (1 + 0.18) * NOISE


def test_price_steps_up_by_half_the_jump_at_14_days(data):
    _, _, obs, _ = data
    p = nonstop_main_by_dtd(obs)
    assert p[13] >= p[15] * (1 + 0.09) * NOISE


def test_fence_jump_pct_is_respected():
    _, _, obs, _ = run(fence_jump_pct=0.40)
    p = nonstop_main_by_dtd(obs)
    assert p[20] >= p[22] * 1.40 * NOISE


def test_price_drifts_up_toward_departure_before_the_fence(data):
    _, _, obs, _ = data
    p = nonstop_main_by_dtd(obs)
    assert p[40] > p[54] * NOISE  # 14 days of 0.4%/day drift, minus worst-case noise


def test_noise_stays_within_three_percent_of_curve(data):
    _, _, obs, _ = data
    p = nonstop_main_by_dtd(obs)
    drift = lambda d: 30000 * (1 + 0.004 * (65 - d))  # noqa: E731
    for dtd in range(35, 55):  # no fence, no dip, no trap in this range
        assert abs(p[dtd] / drift(dtd) - 1) <= 0.03 + 1e-9, dtd


def test_one_band_per_day_with_cheapest_passed_as_lowest(data):
    _, _, obs, bands = data
    for b in bands:
        same_run = [o.price_cents for o in obs if o.run_id == b.run_id and o.risk.passed]
        assert b.lowest_price_cents == min(same_run)
        assert (b.typical_low_cents, b.typical_high_cents) == (27000, 38000)
        assert (b.provider, b.target_id, b.watchlist_id) == (Provider.MOCK, TID, WL)
        assert b.currency == "USD"


def test_same_seed_gives_identical_output_regardless_of_wall_clock():
    with freeze_time("2026-01-01"):
        first = run()
    with freeze_time("2030-06-06"):
        second = run()
    dump = lambda out: [[m.model_dump(mode="json") for m in part] for part in out]  # noqa: E731
    assert dump(first) == dump(second)


def test_different_seed_changes_prices():
    a = [o.price_cents for o in run(seed=7)[2]]
    b = [o.price_cents for o in run(seed=8)[2]]
    assert a != b


def test_scenario_is_frozen():
    sc = scenario()
    with pytest.raises((TypeError, ValueError)):
        sc.days = 3  # type: ignore[misc]


def test_scenario_defaults():
    sc = scenario()
    assert (sc.days, sc.fence_jump_pct, sc.dip_at_dtd) == (45, 0.18, 34)
    assert (sc.basic_economy_trap_at_dtd, sc.seed) == (30, 7)


def test_seeded_api_calls_cost_nothing():
    """Synthetic history must never consume the real daily budget (ARCHITECTURE §6)."""
    wl = thanksgiving()
    scenario = SeedScenario(
        target=wl.targets[0],
        days=5,
        end=END,
        base_price_cents=30000,
        typical_low_cents=28000,
        typical_high_cents=36000,
    )
    _, calls, _, _ = generate(scenario, watchlist=wl)
    assert calls and all(c.cost_units == 0 for c in calls)
    assert all(c.provider is Provider.MOCK for c in calls)
