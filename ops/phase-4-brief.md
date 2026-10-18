# Phase 4 brief — ingestion clients + risk filters

Governing: ARCHITECTURE.md §4 (risk filters), §5 (providers), §6 (breaker; clients never retry),
§10 risk matrix rows "Basic Economy", "Unknown brand", "Phantom fare", "Test-env prices".
Phase 2 brief defines `FlightProvider`, `ProviderResult`, `ProviderError`.

Budget context: SerpAPI **free tier**, 3 calls/day. Every design choice below optimizes for
*one call per target*. Amadeus is a second source with its own free production quota; it is
off in the shipped config and shares the single breaker when enabled (one call = one unit).

## Deliverables

| File | Owner | Tests |
|---|---|---|
| `src/ingestion/airports.py` | done (orchestrator) | `tests/test_airports.py` |
| `src/engine/risk_filters.py` | BUILDER-R | `tests/test_risk_filters.py` |
| `src/ingestion/serpapi_client.py` | BUILDER-S | `tests/test_serpapi_client.py` + `tests/fixtures/serpapi/*.json` |
| `src/ingestion/amadeus_client.py` | BUILDER-A | `tests/test_amadeus_client.py` + `tests/fixtures/amadeus/*.json` |

No changes to `src/models.py`. No changes to `src/db.py`. No runner yet (Phase 5).

## src/ingestion/airports.py (exists)

```python
class UnknownAirportError(LookupError)
def tz_for(iata: str) -> ZoneInfo          # raises UnknownAirportError
def localize(naive: datetime, iata: str) -> datetime   # naive local → aware; raises on aware input
```

## src/engine/risk_filters.py

```python
def risk_verdict(obs: FareObservation, wl: ResolvedWatchlist, *, config_hash: str) -> RiskVerdict
def apply(observations: list[FareObservation], wl: ResolvedWatchlist, *, config_hash: str) -> list[FareObservation]
    # returns copies with .risk set (model_copy(update=...)); input untouched
```

Rules, exactly the §4 table, all checks run, tags in this order:

| Condition | tag |
|---|---|
| `fare_brand is BASIC_ECONOMY and not c.allow_basic_economy` | `basic_economy` |
| any slice `stops > c.max_stops` | `stops>{max_stops}` |
| any slice `max_layover_minutes > c.max_layover_minutes` | `layover>{max_layover_minutes}` |
| any slice `has_coterminal_swap and not c.allow_coterminal_swap` | `coterminal_swap` |
| any slice `has_overnight_layover and c.reject_overnight_layover` | `overnight_layover` |
| any slice `departure_local.time() < c.earliest_departure_local` | `departs_before_{HH:MM}` |
| any slice `arrival_local.time() > c.latest_arrival_local` | `arrives_after_{HH:MM}` |
| `c.allowed_carriers` non-empty and any marketing carrier not in it | `carrier_{XX}` (one tag per offending carrier, sorted) |
| any marketing carrier in `c.excluded_carriers` | `carrier_{XX}` |
| `fare_brand is UNKNOWN` | no tag; `brand_unverified=True` |

`passed = not rejections`. Price is never an input. `c = wl.constraints`.

## src/ingestion/serpapi_client.py

```python
class SerpApiProvider:
    name = Provider.SERPAPI
    def __init__(self, api_key: str, *, client: httpx.Client | None = None, base_url="https://serpapi.com"): ...
    def search(self, target, watchlist, run_id, observed_at) -> ProviderResult
def build_params(target, watchlist, api_key) -> dict[str, str]        # pure; tested directly
def parse(payload: dict, *, target, watchlist, run_id, observed_at) -> ProviderResult   # pure; tested on fixtures
```

Request (`GET {base_url}/search`), one call per target:

| param | value |
|---|---|
| `engine` | `google_flights` |
| `api_key` | key |
| `departure_id` / `arrival_id` | `target.origin` / `target.destination` |
| `outbound_date` | `target.depart` ISO |
| `return_date` + `type=1` | when `target.return_`; else `type=2` |
| `travel_class` | economy 1, premium_economy 2, business 3, first 4 |
| `adults` | `wl.passengers` |
| `currency` | `wl.currency` |
| `hl` / `gl` | `en` / `us` |
| `stops` | `max_stops` 0→`1`, 1→`2`, 2→`3`, else omit |
| `layover_duration` | `"30,{max_layover_minutes}"` when `max_stops > 0`; omit for nonstop-only |
| `outbound_times` | `"{dep_from},23,0,{arr_to}"` (whole hours, rounded inward): `dep_from = ceil(earliest_departure_local to the hour)` (06:00→6, 06:30→7, clamped to 23), `arr_to = hour of latest_arrival_local` (23:30→23, 22:00→22) |
| `return_times` | same value as `outbound_times`, round trips only |
| `exclude_basic` | `true` when `not allow_basic_economy and cabin is ECONOMY` (US-only feature; we are `gl=us`) |
| `include_airlines` | `",".join(allowed_carriers)` when non-empty (never with exclude) |
| `exclude_airlines` | `",".join(excluded_carriers)` when non-empty and allowed is empty |
| `deep_search` | omit (slow, no benefit for price) |
| `no_cache` | omit |

