"""src/ingestion/airports.py: IATA -> tz and naive-local -> aware (phase-4 brief)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.ingestion.airports import UnknownAirportError, localize, tz_for


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
