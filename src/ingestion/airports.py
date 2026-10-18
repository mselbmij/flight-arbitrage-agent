"""IATA airport → IANA timezone, for providers that return local wall-clock times.

Both SerpAPI (Google Flights) and Amadeus give segment times as local time with no
offset ("2026-11-25 07:15", "2026-11-25T07:15:00"). `Segment` requires tz-aware
datetimes, so every provider localizes through here. Data comes from the
`airportsdata` package (OurAirports-derived, ~7.9k IATA codes).
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import airportsdata


class UnknownAirportError(LookupError):
    """No timezone on record for this IATA code; the itinerary cannot be localized."""


@lru_cache(maxsize=1)
def _table() -> dict[str, dict]:
    return airportsdata.load("IATA")


@lru_cache(maxsize=2048)
def tz_for(iata: str) -> ZoneInfo:
    row = _table().get(iata.upper())
    if row is None or not row.get("tz"):
        raise UnknownAirportError(iata)
    return ZoneInfo(row["tz"])


def localize(naive: datetime, iata: str) -> datetime:
    """Attach the airport's zone to a naive local datetime. Raises UnknownAirportError."""
    if naive.tzinfo is not None:
        raise ValueError("localize() expects a naive local datetime")
    return naive.replace(tzinfo=tz_for(iata))