Response shape (from SerpAPI docs; field names exact):

```
search_metadata { id, status: "Success"|"Error"|"Processing", google_flights_url, created_at, ... }
search_parameters { engine, hl, gl, departure_id, arrival_id, outbound_date, return_date?, travel_class, currency, exclude_basic? }
error?  (string; present on failure)
best_flights[] / other_flights[]: {
  flights[]: { departure_airport{name,id,time:"YYYY-MM-DD HH:MM"}, arrival_airport{...}, duration (min),
               airplane, airline (name), airline_logo, travel_class ("Economy"), flight_number ("AS 1234"),
               legroom?, extensions[str], overnight?, often_delayed_by_over_30_min?, ticket_also_sold_by?[] }
  layovers[]?: { duration, name, id, overnight? }
  total_duration, carbon_emissions{...}, price (INTEGER, whole currency units, ROUND-TRIP TOTAL for type=1),
  type ("Round trip"|"One way"), airline_logo, departure_token, extensions?[str]
}
price_insights? { lowest_price, price_level: "low"|"typical"|"high", typical_price_range: [low, high], price_history: [[ts, price],...] }
```

Parsing rules:
* Zero results: the LIVE API answers HTTP 200, `search_metadata.status == "Success"`, no `best_flights`/`other_flights`, AND an `error` string such as `"Google Flights hasn't returned any results for this query."`. Success + no flight lists + an `error` containing `"hasn't returned any results"` (case-insensitive) → `observations=[]`, band from `price_insights` if present. Not an error. `serpapi_zero_results.json` copies this shape. Any other `error` string is still a `ProviderError`.
* `status != "Success"` or any other `error` key or HTTP ≠ 200 → `ProviderError(SERPAPI, msg, status_code=…, retryable = status in {429, 5xx} or timeout)`.
* **Round trips: the response holds OUTBOUND legs only** (return legs cost a second call via `departure_token`, which we do not spend). The observation's `Itinerary` has one slice (outbound) and `price_cents` is the round-trip total. This is documented in ARCHITECTURE §5 (orchestrator adds it). `raw["_note"] = "outbound_only"` for RT.
* Segment: `carrier` = first token of `flight_number` (`"AS 1234"` → `AS`, `"1234"`); `departure_local = localize(parse("%Y-%m-%d %H:%M"), departure_airport.id)`; same for arrival. `aircraft = airplane`. `UnknownAirportError` for any segment → skip that itinerary, count it in `raw["_skipped_unknown_airport"]`, continue. An itinerary that fails `Segment`/`Itinerary`/`FareObservation` model validation (e.g. a 5-digit flight number) is skipped the same way and counted in `raw["_skipped_invalid"]`; the call's band is kept. Whole-payload shape failures (missing `search_metadata`, non-dict body, an entry with no `flights`, an unparseable time or price) stay `ProviderError`.
* `price_cents = price * 100` (integer arithmetic; if `price` is a float with cents, `round(price*100)`). Currency from `search_parameters.currency`, default `wl.currency`.
* Brand, in this order: (1) any itinerary- or segment-level extension containing `"basic"` (case-insensitive) → `BASIC_ECONOMY` with that string as raw, `carry_on_included=None`, ALWAYS, whatever the `exclude_basic` echo says. (2) Else, if the payload echoes `search_parameters.exclude_basic == "true"` AND both `target.origin` and `target.destination` are US airports (`airports.is_us`; unknown code → False) → `MAIN_CABIN`, `fare_brand_raw="google:exclude_basic"`, `carry_on_included=True`. (3) Else `UNKNOWN`, `fare_brand_raw=None`, `carry_on_included=None`. The echo is not evidence on international itineraries.
* `parse(..., api_key=None)` scrubs `api_key=<value>` fragments and the literal key from every string in `raw` (recursively) before returning; `search_metadata.json_endpoint` carries the key.
* `changeable`/`refundable`/`seats_remaining` = None (not provided).
* `booking_url = search_metadata.google_flights_url`.
* `risk = None` always (engine's job). `watchlist_id`/`target_id` from the target. `days_to_departure = target.days_to_departure(observed_at)`.
* Band: `BenchmarkBand(lowest_price_cents=lowest_price*100, typical_low_cents=range[0]*100, typical_high_cents=range[1]*100, price_level=PriceLevel(price_level) or UNKNOWN)`; `None` when `price_insights` absent.
* `raw` = the full payload (runner writes it to `data/raw/`).

Fixtures (tester builds from the shape above; no live key exists):
* `serpapi_sea_sjc_rt_exclude_basic.json` — Thanksgiving target; `best_flights` 2 entries (AS nonstop 07:15→09:25, DL one-stop via SLC with a 95-min layover), `other_flights` 2 (AS 05:40 departure → `departs_before_06:00`; UA one-stop via SFO with a 14h overnight layover → `layover>180` + `overnight_layover`); `price_insights` present; `search_parameters.exclude_basic: "true"`.
* `serpapi_sea_jfk_coterminal.json` — one-way; one itinerary arrives EWR, next segment departs JFK → `coterminal_swap`.
* `serpapi_no_basic_filter_with_basic_extension.json` — `exclude_basic` absent; one entry has extension `"Basic Economy"` → BASIC_ECONOMY; another has no brand hint → UNKNOWN.
* `serpapi_zero_results.json` — the live shape: status Success, no flight lists, `error: "Google Flights hasn't returned any results for this query."`, no `price_insights`.
* `serpapi_sea_lhr_exclude_basic_echo.json` — one-way SEA→LHR with the `exclude_basic` echo: one clean entry (UNKNOWN: echo is not evidence internationally) and one "Basic Economy" extension (BASIC_ECONOMY despite the echo).
* `serpapi_error.json` — `search_metadata.status: "Error"`, `error: "..."`.
* `serpapi_unknown_airport.json` — one itinerary via code `ZZZ` → skipped, others parsed.

## src/ingestion/amadeus_client.py

```python
class AmadeusProvider:
    name = Provider.AMADEUS
    def __init__(self, client_id, client_secret, *, host: Literal["test","production"]="test",
                 client: httpx.Client | None = None, now: Callable[[], datetime] = utcnow): ...
    def search(self, target, watchlist, run_id, observed_at) -> ProviderResult
    # provider on every observation/band is MOCK when host == "test" (ARCHITECTURE §5)
def build_params(target, watchlist) -> dict[str, str]
def parse(payload: dict, *, target, watchlist, run_id, observed_at, provider: Provider) -> ProviderResult
def map_brand(cabin, branded_fare, branded_fare_label, *, carrier: str | None = None) -> tuple[FareBrand, str | None]  # one segment
```

Hosts: test `https://test.api.amadeus.com`, production `https://api.amadeus.com`.
Auth: `POST {host}/v1/security/oauth2/token`, form `grant_type=client_credentials&client_id&client_secret`
→ `{access_token, expires_in, ...}`; cache in memory, refresh when `now() >= acquired + expires_in - 60s`.
Search: `GET {host}/v2/shopping/flight-offers` with
`originLocationCode, destinationLocationCode, departureDate, returnDate?, adults, travelClass (ECONOMY|PREMIUM_ECONOMY|BUSINESS|FIRST), nonStop=true when max_stops==0, currencyCode, max=20, includedAirlineCodes?/excludedAirlineCodes?` (never both).
`Authorization: Bearer <token>`. 401 once → refresh token and retry once (that is auth, not a provider retry). 429/5xx → `ProviderError(retryable=True)`; other 4xx → `retryable=False`.

Response shape (from the official OpenAPI spec; a full example is already at
`tests/fixtures/amadeus/spec_example_syd_bkk.json`):

```
meta { count, links{self} }
data[]: {
  type:"flight-offer", id, source:"GDS", instantTicketingRequired, nonHomogeneous, oneWay,
  lastTicketingDate, numberOfBookableSeats,
  itineraries[]: { duration:"PT14H15M", segments[]: {
      departure{iataCode, terminal?, at:"2021-11-01T11:35:00"}, arrival{iataCode, terminal?, at},
      carrierCode, number, aircraft{code}, operating?{carrierCode}, duration, id, numberOfStops, blacklistedInEU } },
  price { currency, total:"355.34", base, fees[]{amount,type}, grandTotal:"355.34" },
  pricingOptions { fareType[], includedCheckedBagsOnly },
  validatingAirlineCodes[],
  travelerPricings[]: { travelerId, fareOption, travelerType, price{currency,total,base},
      fareDetailsBySegment[]: { segmentId, cabin:"ECONOMY", fareBasis, brandedFare?, brandedFareLabel?, class,
          includedCheckedBags?{quantity? | weight?, weightUnit?}, includedCabinBags?{quantity}, amenities?[]{description, isChargeable, amenityType} } }
}
dictionaries { locations{IATA:{cityCode,countryCode}}, aircraft{code:name}, currencies, carriers{code:name} }
```

Parsing rules:
* Every `data[]` offer → one `FareObservation`. All itineraries become slices (RT = 2 slices, full key).
* `price_cents = int(Decimal(price.grandTotal) * 100)` (Decimal, never float). Currency `price.currency`.
* Segment: `carrier=carrierCode`, `operating_carrier=operating.carrierCode` if present, `flight_number=number`,
  `aircraft=dictionaries.aircraft.get(aircraft.code, aircraft.code)`, times `localize(fromisoformat(at), iataCode)`.
  `UnknownAirportError` → skip offer, count in `raw["_skipped_unknown_airport"]`.
* Brand is the WORST across ALL `travelerPricings[*].fareDetailsBySegment[*]` (one `map_brand` call per segment, passing that segment's `carrierCode`, looked up by `segmentId`): any segment `BASIC_ECONOMY` → `BASIC_ECONOMY`; else any segment `UNKNOWN` → `UNKNOWN`; else the lowest tier present in the order `MAIN_CABIN < MAIN_PLUS < PREMIUM_ECONOMY < BUSINESS < FIRST`. `fare_brand_raw` is the deciding segment's `brandedFareLabel or brandedFare or None`. No fare details at all → `UNKNOWN`. `map_brand` rules, in order (upper-cased `brandedFare` + label; a field that is exactly a cabin name — `ECONOMY`, `PREMIUM ECONOMY`, `BUSINESS`, `FIRST` — is not brand evidence and is ignored):
  - contains `"BASIC"` → `BASIC_ECONOMY`
  - cabin BUSINESS / FIRST → that cabin (beats every economy-family keyword: "BUSINESS SAVER", "BUSINESS FLEX" → `BUSINESS`)
  - contains `LIGHT` → `BASIC_ECONOMY` (any carrier)
  - contains `SAVER` → `BASIC_ECONOMY` if `carrier == "AS"`, else `UNKNOWN` (raw kept); never `MAIN_CABIN`
  - contains `"PREMIUM"` and cabin ECONOMY/PREMIUM_ECONOMY → `PREMIUM_ECONOMY`
  - contains any of `COMFORT`, `PLUS`, `PREFERRED`, `EXTRA`, `FLEX` → `MAIN_PLUS`
  - contains any of `MAIN`, `STANDARD`, `CLASSIC`, or the whole word `ECO` (`\bECO\b`, so "ECONOMY" does not match) → `MAIN_CABIN`
  - cabin PREMIUM_ECONOMY → `PREMIUM_ECONOMY`
  - otherwise `UNKNOWN` (raw kept, so mapping fixes can be made from the DB)
  Tests pin every branch; the table is expected to grow as `fare_brand_raw` values show up in the DB.
* `carry_on_included`, aggregated over every segment: `includedCabinBags.quantity > 0` per segment; False if any segment is known to have no carry-on, else None if any segment is silent, else True. `seats_remaining = numberOfBookableSeats`.
* `changeable` / `refundable`, aggregated over every segment: per segment, from `amenities` with `amenityType == "BRANDED_FARES"` whose description upper-contains `CHANGE` / `REFUND` and `isChargeable is False`; the offer is `True` only if EVERY segment is, else `None` (chargeable or absent → None; don't guess).
* `booking_url = None` (Amadeus is not a booking link). `risk=None`.
* Band: Amadeus has no typical-price band → `None`.
* Empty `data` → `observations=[]`, not an error. `errors[]` key or HTTP ≠ 200 → `ProviderError`.

Fixtures: `spec_example_syd_bkk.json` (exists; two PR offers, bags by weight), plus tester-built
`amadeus_sea_sjc_branded.json` with four offers covering BASIC / MAIN / COMFORT+ / UNKNOWN brands,
`includedCabinBags`, a chargeable CHANGE amenity, `numberOfBookableSeats`, and one offer with an
unknown airport code; `amadeus_mixed_brand.json` (seven round-trip offers where segment 1 and a later segment/traveler disagree: Main/Basic, Main/no-brand, Comfort+/Main, two travelers, AS Saver, DL Saver, silent carry-on bag count); `amadeus_token.json` `{access_token, token_type, expires_in: 1799}`;
`amadeus_error_400.json` `{"errors":[{"status":400,"code":477,"title":"INVALID FORMAT","detail":"..."}]}`.
Auth tests use `respx` to assert the token endpoint is hit once across two searches, and again after expiry.

## Budget note (for ARCHITECTURE §7, orchestrator adds)

Adding `amadeus` to a watchlist's `providers` doubles that watchlist's calls under the single
breaker. The Amadeus production free quota is separate from SerpAPI's; a per-provider breaker is
a Phase 5 option if Amadeus is turned on.
