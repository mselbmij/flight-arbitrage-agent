# Fixtures

- `mock/sea-sjc-thanksgiving_SEASJC_2026-11-25_2026-11-29.json`: MockProvider input for the Thanksgiving target: AS nonstop main_cabin (provider serpapi), DL 1-stop main_cabin via SLC (provider mock), AS basic_economy trap (provider serpapi, risk rejected), plus a serpapi band; run_id, observed_at and days_to_departure are filled by the provider.
- `amadeus/spec_example_syd_bkk.json`: official Amadeus OpenAPI example; two PR offers SYD->MNL->BKK (one-way search, one slice each), EUR 355.34, bags by weight, no brand fields (UNKNOWN), no carry-on info.
- `amadeus/amadeus_sea_sjc_branded.json`: round-trip SEA<->SJC, five offers: BASIC (AS, QX-operated, 0 cabin bags, chargeable change), MAIN (DL, free change, chargeable refund), COMFORT+ (DL, grandTotal differs from total), no-brand UNKNOWN (UA, float-trap price 256.03, unmapped aircraft code, non-BRANDED_FARES CHANGE amenity, no seat count), and one via unknown airport ZZZ (skipped + counted).
- `amadeus/amadeus_mixed_brand.json`: seven DL/AS round-trip offers whose segments disagree: Main+Basic (BASIC), Main+no brand (UNKNOWN), Comfort+ and Main (MAIN_CABIN, free change throughout), two travelers with a Basic return for traveler 2 (BASIC), AS Saver outbound (BASIC), DL Saver both ways (UNKNOWN), and a silent cabin-bag count on one segment (carry-on None).
- `amadeus/amadeus_token.json`: OAuth token response (`expires_in` 1799); tests override `access_token` per case.
- `amadeus/amadeus_error_400.json`: Amadeus `errors[]` body (400, code 477 INVALID FORMAT).
- `serpapi/serpapi_sea_sjc_rt_exclude_basic.json`: Thanksgiving round-trip with `exclude_basic: "true"`; AS nonstop and DL via SLC (95 min) pass, AS 05:40 trips `departs_before_06:00`, UA via SFO with a 14h overnight layover trips `layover>180` + `overnight_layover`; `price_insights` typical band 190-260.
- `serpapi/serpapi_sea_jfk_coterminal.json`: one-way SEA->BOS; a clean AS nonstop plus a UA SEA->EWR then DL JFK->BOS itinerary that trips `coterminal_swap`; band level `low`.
- `serpapi/serpapi_no_basic_filter_with_basic_extension.json`: round-trip with no `exclude_basic`, no `price_insights`; one itinerary-level "Basic Economy" extension, one segment-level lowercase "basic economy fare: no changes", one entry with no brand hint (UNKNOWN).
- `serpapi/serpapi_zero_results.json`: the live no-results shape: status Success, no flight lists, `error` "Google Flights hasn't returned any results for this query.", no `price_insights`.
- `serpapi/serpapi_sea_lhr_exclude_basic_echo.json`: one-way SEA->LHR with the `exclude_basic` echo; a clean BA entry (echo is not evidence internationally -> UNKNOWN) and a VS entry with a "Basic Economy" extension (BASIC_ECONOMY despite the echo).
- `serpapi/serpapi_error.json`: `search_metadata.status` "Error" with an `error` string (invalid API key).
- `serpapi/serpapi_unknown_airport.json`: three itineraries, the middle one connects through unknown code ZZZ and is skipped (counted), the others parse.
