"""SerpAPI Google Flights provider (ARCHITECTURE.md section 5).

One HTTP call per `search()`, never retried here: the runner owns retries, the ApiCall ledger and
the circuit breaker (section 6). Every failure surfaces as `ProviderError`; a raw exception after a
paid call would leave the ledger row `ok` and crash the run.

Round trips: the response holds OUTBOUND legs only (return legs cost a second call through
`departure_token`, which we do not spend). A round-trip observation therefore has a one-slice
itinerary and a round-trip total price, and `raw["_note"] == "outbound_only"`.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any

import httpx
from pydantic import ValidationError

from src.ingestion.airports import UnknownAirportError, is_us, localize
from src.ingestion.base import ProviderError, ProviderResult
from src.models import (
    BenchmarkBand,
    Cabin,
    FareBrand,
    FareObservation,
    Itinerary,
    PriceLevel,
    Provider,
    ResolvedWatchlist,
    SearchTarget,
    Segment,
    Slice,
)

_TRAVEL_CLASS = {
    Cabin.ECONOMY: "1",
    Cabin.PREMIUM_ECONOMY: "2",
    Cabin.BUSINESS: "3",
    Cabin.FIRST: "4",
}
_STOPS = {0: "1", 1: "2", 2: "3"}  # max_stops 3 -> omit (any number of stops)
_TIME_FORMAT = "%Y-%m-%d %H:%M"
_EXCLUDE_BASIC_RAW = "google:exclude_basic"
_API_KEY_IN_TEXT = re.compile(r"(api_key=)[^&\s\"']+", re.IGNORECASE)
_MAX_MESSAGE = 300
_NO_RESULTS_ERROR = "hasn't returned any results"

#: Anything that can go wrong while reading a payload we do not control.
_PARSE_ERRORS = (KeyError, IndexError, TypeError, ValueError, AttributeError, ArithmeticError)


class _BadFlightNumberError(Exception):
    """flight_number has no carrier token ("1234"); the itinerary cannot be identified."""


def _redact_full(text: str, api_key: str | None = None) -> str:
    """Remove the key (and any `api_key=...` query fragment) from a string, untruncated."""
    text = _API_KEY_IN_TEXT.sub(r"\1REDACTED", text)
    if api_key:
        text = text.replace(api_key, "REDACTED")
    return text


def _redact(text: str, api_key: str | None = None) -> str:
    """`_redact_full`, truncated: for text we are about to put in an exception message."""
    return _redact_full(text, api_key)[:_MAX_MESSAGE]


def _error(
    message: str, *, status_code: int | None = None, retryable: bool = False
) -> ProviderError:
    return ProviderError(Provider.SERPAPI, message, status_code=status_code, retryable=retryable)


# --------------------------------------------------------------------------- request


def _times_param(earliest_departure: time, latest_arrival: time) -> str:
    """`dep_from,dep_to,arr_from,arr_to` in whole hours, rounded inward (never admit a fare the
    local risk filter would reject): the departure window opens at the next whole hour, the
    arrival window closes at the hour of `latest_arrival_local`."""
    dep_from = min(earliest_departure.hour + (1 if earliest_departure.minute else 0), 23)
    return f"{dep_from},23,0,{latest_arrival.hour}"


def build_params(
    target: SearchTarget, watchlist: ResolvedWatchlist, api_key: str
) -> dict[str, str]:
    c = watchlist.constraints
    params: dict[str, str] = {
        "engine": "google_flights",
        "api_key": api_key,
        "departure_id": target.origin,
        "arrival_id": target.destination,
        "outbound_date": target.depart.isoformat(),
    }
    if target.return_ is not None:
        params["return_date"] = target.return_.isoformat()
        params["type"] = "1"
    else:
        params["type"] = "2"
    params["travel_class"] = _TRAVEL_CLASS[watchlist.cabin]
    params["adults"] = str(watchlist.passengers)
    params["currency"] = watchlist.currency
    params["hl"] = "en"
    params["gl"] = "us"
    if c.max_stops in _STOPS:
        params["stops"] = _STOPS[c.max_stops]
    if c.max_stops > 0:
        params["layover_duration"] = f"30,{c.max_layover_minutes}"
    times = _times_param(c.earliest_departure_local, c.latest_arrival_local)
    params["outbound_times"] = times
    if target.return_ is not None:
        params["return_times"] = times
    if not c.allow_basic_economy and watchlist.cabin is Cabin.ECONOMY:
        params["exclude_basic"] = "true"
    if c.allowed_carriers:
        params["include_airlines"] = ",".join(c.allowed_carriers)
    elif c.excluded_carriers:
        params["exclude_airlines"] = ",".join(c.excluded_carriers)
    return params


# --------------------------------------------------------------------------- response


def _cents(value: Any) -> int:
    """Whole currency units (or a float with cents) to integer cents."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"price is not a number: {value!r}")
    return value * 100 if isinstance(value, int) else round(value * 100)


