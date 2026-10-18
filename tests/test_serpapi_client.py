"""SerpAPI Google Flights: pure `parse` on hand-built fixtures (phase-4 brief).

Request building and HTTP behaviour live in test_serpapi_transport.py.
"""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.ingestion.base import ProviderError, ProviderResult
from src.ingestion.serpapi_client import parse
from src.models import (
    Constraints,
    FareBrand,
    Provider,
    ResolvedWatchlist,
    SearchTarget,
    Watchlist,
    WatchlistFile,
)
from tests.conftest import T0, WL, thanksgiving

FIX = Path(__file__).parent / "fixtures" / "serpapi"
PT = ZoneInfo("America/Los_Angeles")
RUN = "run-serp-1"


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def run_parse(payload, wl: ResolvedWatchlist | None = None, target: SearchTarget | None = None):
    wl = wl or thanksgiving()
    return parse(payload, target=target or wl.targets[0], watchlist=wl, run_id=RUN, observed_at=T0)


def allow_basic_wl() -> ResolvedWatchlist:
    return thanksgiving().model_copy(update={"constraints": Constraints(allow_basic_economy=True)})


def sea_bos_oneway() -> ResolvedWatchlist:
    w = Watchlist(id="sea-bos-march", origin="SEA", destination="BOS", depart=date(2027, 3, 12),
                  ceiling_cents=45000)  # fmt: skip
    return WatchlistFile(version=1, watchlists=[w]).resolved()[0]


@pytest.fixture(scope="module")
def rt():
    return run_parse(load("serpapi_sea_sjc_rt_exclude_basic.json"))


# --------------------------------------------------------------------------- round trip, filtered


def test_rt_fixture_yields_one_observation_per_listed_itinerary_best_first(rt):
    assert isinstance(rt, ProviderResult)
    assert [o.price_cents for o in rt.observations] == [21400, 23100, 19800, 17600]
    assert [o.itinerary.outbound.segments[0].flight_number for o in rt.observations] == [
        "327", "1651", "401", "2040",
    ]  # fmt: skip


def test_rt_observation_has_a_single_outbound_slice(rt):
    assert all(len(o.itinerary.slices) == 1 for o in rt.observations)
    assert all(o.itinerary.inbound is None for o in rt.observations)


def test_rt_raw_is_marked_outbound_only(rt):
    assert rt.raw["_note"] == "outbound_only"


def test_raw_keeps_the_whole_payload(rt):
    payload = load("serpapi_sea_sjc_rt_exclude_basic.json")
    assert all(rt.raw[k] == v for k, v in payload.items())


def test_price_cents_is_integer_price_times_100(rt):
    first = rt.observations[0].price_cents
    assert first == 21400 and type(first) is int


def test_fractional_price_rounds_to_whole_cents():
    payload = copy.deepcopy(load("serpapi_sea_sjc_rt_exclude_basic.json"))
    payload["best_flights"][0]["price"] = 214.29
    assert run_parse(payload).observations[0].price_cents == 21429


def test_currency_comes_from_search_parameters(rt):
    assert {o.currency for o in rt.observations} == {"USD"}
    assert rt.band.currency == "USD"


def test_currency_defaults_to_watchlist_currency_when_payload_omits_it():
    payload = copy.deepcopy(load("serpapi_sea_sjc_rt_exclude_basic.json"))
    del payload["search_parameters"]["currency"]
    wl = thanksgiving().model_copy(update={"currency": "CAD"})
    assert {o.currency for o in run_parse(payload, wl).observations} == {"CAD"}


def test_identity_stamps_come_from_the_target_and_call(rt):
    target = thanksgiving().targets[0]
    for o in rt.observations:
        assert (o.watchlist_id, o.target_id) == (WL, target.target_id)
        assert (o.provider, o.run_id, o.observed_at) == (Provider.SERPAPI, RUN, T0)
    assert (rt.band.watchlist_id, rt.band.target_id) == (WL, target.target_id)


def test_days_to_departure_counts_from_observed_at(rt):
    assert {o.days_to_departure for o in rt.observations} == {(date(2026, 11, 25) - T0.date()).days}


def test_client_never_fills_risk(rt):
    assert all(o.risk is None for o in rt.observations)


def test_booking_url_is_the_google_flights_url(rt):
    expected = rt.raw["search_metadata"]["google_flights_url"]
    assert {o.booking_url for o in rt.observations} == {expected}


def test_fields_serpapi_does_not_provide_stay_none(rt):
    for o in rt.observations:
        assert (o.changeable, o.refundable, o.seats_remaining) == (None, None, None)


# --------------------------------------------------------------------------- segments


def test_nonstop_segment_carrier_flight_number_and_aircraft(rt):
    (s,) = rt.observations[0].itinerary.outbound.segments
    assert (s.carrier, s.flight_number, s.origin, s.destination) == ("AS", "327", "SEA", "SJC")
    assert s.aircraft == "Boeing 737"
    assert s.operating_carrier is None


