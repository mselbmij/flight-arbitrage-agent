"""Amadeus Self-Service flight-offers client (ARCHITECTURE.md section 5, phase-4 brief).

One `search` call is one `GET /v2/shopping/flight-offers`. The only re-send this client ever
makes is the 401 -> refresh token -> retry once, which is authentication; retries of provider
calls belong to the runner (ARCHITECTURE.md section 6).

The Amadeus *test* host returns fake prices, so everything it produces is stamped
`provider=mock`, never `amadeus` (section 5, risk matrix "Test-env prices"). That stamp also
applies to a `ProviderError` raised while talking to the test host.

Messages in `ProviderError` never contain the client secret or the bearer token.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

import httpx

from src.ingestion.airports import UnknownAirportError, localize
from src.ingestion.base import ProviderError, ProviderResult
from src.models import (
    Cabin,
    FareBrand,
    FareObservation,
    Itinerary,
    Provider,
    ResolvedWatchlist,
    SearchTarget,
    Segment,
    Slice,
)

HOSTS = {"test": "https://test.api.amadeus.com", "production": "https://api.amadeus.com"}
TOKEN_PATH = "/v1/security/oauth2/token"
SEARCH_PATH = "/v2/shopping/flight-offers"
TOKEN_REFRESH_MARGIN = timedelta(seconds=60)
MAX_OFFERS = 20

TRAVEL_CLASS = {
    Cabin.ECONOMY: "ECONOMY",
    Cabin.PREMIUM_ECONOMY: "PREMIUM_ECONOMY",
    Cabin.BUSINESS: "BUSINESS",
    Cabin.FIRST: "FIRST",
}

# Brand keyword tables, checked in this order (see `map_brand`). Extend as `fare_brand_raw`
# values show up in the DB.
_MAIN_PLUS_WORDS = ("COMFORT", "PLUS", "PREFERRED", "EXTRA", "FLEX")
_MAIN_CABIN_WORDS = ("MAIN", "STANDARD", "CLASSIC", "SAVER", "ECO", "LIGHT")
_ECONOMY_FAMILY = ("ECONOMY", "PREMIUM_ECONOMY")


def utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- pure helpers


def build_params(target: SearchTarget, watchlist: ResolvedWatchlist) -> dict[str, str]:
    c = watchlist.constraints
    params = {
        "originLocationCode": target.origin,
        "destinationLocationCode": target.destination,
        "departureDate": target.depart.isoformat(),
        "adults": str(watchlist.passengers),
        "travelClass": TRAVEL_CLASS[watchlist.cabin],
        "currencyCode": watchlist.currency,
        "max": str(MAX_OFFERS),
    }
    if target.return_ is not None:
        params["returnDate"] = target.return_.isoformat()
    if c.max_stops == 0:
        params["nonStop"] = "true"
    # Amadeus rejects both airline filters in one request; an allow-list wins.
    if c.allowed_carriers:
        params["includedAirlineCodes"] = ",".join(c.allowed_carriers)
    elif c.excluded_carriers:
        params["excludedAirlineCodes"] = ",".join(c.excluded_carriers)
    return params


def map_brand(
    cabin: str | None, branded_fare: str | None, branded_fare_label: str | None
) -> tuple[FareBrand, str | None]:
    """Map Amadeus brand fields onto `FareBrand`; returns (brand, fare_brand_raw).

    Precedence: BASIC beats everything; PREMIUM counts only in an economy-family cabin;
    MAIN_PLUS keywords beat MAIN_CABIN keywords; with no keyword the cabin decides
    (PREMIUM_ECONOMY / BUSINESS / FIRST); otherwise UNKNOWN. The raw string is the label,
    else the brand code, else None, and is kept even when it matched nothing.
    """
    raw = branded_fare_label or branded_fare or None
    text = f"{branded_fare or ''} {branded_fare_label or ''}".upper()
    cabin_u = (cabin or "").upper()

    if "BASIC" in text:
        return FareBrand.BASIC_ECONOMY, raw
    if "PREMIUM" in text and cabin_u in _ECONOMY_FAMILY:
        return FareBrand.PREMIUM_ECONOMY, raw
    if any(w in text for w in _MAIN_PLUS_WORDS):
        return FareBrand.MAIN_PLUS, raw
    if any(w in text for w in _MAIN_CABIN_WORDS):
        return FareBrand.MAIN_CABIN, raw
    fallback = {
        "PREMIUM_ECONOMY": FareBrand.PREMIUM_ECONOMY,
        "BUSINESS": FareBrand.BUSINESS,
        "FIRST": FareBrand.FIRST,
    }.get(cabin_u)
    if fallback is not None:
        return fallback, raw
    return FareBrand.UNKNOWN, raw


def _error_detail(payload: dict[str, Any]) -> str:
    parts = []
    for e in payload.get("errors") or []:
        if isinstance(e, dict):
            parts.append(
                f"{e.get('title', 'error')} ({e.get('code', '?')}): {e.get('detail', '')}".strip()
            )
    return "; ".join(parts) or "no detail"


def _amenity_is_free(details: dict[str, Any], word: str) -> bool | None:
    """True when a BRANDED_FARES amenity mentioning `word` is explicitly not chargeable."""
    for a in details.get("amenities") or []:
        if (
            a.get("amenityType") == "BRANDED_FARES"
            and word in str(a.get("description", "")).upper()
            and a.get("isChargeable") is False
        ):
            return True
    return None


def _segment(seg: dict[str, Any], aircraft_names: dict[str, str]) -> Segment:
    dep, arr = seg["departure"], seg["arrival"]
    code = (seg.get("aircraft") or {}).get("code")
    operating = (seg.get("operating") or {}).get("carrierCode")
    return Segment(
        carrier=seg["carrierCode"],
        operating_carrier=operating,
        flight_number=seg["number"],
        origin=dep["iataCode"],
        destination=arr["iataCode"],
        departure_local=localize(datetime.fromisoformat(dep["at"]), dep["iataCode"]),
        arrival_local=localize(datetime.fromisoformat(arr["at"]), arr["iataCode"]),
        aircraft=aircraft_names.get(code, code) if code else None,
    )


def _observation(
    offer: dict[str, Any],
    aircraft_names: dict[str, str],
    *,
    target: SearchTarget,
    watchlist: ResolvedWatchlist,
    run_id: str,
    observed_at: datetime,
    provider: Provider,
) -> FareObservation:
    itinerary = Itinerary(
        slices=[
            Slice(segments=[_segment(s, aircraft_names) for s in it["segments"]])
            for it in offer["itineraries"]
        ]
    )
    price = offer["price"]
    price_cents = int(Decimal(str(price["grandTotal"])) * 100)

    travelers = offer.get("travelerPricings") or []
    by_segment = (travelers[0].get("fareDetailsBySegment") or []) if travelers else []
    first = by_segment[0] if by_segment else {}
    brand, brand_raw = map_brand(
        first.get("cabin"), first.get("brandedFare"), first.get("brandedFareLabel")
    )
    cabin_bags = first.get("includedCabinBags")
    carry_on = (
        cabin_bags["quantity"] > 0
        if isinstance(cabin_bags, dict) and cabin_bags.get("quantity") is not None
        else None
    )

    return FareObservation(
        watchlist_id=target.watchlist_id,
        target_id=target.target_id,
        observed_at=observed_at,
        provider=provider,
        run_id=run_id,
        itinerary=itinerary,
        price_cents=price_cents,
        currency=price["currency"],
        fare_brand=brand,
        fare_brand_raw=brand_raw,
        changeable=_amenity_is_free(first, "CHANGE"),
        refundable=_amenity_is_free(first, "REFUND"),
        carry_on_included=carry_on,
        seats_remaining=offer.get("numberOfBookableSeats"),
        days_to_departure=target.days_to_departure(observed_at),
        booking_url=None,
        risk=None,
    )


def parse(
    payload: dict[str, Any],
    *,
    target: SearchTarget,
    watchlist: ResolvedWatchlist,
    run_id: str,
    observed_at: datetime,
    provider: Provider,
) -> ProviderResult:
    """Turn a flight-offers payload into observations. Raises `ProviderError` (never retryable)
    for an `errors` body or any offer that does not validate; an unknown airport skips that
    offer only and is counted in `raw["_skipped_unknown_airport"]`."""
    if not isinstance(payload, dict):
        raise ProviderError(provider, "amadeus: response is not a JSON object", retryable=False)
    if payload.get("errors"):
        raise ProviderError(
            provider, f"amadeus: request rejected: {_error_detail(payload)}", retryable=False
        )
    offers = payload.get("data")
    if not isinstance(offers, list):
        raise ProviderError(provider, "amadeus: response has no 'data' list", retryable=False)

    aircraft_names = (payload.get("dictionaries") or {}).get("aircraft") or {}
    observations: list[FareObservation] = []
    skipped = 0
    for i, offer in enumerate(offers):
        try:
            observations.append(
                _observation(
                    offer,
                    aircraft_names,
                    target=target,
                    watchlist=watchlist,
                    run_id=run_id,
                    observed_at=observed_at,
                    provider=provider,
                )
            )
        except UnknownAirportError:
            skipped += 1
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, ArithmeticError) as e:
            # ValueError covers pydantic ValidationError and unparseable times;
            # ArithmeticError covers decimal.InvalidOperation and Infinity prices.
            raise ProviderError(
                provider,
                f"amadeus: malformed offer at data[{i}]: {type(e).__name__}",
                retryable=False,
            ) from e

    return ProviderResult(
        observations=observations,
        band=None,  # Amadeus has no typical-price band
        raw={**payload, "_skipped_unknown_airport": skipped},
    )


# --------------------------------------------------------------------------- provider


class AmadeusProvider:
    name = Provider.AMADEUS

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        host: Literal["test", "production"] = "test",
        client: httpx.Client | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        if host not in HOSTS:
            raise ValueError(f"host must be 'test' or 'production', not {host!r}")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base = HOSTS[host]
        # Test-host prices are fake: never let them be stored as `amadeus`.
        self._stamp = Provider.MOCK if host == "test" else Provider.AMADEUS
        self._http = client if client is not None else httpx.Client(timeout=30)
        self._now = now
        self._token: str | None = None
        self._token_expires_at: datetime | None = None

    # -- errors

    def _fail(
        self, message: str, *, status_code: int | None = None, retryable: bool = False
    ) -> ProviderError:
        return ProviderError(self._stamp, message, status_code=status_code, retryable=retryable)

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return status == 429 or status >= 500

    # -- auth

    def _fetch_token(self) -> str:
        try:
            resp = self._http.post(
                f"{self._base}{TOKEN_PATH}",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
            )
        except httpx.TransportError as e:
            raise self._fail(
                f"amadeus token request failed: {type(e).__name__}", retryable=True
            ) from e
        if resp.status_code != 200:
            raise self._fail(
                f"amadeus token request failed: HTTP {resp.status_code}",
                status_code=resp.status_code,
                retryable=self._is_retryable_status(resp.status_code),
            )
        try:
            body = resp.json()
            token = body["access_token"]
            expires_in = int(body["expires_in"])
        except (ValueError, KeyError, TypeError) as e:
            raise self._fail("amadeus token response is malformed") from e
        if not isinstance(token, str) or not token:
            raise self._fail("amadeus token response is malformed")
        self._token = token
        self._token_expires_at = self._now() + timedelta(seconds=expires_in)
        return token

    def _bearer(self) -> str:
        if (
            self._token is None
            or self._token_expires_at is None
            or self._now() >= self._token_expires_at - TOKEN_REFRESH_MARGIN
        ):
            return self._fetch_token()
        return self._token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires_at = None

    # -- search

    def _get(self, params: dict[str, str]) -> httpx.Response:
        try:
            return self._http.get(
                f"{self._base}{SEARCH_PATH}",
                params=params,
                headers={"Authorization": f"Bearer {self._bearer()}"},
            )
        except httpx.TimeoutException as e:
            raise self._fail("amadeus search timed out", retryable=True) from e
        except httpx.TransportError as e:
            raise self._fail(f"amadeus search failed: {type(e).__name__}", retryable=True) from e

    def search(
        self,
        target: SearchTarget,
        watchlist: ResolvedWatchlist,
        run_id: str,
        observed_at: datetime,
    ) -> ProviderResult:
        params = build_params(target, watchlist)
        resp = self._get(params)
        if resp.status_code == 401:
            # Authentication, not a provider retry: refresh the token and send once more.
            self._invalidate_token()
            resp = self._get(params)

        status = resp.status_code
        if status != 200:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("errors"):
                    detail = f": {_error_detail(body)}"
            except ValueError:
                pass
            raise self._fail(
                f"amadeus search failed: HTTP {status}{detail}",
                status_code=status,
                retryable=self._is_retryable_status(status),
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise self._fail("amadeus search returned a non-JSON body") from e

        return parse(
            payload,
            target=target,
            watchlist=watchlist,
            run_id=run_id,
            observed_at=observed_at,
            provider=self._stamp,
        )