def _segment(flight: dict[str, Any]) -> Segment:
    parts = str(flight["flight_number"]).split()
    if len(parts) < 2:
        raise _BadFlightNumberError(str(flight["flight_number"]))
    dep, arr = flight["departure_airport"], flight["arrival_airport"]
    return Segment(
        carrier=parts[0],
        flight_number=parts[1],
        origin=dep["id"],
        destination=arr["id"],
        departure_local=localize(datetime.strptime(dep["time"], _TIME_FORMAT), dep["id"]),
        arrival_local=localize(datetime.strptime(arr["time"], _TIME_FORMAT), arr["id"]),
        aircraft=flight.get("airplane"),
    )


def _basic_extension(entry: dict[str, Any]) -> str | None:
    texts = list(entry.get("extensions") or [])
    for flight in entry["flights"]:
        texts.extend(flight.get("extensions") or [])
    return next((t for t in texts if "basic" in str(t).lower()), None)


def _band(
    insights: dict[str, Any] | None,
    *,
    target: SearchTarget,
    run_id: str,
    observed_at: datetime,
    currency: str,
) -> BenchmarkBand | None:
    if not insights:
        return None
    low_high = insights.get("typical_price_range") or [None, None]
    try:
        level = PriceLevel(insights.get("price_level"))
    except ValueError:
        level = PriceLevel.UNKNOWN

    def cents(v: Any) -> int | None:
        return None if v is None else _cents(v)

    return BenchmarkBand(
        watchlist_id=target.watchlist_id,
        target_id=target.target_id,
        observed_at=observed_at,
        provider=Provider.SERPAPI,
        run_id=run_id,
        lowest_price_cents=cents(insights.get("lowest_price")),
        typical_low_cents=cents(low_high[0]),
        typical_high_cents=cents(low_high[1]),
        price_level=level,
        currency=currency,
    )


def _scrub(value: Any, api_key: str | None) -> Any:
    """Copy of `value` with every `api_key=...` fragment and the literal key removed from strings."""
    if isinstance(value, str):
        return _redact_full(value, api_key)
    if isinstance(value, dict):
        return {k: _scrub(v, api_key) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, api_key) for v in value]
    return value


def _is_empty_result(payload: dict[str, Any]) -> bool:
    """The live "no flights" shape: HTTP 200, status Success, no flight lists, and an `error`
    string saying Google returned nothing. Anything else carrying `error` is a real failure."""
    error = payload.get("error")
    return (
        isinstance(error, str)
        and _NO_RESULTS_ERROR in error.lower()
        and not (payload.get("best_flights") or payload.get("other_flights"))
    )


def parse(
    payload: dict[str, Any],
    *,
    target: SearchTarget,
    watchlist: ResolvedWatchlist,
    run_id: str,
    observed_at: datetime,
    api_key: str | None = None,
) -> ProviderResult:
    if not isinstance(payload, dict) or not isinstance(payload.get("search_metadata"), dict):
        raise _error("unparseable SerpAPI payload: missing search_metadata")
    status = payload["search_metadata"].get("status")
    if status != "Success" or (payload.get("error") and not _is_empty_result(payload)):
        detail = payload.get("error") or f"status {status!r}"
        raise _error(f"SerpAPI error: {_redact(str(detail), api_key)}")

    try:
        return _parse(
            payload,
            target=target,
            watchlist=watchlist,
            run_id=run_id,
            observed_at=observed_at,
            api_key=api_key,
        )
    except ProviderError:
        raise
    except _PARSE_ERRORS as exc:  # includes pydantic.ValidationError (a ValueError)
        raise _error(
            f"unparseable SerpAPI payload: {type(exc).__name__}: {_redact(str(exc))}"
        ) from None