def test_segment_times_are_tz_aware_local_with_the_right_offset(rt):
    (s,) = rt.observations[0].itinerary.outbound.segments
    assert s.departure_local.tzinfo == PT
    assert (s.departure_local.hour, s.departure_local.minute) == (7, 15)
    assert (s.arrival_local.hour, s.arrival_local.minute) == (9, 25)
    assert s.departure_local.utcoffset() == timedelta(hours=-8)
    assert s.arrival_local.tzinfo is not None


def test_connection_segment_is_localized_to_its_own_airport_zone(rt):
    first, second = rt.observations[1].itinerary.outbound.segments
    assert (first.carrier, first.flight_number, second.flight_number) == ("DL", "1651", "2203")
    assert first.arrival_local.utcoffset() == timedelta(hours=-7)  # SLC, mountain
    assert second.departure_local.utcoffset() == timedelta(hours=-7)
    assert second.arrival_local.utcoffset() == timedelta(hours=-8)  # SJC


def test_connection_layover_is_95_minutes(rt):
    assert rt.observations[1].itinerary.outbound.layovers_minutes == [95]


def test_overnight_layover_itinerary_shape(rt):
    sl = rt.observations[3].itinerary.outbound
    assert sl.layovers_minutes == [840]
    assert sl.has_overnight_layover is True


def test_early_departure_itinerary_shape(rt):
    sl = rt.observations[2].itinerary.outbound
    assert (sl.departure_local.hour, sl.departure_local.minute) == (5, 40)


def test_carrier_code_with_a_digit_is_kept_whole():
    payload = copy.deepcopy(load("serpapi_sea_sjc_rt_exclude_basic.json"))
    payload["best_flights"][0]["flights"][0]["flight_number"] = "B6 623"
    (s,) = run_parse(payload).observations[0].itinerary.outbound.segments
    assert (s.carrier, s.flight_number) == ("B6", "623")


# --------------------------------------------------------------------------- brand


def test_exclude_basic_filter_maps_every_fare_to_main_cabin(rt):
    for o in rt.observations:
        assert o.fare_brand is FareBrand.MAIN_CABIN
        assert o.fare_brand_raw == "google:exclude_basic"
        assert o.carry_on_included is True


def test_extension_text_maps_to_basic_economy_when_filter_not_sent():
    res = run_parse(load("serpapi_no_basic_filter_with_basic_extension.json"), allow_basic_wl())
    by_price = {o.price_cents: o for o in res.observations}
    assert by_price[12900].fare_brand is FareBrand.BASIC_ECONOMY
    assert by_price[12900].fare_brand_raw == "Basic Economy"


def test_segment_level_extension_also_maps_to_basic_case_insensitively():
    res = run_parse(load("serpapi_no_basic_filter_with_basic_extension.json"), allow_basic_wl())
    seg_level = next(o for o in res.observations if o.price_cents == 13900)
    assert seg_level.fare_brand is FareBrand.BASIC_ECONOMY
    assert seg_level.fare_brand_raw == "basic economy fare: no changes"


def test_no_brand_hint_and_no_filter_is_unknown():
    res = run_parse(load("serpapi_no_basic_filter_with_basic_extension.json"), allow_basic_wl())
    plain = next(o for o in res.observations if o.price_cents == 24900)
    assert (plain.fare_brand, plain.fare_brand_raw, plain.carry_on_included) == (
        FareBrand.UNKNOWN,
        None,
        None,
    )


def test_exclude_basic_in_payload_wins_over_extension_text():
    payload = copy.deepcopy(load("serpapi_no_basic_filter_with_basic_extension.json"))
    payload["search_parameters"]["exclude_basic"] = "true"
    res = run_parse(payload, allow_basic_wl())
    assert {o.fare_brand for o in res.observations} == {FareBrand.MAIN_CABIN}


# --------------------------------------------------------------------------- band


def test_band_values_are_scaled_to_cents(rt):
    b = rt.band
    assert (b.lowest_price_cents, b.typical_low_cents, b.typical_high_cents) == (
        17600,
        19000,
        26000,
    )
    assert b.provider is Provider.SERPAPI
    assert b.usable is True


def test_band_price_level_is_mapped(rt):
    assert rt.band.price_level.value == "typical"


@pytest.mark.parametrize("raw_level", ["low", "typical", "high"])
def test_band_price_level_round_trips(raw_level):
    payload = copy.deepcopy(load("serpapi_sea_sjc_rt_exclude_basic.json"))
    payload["price_insights"]["price_level"] = raw_level
    assert run_parse(payload).band.price_level.value == raw_level


