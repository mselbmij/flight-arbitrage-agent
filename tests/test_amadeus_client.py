"""Amadeus flight-offers: `map_brand` and pure `parse` (phase-4 brief).

Request building, OAuth and HTTP behaviour live in test_amadeus_transport.py.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from src.ingestion.amadeus_client import map_brand, parse
from src.ingestion.base import ProviderError
from src.models import (
    FareBrand,
    Provider,
    ResolvedWatchlist,
    Watchlist,
    WatchlistFile,
)
from tests.conftest import T0, WL, thanksgiving

FIX = Path(__file__).parent / "fixtures" / "amadeus"
RUN = "run-amd-1"
B = FareBrand


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def syd_bkk() -> ResolvedWatchlist:
    w = Watchlist(id="syd-bkk-test", origin="SYD", destination="BKK", depart=date(2021, 11, 1),
                  ceiling_cents=50000, currency="EUR")  # fmt: skip
    return WatchlistFile(version=1, watchlists=[w]).resolved()[0]


def run_parse(payload, wl=None, observed_at=T0, provider=Provider.MOCK):
    wl = wl or thanksgiving()
    return parse(payload, target=wl.targets[0], watchlist=wl, run_id=RUN, observed_at=observed_at,
                 provider=provider)  # fmt: skip


# --------------------------------------------------------------------------- map_brand


@pytest.mark.parametrize(
    ("cabin", "fare", "label", "expected"),
    [
        # BASIC wins over everything, from either field, any case
        ("ECONOMY", "BASIC", "BASIC ECONOMY", (B.BASIC_ECONOMY, "BASIC ECONOMY")),
        ("ECONOMY", "BASIC", None, (B.BASIC_ECONOMY, "BASIC")),
        ("ECONOMY", None, "Basic Economy", (B.BASIC_ECONOMY, "Basic Economy")),
        ("ECONOMY", "basic", None, (B.BASIC_ECONOMY, "basic")),
        ("ECONOMY", "STANDARD", "Basic Fare", (B.BASIC_ECONOMY, "Basic Fare")),
        ("ECONOMY", "BASIC", "Premium Basic", (B.BASIC_ECONOMY, "Premium Basic")),
        ("ECONOMY", "BASIC", "Main Cabin Basic", (B.BASIC_ECONOMY, "Main Cabin Basic")),
        # PREMIUM, only in an economy-family cabin
        ("ECONOMY", "PREMIUM", "Premium Economy", (B.PREMIUM_ECONOMY, "Premium Economy")),
        ("PREMIUM_ECONOMY", "PREMIUM", None, (B.PREMIUM_ECONOMY, "PREMIUM")),
        ("ECONOMY", None, "premium select", (B.PREMIUM_ECONOMY, "premium select")),
        ("ECONOMY", "PREMIUM", "Premium Comfort", (B.PREMIUM_ECONOMY, "Premium Comfort")),
        ("BUSINESS", "PREMIUM", "Business Premium", (B.BUSINESS, "Business Premium")),
        # MAIN_PLUS keywords
        ("ECONOMY", "COMFORTPLUS", "COMFORT+", (B.MAIN_PLUS, "COMFORT+")),
        ("ECONOMY", "COMFORT", None, (B.MAIN_PLUS, "COMFORT")),
        ("ECONOMY", "MAINPLUS", "Main Plus", (B.MAIN_PLUS, "Main Plus")),
        ("ECONOMY", "PREFERRED", None, (B.MAIN_PLUS, "PREFERRED")),
        ("ECONOMY", "EXTRA", None, (B.MAIN_PLUS, "EXTRA")),
        ("ECONOMY", "FLEX", None, (B.MAIN_PLUS, "FLEX")),
        ("ECONOMY", "MAIN", "Main Cabin Extra", (B.MAIN_PLUS, "Main Cabin Extra")),
        # MAIN_CABIN keywords
        ("ECONOMY", "MAIN", "Main Cabin", (B.MAIN_CABIN, "Main Cabin")),
        ("ECONOMY", "STANDARD", None, (B.MAIN_CABIN, "STANDARD")),
        ("ECONOMY", "CLASSIC", None, (B.MAIN_CABIN, "CLASSIC")),
        ("ECONOMY", "ECO", None, (B.MAIN_CABIN, "ECO")),
        ("ECONOMY", None, "Eco Fare", (B.MAIN_CABIN, "Eco Fare")),
        # ECO is a whole word only: "ECONOMY" / "ECOLOGY" are not the ECO brand
        ("ECONOMY", None, "Ecology Fare", (B.UNKNOWN, "Ecology Fare")),
        # a label that is exactly the cabin name is not brand evidence -> cabin fallback
        ("ECONOMY", None, "Economy", (B.UNKNOWN, "Economy")),
        ("ECONOMY", "ECONOMY", None, (B.UNKNOWN, "ECONOMY")),
        ("PREMIUM_ECONOMY", None, "Premium Economy", (B.PREMIUM_ECONOMY, "Premium Economy")),
        ("BUSINESS", None, "Business", (B.BUSINESS, "Business")),
        ("FIRST", None, "First", (B.FIRST, "First")),
        # LIGHT is Basic Economy on any carrier (and not a MAIN_CABIN word)
        ("ECONOMY", "LIGHT", None, (B.BASIC_ECONOMY, "LIGHT")),
        ("ECONOMY", None, "Economy Light", (B.BASIC_ECONOMY, "Economy Light")),
        # SAVER with no carrier is unknown, never main cabin
        ("ECONOMY", "SAVER", None, (B.UNKNOWN, "SAVER")),
        (None, "MAIN", None, (B.MAIN_CABIN, "MAIN")),
        # label is preferred as the raw string; a keyword in either field still maps
        ("ECONOMY", "MAIN", "Main Cabin", (B.MAIN_CABIN, "Main Cabin")),
        ("ECONOMY", "SAVER", "Fare of the day", (B.UNKNOWN, "Fare of the day")),
        # in a BUSINESS/FIRST cabin the cabin outranks every economy-family keyword
        ("BUSINESS", "MAIN", None, (B.BUSINESS, "MAIN")),
        ("BUSINESS", "SAVER", "Business Saver", (B.BUSINESS, "Business Saver")),
        ("BUSINESS", "FLEX", "Business Flex", (B.BUSINESS, "Business Flex")),
        ("FIRST", "LIGHT", None, (B.FIRST, "LIGHT")),
        # ...but a Basic label still wins
        ("BUSINESS", "BASIC", None, (B.BASIC_ECONOMY, "BASIC")),
        # cabin fallback when no keyword matched (raw string kept for mapping fixes)
        ("BUSINESS", None, None, (B.BUSINESS, None)),
        ("FIRST", None, None, (B.FIRST, None)),
        ("BUSINESS", "ODDBALL", None, (B.BUSINESS, "ODDBALL")),
        ("FIRST", None, "Odd Label", (B.FIRST, "Odd Label")),
        # nothing to go on
        ("ECONOMY", None, None, (B.UNKNOWN, None)),
        (None, None, None, (B.UNKNOWN, None)),
        ("ECONOMY", "ODDBALL", None, (B.UNKNOWN, "ODDBALL")),
        ("ECONOMY", None, "Odd Label", (B.UNKNOWN, "Odd Label")),
    ],
)
def test_map_brand(cabin, fare, label, expected):
    assert map_brand(cabin, fare, label) == expected


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        ("AS", B.BASIC_ECONOMY),  # Alaska Saver is its Basic Economy product
        ("as", B.BASIC_ECONOMY),
        ("DL", B.UNKNOWN),
        ("UA", B.UNKNOWN),
        (None, B.UNKNOWN),
    ],
)
def test_map_brand_saver_is_basic_only_on_alaska(carrier, expected):
    assert map_brand("ECONOMY", "SAVER", "Saver", carrier=carrier) == (expected, "Saver")


@pytest.mark.parametrize("carrier", ["AS", "DL", "LH", None])
def test_map_brand_light_is_basic_on_every_carrier(carrier):
    assert map_brand("ECONOMY", "LIGHT", None, carrier=carrier) == (B.BASIC_ECONOMY, "LIGHT")


def test_map_brand_saver_with_a_main_keyword_is_still_unknown_off_alaska():
    assert map_brand("ECONOMY", "MAIN", "Main Saver", carrier="DL") == (B.UNKNOWN, "Main Saver")


# --------------------------------------------------------------------------- spec example


@pytest.fixture(scope="module")
def spec():
    return run_parse(
        load("spec_example_syd_bkk.json"),
        syd_bkk(),
        observed_at=datetime(2021, 10, 1, 12, 0, tzinfo=UTC),
        provider=Provider.AMADEUS,
    )


def test_spec_example_has_two_offers_each_a_single_two_segment_slice(spec):
    assert len(spec.observations) == 2
    for o in spec.observations:
        assert len(o.itinerary.slices) == 1
        assert [s.origin for s in o.itinerary.outbound.segments] == ["SYD", "MNL"]
        assert [s.destination for s in o.itinerary.outbound.segments] == ["MNL", "BKK"]


def test_price_is_decimal_exact_cents_in_the_offer_currency(spec):
    for o in spec.observations:
        assert o.price_cents == 35534 and type(o.price_cents) is int
        assert o.currency == "EUR"


def test_price_uses_grand_total_without_float_error():
    payload = copy.deepcopy(load("spec_example_syd_bkk.json"))
    payload["data"][0]["price"]["grandTotal"] = "256.03"  # float(256.03)*100 == 25602.999...
    payload["data"][0]["price"]["total"] = "1.00"
    assert run_parse(payload, syd_bkk()).observations[0].price_cents == 25603


def test_segments_carry_marketing_and_operating_carrier_and_numbers(spec):
    a, b = spec.observations
    assert [(s.carrier, s.operating_carrier) for s in a.itinerary.outbound.segments] == [
        ("PR", "PR"),
        ("PR", "PR"),
    ]
    assert [s.flight_number for s in a.itinerary.outbound.segments] == ["212", "732"]
    assert [s.flight_number for s in b.itinerary.outbound.segments] == ["212", "740"]


def test_aircraft_names_come_from_the_dictionary(spec):
    a, b = spec.observations
    assert [s.aircraft for s in a.itinerary.outbound.segments] == ["AIRBUS A330-300", "AIRBUS A320"]
    assert b.itinerary.outbound.segments[1].aircraft == "AIRBUS A321"


def test_segment_times_are_localized_with_each_airports_offset(spec):
    syd, mnl = spec.observations[0].itinerary.outbound.segments
    assert syd.departure_local.utcoffset() == timedelta(hours=11)  # Sydney DST in November
    assert mnl.departure_local.utcoffset() == timedelta(hours=8)
    assert (syd.departure_local.hour, syd.departure_local.minute) == (11, 35)
    assert syd.arrival_local.utcoffset() == timedelta(hours=8)  # lands in Manila
    assert mnl.arrival_local.utcoffset() == timedelta(hours=7)  # lands in Bangkok
    assert mnl.arrival_local.date() == date(2021, 11, 1)


def test_arrival_after_midnight_keeps_its_own_date(spec):
    last = spec.observations[1].itinerary.outbound.segments[-1]
    assert (last.arrival_local.date(), last.arrival_local.hour) == (date(2021, 11, 2), 0)


def test_no_brand_fields_means_unknown_brand_and_no_carry_on_info(spec):
    for o in spec.observations:
        assert (o.fare_brand, o.fare_brand_raw, o.carry_on_included) == (B.UNKNOWN, None, None)


def test_seats_remaining_comes_from_bookable_seats(spec):
    assert [o.seats_remaining for o in spec.observations] == [9, 9]


def test_unprovided_fields_stay_none_and_risk_is_never_filled(spec):
    for o in spec.observations:
        assert (o.changeable, o.refundable, o.booking_url, o.risk) == (None, None, None, None)


def test_amadeus_has_no_band(spec):
    assert spec.band is None


def test_identity_and_dtd_come_from_the_call(spec):
    wl = syd_bkk()
    for o in spec.observations:
        assert (o.watchlist_id, o.target_id) == ("syd-bkk-test", wl.targets[0].target_id)
        assert (o.run_id, o.provider) == (RUN, Provider.AMADEUS)
        assert o.days_to_departure == 31


def test_raw_keeps_the_whole_payload(spec):
    payload = load("spec_example_syd_bkk.json")
    assert all(spec.raw[k] == v for k, v in payload.items())


def test_the_two_offers_have_distinct_itinerary_keys(spec):
    assert spec.observations[0].itinerary_key != spec.observations[1].itinerary_key


# --------------------------------------------------------------------------- branded fixture


@pytest.fixture(scope="module")
def branded():
    return run_parse(load("amadeus_sea_sjc_branded.json"), provider=Provider.AMADEUS)


def test_unknown_airport_offer_is_skipped_and_counted(branded):
    assert len(branded.observations) == 4
    assert branded.raw["_skipped_unknown_airport"] == 1
    flights = {s.flight_number for o in branded.observations for s in o.itinerary.outbound.segments}
    assert "100" not in flights


def test_all_four_brands_map(branded):
    assert [o.fare_brand for o in branded.observations] == [
        B.BASIC_ECONOMY, B.MAIN_CABIN, B.MAIN_PLUS, B.UNKNOWN,
    ]  # fmt: skip


def test_fare_brand_raw_prefers_the_label(branded):
    assert [o.fare_brand_raw for o in branded.observations] == [
        "BASIC ECONOMY", "Main Cabin", "COMFORT+", None,
    ]  # fmt: skip


def test_round_trip_offer_has_two_full_slices(branded):
    o = branded.observations[0]
    assert len(o.itinerary.slices) == 2
    assert (o.itinerary.inbound.origin, o.itinerary.inbound.destination) == ("SJC", "SEA")
    assert o.itinerary.inbound.departure_local.date() == date(2026, 11, 29)
    assert o.itinerary.outbound.departure_local.utcoffset() == timedelta(hours=-8)


def test_prices_are_decimal_exact_from_grand_total(branded):
    # offer 3 has total 250.00 but grandTotal 262.50; offer 4 is the float trap 256.03
    assert [o.price_cents for o in branded.observations] == [19840, 23107, 26250, 25603]


def test_carry_on_included_follows_included_cabin_bags(branded):
    assert [o.carry_on_included for o in branded.observations] == [False, True, True, None]


def test_seats_remaining_follows_bookable_seats(branded):
    assert [o.seats_remaining for o in branded.observations] == [4, 9, 7, None]


def test_changeable_is_true_only_for_a_non_chargeable_change_amenity(branded):
    # offer 1: chargeable change -> None; 2: free change -> True; 3: chargeable -> None;
    # 4: a non-chargeable amenity of another type that mentions CHANGE -> None
    assert [o.changeable for o in branded.observations] == [None, True, None, None]


def test_refundable_is_true_only_for_a_non_chargeable_refund_amenity(branded):
    assert [o.refundable for o in branded.observations] == [None, None, True, None]


def test_operating_carrier_and_dictionary_fallback_for_aircraft(branded):
    first = branded.observations[0].itinerary.outbound.segments[0]
    assert (first.carrier, first.operating_carrier, first.aircraft) == ("AS", "QX", "EMBRAER 175")
    unmapped = branded.observations[3].itinerary.outbound.segments[0]
    assert unmapped.aircraft == "7M9"  # code absent from dictionaries -> code itself


def test_branded_offers_have_no_band_and_no_booking_url(branded):
    assert branded.band is None
    assert all(o.booking_url is None and o.risk is None for o in branded.observations)


def test_branded_identity_comes_from_the_requested_target(branded):
    target = thanksgiving().targets[0]
    for o in branded.observations:
        assert (o.watchlist_id, o.target_id) == (WL, target.target_id)
        assert o.days_to_departure == (date(2026, 11, 25) - T0.date()).days


# --------------------------------------------------------------------------- mixed-brand offers


@pytest.fixture(scope="module")
def mixed():
    return run_parse(load("amadeus_mixed_brand.json"), provider=Provider.AMADEUS).observations


def test_mixed_brand_offers_all_parse(mixed):
    assert len(mixed) == 7


def test_a_basic_return_segment_makes_the_whole_offer_basic_economy(mixed):
    # segment 1 is Main Cabin; the verdict must not be segment 1's
    assert (mixed[0].fare_brand, mixed[0].fare_brand_raw) == (B.BASIC_ECONOMY, "BASIC ECONOMY")


def test_a_basic_segment_on_the_second_traveler_makes_the_offer_basic_economy(mixed):
    assert (mixed[3].fare_brand, mixed[3].fare_brand_raw) == (B.BASIC_ECONOMY, "BASIC ECONOMY")


def test_a_segment_with_no_brand_data_makes_the_offer_unknown(mixed):
    assert (mixed[1].fare_brand, mixed[1].fare_brand_raw) == (B.UNKNOWN, None)


def test_otherwise_the_lowest_tier_present_decides(mixed):
    # COMFORT+ outbound, Main Cabin return -> MAIN_CABIN, raw is the deciding (return) segment's
    assert (mixed[2].fare_brand, mixed[2].fare_brand_raw) == (B.MAIN_CABIN, "Main Cabin")


def test_saver_is_judged_per_segment_carrier(mixed):
    # AS outbound Saver is Basic Economy; the same label on DL on both legs is unknown
    assert (mixed[4].fare_brand, mixed[4].fare_brand_raw) == (B.BASIC_ECONOMY, "Saver")
    assert (mixed[5].fare_brand, mixed[5].fare_brand_raw) == (B.UNKNOWN, "Saver")


def test_carry_on_is_true_only_if_every_segment_includes_one(mixed):
    # 0: main(1 bag) + basic(0 bags) -> False; 1: 1 bag + no info -> None; 2: all bags -> True
    # 3: second traveler's basic leg has 0 bags -> False; 6: one segment silent -> None
    assert [o.carry_on_included for o in mixed] == [False, None, True, False, False, True, None]


def test_changeable_is_true_only_if_every_segment_is_changeable(mixed):
    # offer 1 mixes a free-change leg with a chargeable one; offers 3 and 7 are free-change throughout
    assert [o.changeable for o in mixed] == [None, None, True, None, None, None, True]


def test_refundable_needs_every_segment_to_be_refundable(mixed):
    assert [o.refundable for o in mixed] == [None] * 7


# --------------------------------------------------------------------------- provider stamp


def test_parse_stamps_the_provider_it_is_given():
    mock = run_parse(load("amadeus_sea_sjc_branded.json"), provider=Provider.MOCK)
    real = run_parse(load("amadeus_sea_sjc_branded.json"), provider=Provider.AMADEUS)
    assert {o.provider for o in mock.observations} == {Provider.MOCK}
    assert {o.provider for o in real.observations} == {Provider.AMADEUS}


# --------------------------------------------------------------------------- empty / errors


def test_empty_data_is_an_empty_list_not_an_error():
    res = run_parse({"meta": {"count": 0}, "data": [], "dictionaries": {}})
    assert res.observations == [] and res.band is None


def test_errors_payload_raises_non_retryable_provider_error():
    with pytest.raises(ProviderError) as exc:
        run_parse(load("amadeus_error_400.json"))
    assert exc.value.retryable is False
    assert "INVALID FORMAT" in str(exc.value) or "departureDate" in str(exc.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["data"][0]["price"].__setitem__("grandTotal", "free"),
        lambda p: p["data"][0].pop("itineraries"),
        lambda p: p["data"][0]["itineraries"][0]["segments"][0]["departure"].__setitem__(
            "at", "soon"
        ),
        lambda p: p["data"][0]["itineraries"][0]["segments"][0].pop("number"),
    ],
    ids=["price-not-a-number", "missing-itineraries", "unparseable-time", "missing-flight-number"],
)
def test_malformed_offer_raises_non_retryable_provider_error(mutate):
    """ARCHITECTURE section 5: parse/validation failures are ProviderError, never raw exceptions."""
    payload = copy.deepcopy(load("spec_example_syd_bkk.json"))
    mutate(payload)
    with pytest.raises(ProviderError) as exc:
        run_parse(payload, syd_bkk(), observed_at=datetime(2021, 10, 1, tzinfo=UTC))
    assert exc.value.retryable is False