def _parse(
    payload: dict[str, Any],
    *,
    target: SearchTarget,
    watchlist: ResolvedWatchlist,
    run_id: str,
    observed_at: datetime,
    api_key: str | None,
) -> ProviderResult:
    params = payload.get("search_parameters") or {}
    currency = params.get("currency") or watchlist.currency
    # "Was exclude_basic sent" is read from the payload echo only, never inferred from the watchlist.
    exclude_basic_sent = params.get("exclude_basic") == "true"
    url = (payload.get("search_metadata") or {}).get("google_flights_url")
    dtd = target.days_to_departure(observed_at)
    # The echo only vouches for a non-Basic fare on US domestic routes (Google applies the filter
    # to US carriers' Basic fares; on international itineraries it is not evidence of anything).
    echo_is_evidence = exclude_basic_sent and is_us(target.origin) and is_us(target.destination)

    skipped_airport = 0
    skipped_flight_number = 0
    skipped_invalid = 0
    observations: list[FareObservation] = []

    for entry in [*(payload.get("best_flights") or []), *(payload.get("other_flights") or [])]:
        price_cents = _cents(entry["price"])
        # Evidence of Basic always wins; the echo only counts where it is evidence (US domestic).
        if (hint := _basic_extension(entry)) is not None:
            brand, raw_brand, carry_on = FareBrand.BASIC_ECONOMY, hint, None
        elif echo_is_evidence:
            brand, raw_brand, carry_on = FareBrand.MAIN_CABIN, _EXCLUDE_BASIC_RAW, True
        else:
            brand, raw_brand, carry_on = FareBrand.UNKNOWN, None, None

        try:
            segments = [_segment(f) for f in entry["flights"]]
            observation = FareObservation(
                watchlist_id=target.watchlist_id,
                target_id=target.target_id,
                observed_at=observed_at,
                provider=Provider.SERPAPI,
                run_id=run_id,
                itinerary=Itinerary(slices=[Slice(segments=segments)]),
                price_cents=price_cents,
                currency=currency,
                fare_brand=brand,
                fare_brand_raw=raw_brand,
                carry_on_included=carry_on,
                days_to_departure=dtd,
                booking_url=url,
            )
        except UnknownAirportError:
            skipped_airport += 1
            continue
        except _BadFlightNumberError:
            skipped_flight_number += 1
            continue
        except ValidationError:  # one bad itinerary (e.g. 5-digit flight number) is not fatal
            skipped_invalid += 1
            continue
        observations.append(observation)

    raw: dict[str, Any] = _scrub(payload, api_key)
    if target.return_ is not None:
        raw["_note"] = "outbound_only"
    if skipped_airport:
        raw["_skipped_unknown_airport"] = skipped_airport
    if skipped_flight_number:
        raw["_skipped_bad_flight_number"] = skipped_flight_number
    if skipped_invalid:
        raw["_skipped_invalid"] = skipped_invalid

    band = _band(
        payload.get("price_insights"),
        target=target,
        run_id=run_id,
        observed_at=observed_at,
        currency=currency,
    )
    return ProviderResult(observations=observations, band=band, raw=raw)


# --------------------------------------------------------------------------- provider


class SerpApiProvider:
    name = Provider.SERPAPI

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        base_url: str = "https://serpapi.com",
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=30)
        self._base_url = base_url.rstrip("/")

    def search(
        self,
        target: SearchTarget,
        watchlist: ResolvedWatchlist,
        run_id: str,
        observed_at: datetime,
    ) -> ProviderResult:
        params = build_params(target, watchlist, self._api_key)
        try:
            response = self._client.get(f"{self._base_url}/search", params=params)
        except httpx.TimeoutException as exc:
            # `from None`: the request URL (which carries the key) must not ride along in a traceback.
            raise _error(f"SerpAPI timed out ({type(exc).__name__})", retryable=True) from None
        except httpx.HTTPError as exc:
            raise _error(f"SerpAPI transport failure ({type(exc).__name__})") from None

        if response.status_code != 200:
            code = response.status_code
            raise _error(
                f"SerpAPI HTTP {code}{self._body_error(response)}",
                status_code=code,
                retryable=code == 429 or 500 <= code < 600,
            )

        try:
            body = response.json()
        except ValueError:
            raise _error("SerpAPI returned a non-JSON 200 body", status_code=200) from None
        if not isinstance(body, dict):
            raise _error("SerpAPI returned a JSON body that is not an object", status_code=200)

        try:
            return parse(
                body,
                target=target,
                watchlist=watchlist,
                run_id=run_id,
                observed_at=observed_at,
                api_key=self._api_key,
            )
        except ProviderError as exc:
            raise _error(
                _redact(str(exc), self._api_key),
                status_code=exc.status_code,
                retryable=exc.retryable,
            ) from None

    def _body_error(self, response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        err = body.get("error") if isinstance(body, dict) else None
        return f": {_redact(str(err), self._api_key)}" if err else ""