def test_unrecognised_price_level_becomes_unknown():
    payload = copy.deepcopy(load("serpapi_sea_sjc_rt_exclude_basic.json"))
    payload["price_insights"]["price_level"] = "bonkers"
    assert run_parse(payload).band.price_level.value == "unknown"


def test_band_is_none_without_price_insights():
    res = run_parse(load("serpapi_no_basic_filter_with_basic_extension.json"), allow_basic_wl())
    assert res.band is None


# --------------------------------------------------------------------------- one-way, co-terminal


def test_one_way_has_one_slice_and_no_outbound_only_note():
    wl = sea_bos_oneway()
    res = run_parse(load("serpapi_sea_jfk_coterminal.json"), wl)
    assert all(len(o.itinerary.slices) == 1 for o in res.observations)
    assert res.raw.get("_note") != "outbound_only"
    assert [o.price_cents for o in res.observations] == [38900, 30500]
    assert res.observations[0].days_to_departure == (date(2027, 3, 12) - T0.date()).days


def test_coterminal_swap_itinerary_is_localized_and_flagged_by_the_model():
    res = run_parse(load("serpapi_sea_jfk_coterminal.json"), sea_bos_oneway())
    clean, swap = (o.itinerary.outbound for o in res.observations)
    assert clean.has_coterminal_swap is False
    assert swap.has_coterminal_swap is True
    assert [(s.origin, s.destination) for s in swap.segments] == [("SEA", "EWR"), ("JFK", "BOS")]
    assert swap.segments[0].arrival_local.utcoffset() == timedelta(hours=-5)


# --------------------------------------------------------------------------- zero results / errors


def test_zero_results_is_an_empty_list_not_an_error():
    res = run_parse(load("serpapi_zero_results.json"))
    assert res.observations == []


def test_zero_results_still_returns_the_band():
    res = run_parse(load("serpapi_zero_results.json"))
    assert (res.band.lowest_price_cents, res.band.typical_low_cents) == (20500, 19000)
    assert res.band.price_level.value == "high"


def test_empty_flight_lists_are_treated_like_absent_ones():
    payload = copy.deepcopy(load("serpapi_zero_results.json"))
    payload["best_flights"], payload["other_flights"] = [], []
    assert run_parse(payload).observations == []


def test_error_payload_raises_non_retryable_provider_error():
    with pytest.raises(ProviderError) as exc:
        run_parse(load("serpapi_error.json"))
    assert exc.value.provider is Provider.SERPAPI
    assert exc.value.retryable is False
    assert "Invalid API key" in str(exc.value)


def test_error_key_alone_raises_even_if_status_says_success():
    payload = copy.deepcopy(load("serpapi_zero_results.json"))
    payload["error"] = "Google hasn't returned any results for this query."
    with pytest.raises(ProviderError) as exc:
        run_parse(payload)
    assert exc.value.retryable is False


def test_non_success_status_without_error_key_raises():
    payload = copy.deepcopy(load("serpapi_zero_results.json"))
    payload["search_metadata"]["status"] = "Processing"
    with pytest.raises(ProviderError):
        run_parse(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["best_flights"][0].__setitem__("price", "n/a"),
        lambda p: p["best_flights"][0]["flights"][0]["departure_airport"].__setitem__(
            "time", "soon"
        ),
        lambda p: p["best_flights"][0].pop("flights"),
    ],
    ids=["price-not-a-number", "unparseable-time", "missing-flights"],
)
def test_malformed_itinerary_raises_non_retryable_provider_error(mutate):
    """ARCHITECTURE section 5: any parse/validation failure is ProviderError, never a raw exception."""
    payload = copy.deepcopy(load("serpapi_sea_sjc_rt_exclude_basic.json"))
    mutate(payload)
    with pytest.raises(ProviderError) as exc:
        run_parse(payload)
    assert exc.value.retryable is False


# --------------------------------------------------------------------------- unknown airport


def test_unknown_airport_itinerary_is_skipped_and_counted():
    res = run_parse(load("serpapi_unknown_airport.json"))
    assert [o.itinerary.outbound.segments[0].flight_number for o in res.observations] == [
        "327",
        "2540",
    ]
    assert res.raw["_skipped_unknown_airport"] == 1


def test_unknown_airport_skip_keeps_the_band_and_other_prices():
    res = run_parse(load("serpapi_unknown_airport.json"))
    assert [o.price_cents for o in res.observations] == [21400, 24100]
    assert res.band is not None


def test_nothing_skipped_means_zero_or_absent_counter(rt):
    assert rt.raw.get("_skipped_unknown_airport", 0) == 0


def test_all_itineraries_unknown_airport_is_empty_not_an_error():
    payload = copy.deepcopy(load("serpapi_unknown_airport.json"))
    payload["best_flights"] = payload["best_flights"][1:]
    payload["other_flights"] = []
    res = run_parse(payload)
    assert res.observations == []
    assert res.raw["_skipped_unknown_airport"] == 1
