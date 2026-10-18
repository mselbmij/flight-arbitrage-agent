"""src/ingestion/airports.py: IATA -> tz and naive-local -> aware (phase-4 brief)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.ingestion import airports
from src.ingestion.airports import UnknownAirportError, is_us, localize, tz_for


def test_tz_for_sea_is_los_angeles():
    assert tz_for("SEA") == ZoneInfo("America/Los_Angeles")


def test_tz_for_accepts_lowercase_code():
    assert tz_for("sea") == ZoneInfo("America/Los_Angeles")


def test_tz_for_covers_non_us_airport():
    assert tz_for("SYD") == ZoneInfo("Australia/Sydney")


def test_tz_for_unknown_code_raises():
    with pytest.raises(UnknownAirportError):
        tz_for("ZZZ")


def test_unknown_airport_error_is_a_lookup_error():
    assert issubclass(UnknownAirportError, LookupError)


def test_localize_attaches_the_airport_zone():
    out = localize(datetime(2026, 11, 25, 7, 15), "SEA")
    assert out.tzinfo == ZoneInfo("America/Los_Angeles")
    assert (out.hour, out.minute) == (7, 15)  # wall clock untouched


def test_localize_november_is_standard_time():
    out = localize(datetime(2026, 11, 25, 7, 15), "SEA")
    assert out.utcoffset() == timedelta(hours=-8)


def test_localize_july_is_daylight_time():
    out = localize(datetime(2026, 7, 4, 7, 15), "SEA")
    assert out.utcoffset() == timedelta(hours=-7)


def test_localize_lowercase_code_works():
    assert localize(datetime(2026, 11, 25, 7, 15), "sea").utcoffset() == timedelta(hours=-8)


def test_localize_uses_the_given_airports_zone_not_a_default():
    sea = localize(datetime(2026, 11, 25, 12, 0), "SEA")
    jfk = localize(datetime(2026, 11, 25, 12, 0), "JFK")
    assert jfk.utcoffset() == timedelta(hours=-5)
    assert jfk.astimezone(UTC) < sea.astimezone(UTC)


def test_localize_unknown_code_raises():
    with pytest.raises(UnknownAirportError):
        localize(datetime(2026, 11, 25, 7, 15), "ZZZ")


def test_localize_rejects_aware_input():
    aware = datetime(2026, 11, 25, 7, 15, tzinfo=UTC)
    with pytest.raises(ValueError):
        localize(aware, "SEA")


def test_localize_rejects_aware_input_even_for_unknown_airport():
    """Caller bug (aware input) must not be masked as a data problem (unknown airport)."""
    aware = datetime(2026, 11, 25, 7, 15, tzinfo=UTC)
    with pytest.raises(ValueError):
        localize(aware, "ZZZ")


def test_tz_for_code_whose_tz_is_missing_on_this_host_is_unknown_airport(monkeypatch):
    """airportsdata lists some zones (Europe/Zaporozhye, Pacific/Enderbury) tzdata may not ship."""
    table = {"BOG": {"tz": "Mars/Olympus_Mons", "country": "US"}}
    monkeypatch.setattr(airports, "_table", lambda: table)
    airports.tz_for.cache_clear()
    try:
        with pytest.raises(UnknownAirportError):
            tz_for("BOG")
    finally:
        airports.tz_for.cache_clear()


def test_localize_with_a_missing_tz_raises_unknown_airport(monkeypatch):
    monkeypatch.setattr(airports, "_table", lambda: {"BOG": {"tz": "Mars/Olympus_Mons"}})
    airports.tz_for.cache_clear()
    try:
        with pytest.raises(UnknownAirportError):
            localize(datetime(2026, 11, 25, 7, 15), "BOG")
    finally:
        airports.tz_for.cache_clear()


@pytest.mark.parametrize("code", ["SEA", "sea", "JFK", "SJC", "HNL", "ANC"])
def test_is_us_true_for_us_airports(code):
    assert is_us(code) is True


@pytest.mark.parametrize("code", ["LHR", "YVR", "SYD", "MEX", "ZZZ", ""])
def test_is_us_false_for_foreign_and_unknown_codes(code):
    assert is_us(code) is False
