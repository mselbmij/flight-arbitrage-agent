# flight-arbitrage-agent — Architecture

A risk-aware airline fare tracker. It polls a small set of routes a couple of times a day,
throws away fares that are traps (Basic Economy, 14-hour layovers, JFK→EWR "connections"),
alerts when a clean fare lands in the buy window for its days-to-departure regime, and
after you buy, keeps watching that exact itinerary for a re-fare credit.

This document is the contract the agents build against. If code and this doc disagree,
the doc is wrong until someone updates it in the same PR.

## 0. Decisions that differ from the original blueprint

| Original | Here | Why |
|---|---|---|
| μ − 1.5σ Bollinger signals with a 14-observation cold-start gate | **External benchmark band only.** Internal stats are deferred to a "Phase 7, if ever". | 14 daily points is not a σ estimate, and the series drifts with DTD so a rolling mean fires on the drift. Google's typical-price band already encodes a year of route history. |
| Duffel for live NDC verification | **Amadeus Self-Service** as the second provider; SerpAPI/Google Flights as primary | Duffel's live mode needs business verification and is priced per booking. Amadeus has a free production quota and returns branded fares. Provider is an interface; the choice is reversible. |
| GitHub Actions cron + commit `fares.db` back to a branch | **cron on a host you control** (Pi / VPS / Mac mini), local SQLite, `scripts/backup_db.sh` | Actions cron is delayed at peak hours and auto-disabled after 60 days of repo inactivity. Committing a binary DB from a runner is non-idempotent and conflicts on overlap. Turso only if the dashboard must run off-host. |
| `MAX_DAILY_API_CALLS = 25` | Set from the plan you pay for; default 4/day | 25/day is a paid SerpAPI tier. The breaker must match the budget or it protects nothing. |
| "20th percentile" thresholds | `price <= reference × multiplier`, reference ∈ {typical_low, typical_high, lowest_seen, ceiling} | Google gives a band, not percentiles. Thresholds are per-route config, not constants. |
| Phases 1–6 as listed | Schema → DB + seeder → **dashboard on mock data** → ingestion + risk → rules → alerts → stats (maybe) | You can't write thresholds against data you haven't plotted. |
| Regimes 1–4 | Adds `LATE` (DTD ≤ 14) and `EXPIRED` | The original was silent on the last two weeks. |

## 1. System shape

```
                 cron (2×/day, jittered)
                          │
                          ▼
   config/watchlists.yaml ──► src/runner/cli.py ──► src/db.py ◄── dashboard/app.py (Streamlit, read-only)
                                   │                   ▲
                                   │ one Run           │ observations, bands, api_calls, alerts
                                   ▼                   │
                     ┌──── src/ingestion/* ────┐        │
                     │ serpapi_client.py       │        │
                     │ amadeus_client.py       │──► FareObservation[] + BenchmarkBand
                     │ mock_client.py          │        │
                     └─────────────────────────┘        │
                                   │                    │
                                   ▼                    │
                      src/engine/risk_filters.py  ──► RiskVerdict on each observation
                                   │
                                   ▼
                      src/engine/rules.py         ──► Alert[] (regime-aware, external-band strikes,
                                   │                  post-purchase credit + DOT window)
                                   ▼
                      src/alerts/notifier.py      ──► Telegram / Discord (idempotent by alert_key)
```

Everything is synchronous and single-process. One run = one `Run` row. The circuit breaker
is consulted before every provider call and trips the run, not the process.

## 2. Data model

All rows validate through `src/models.py`. SQLite, WAL mode, one file.

```
┌──────────────────┐        ┌──────────────────────┐
│ runs             │        │ api_calls            │
│──────────────────│        │──────────────────────│
│ run_id      PK   │◄──┐    │ id            PK     │
│ started_at       │   └────│ run_id        FK     │
│ finished_at      │        │ called_at            │
│ trigger          │        │ provider             │
│ calls_made       │        │ endpoint             │
│ status           │        │ watchlist_id  (yaml) │
│ error            │        │ status               │
└──────────────────┘        │ cost_units           │
                            │ latency_ms, error    │
                            └──────────────────────┘

┌───────────────────────────┐      ┌──────────────────────────┐
│ observations              │      │ benchmark_bands          │
│───────────────────────────│      │──────────────────────────│
│ id              PK        │      │ id             PK        │
│ watchlist_id    (yaml id) │      │ watchlist_id   (yaml id) │
│ observed_at     UTC       │      │ observed_at    UTC       │
│ provider                  │      │ provider                 │
│ run_id          FK        │      │ run_id         FK        │
│ itinerary_key   idx       │      │ lowest_price_cents       │
│ itinerary_json            │      │ typical_low_cents        │
│ price_cents, currency     │      │ typical_high_cents       │
│ fare_brand, fare_brand_raw│      │ price_level              │
│ changeable, refundable    │      │ currency                 │
│ carry_on_included         │      └──────────────────────────┘
│ seats_remaining           │
│ days_to_departure         │      ┌──────────────────────────┐
│ booking_url               │      │ alerts                   │
│ raw_payload_ref           │      │──────────────────────────│
│ risk_passed               │      │ id             PK        │
│ risk_rejections_json      │      │ created_at               │
│ risk_brand_unverified     │      │ watchlist_id             │
│ risk_config_hash          │      │ kind, regime             │
└───────────────────────────┘      │ observation_id FK (null) │
        ▲                          │ itinerary_key            │
        └──────────────────────────│ price_cents              │
                                   │ reference_cents          │
                                   │ message                  │
                                   │ alert_key      UNIQUE    │
                                   │ delivered_at, channel    │
                                   └──────────────────────────┘
```

Indexes that matter: `observations(watchlist_id, observed_at)`, `observations(itinerary_key, observed_at)`,
`api_calls(called_at)` (the breaker sums today), `alerts(alert_key)` unique.

Watchlists are **not** a table. They live in YAML, are the source of truth, and are referenced
by id. `config_hash` on each observation records which rules judged it.

### Why these choices

* **Integer cents.** `$418.00` is `41800`. Float money is a bug waiting for a reviewer.
* **`itinerary_key`** = sha1 of (carrier, flight number, local departure, origin, destination) per
  segment. Two providers reporting the same flights produce the same key, so post-purchase tracking
  and cross-provider comparison are joins, not heuristics. Price is not part of identity.
* **Local times on segments, UTC on observations.** "No departures before 06:00" is about the
  passenger's clock. "When did we poll" is about ours.
* **Risk verdict stored on the row.** It is recomputable, but the dashboard needs it without
  re-running the engine, and `config_hash` says which rules produced it.
* **Raw payloads on disk** (`data/raw/<run_id>/<provider>-<watchlist>.json`) rather than in the DB.
  They exist for replay and for fixing brand mappings; they are not query data.

### Schema change policy

`src/models.py` is the one-way door. A change there needs: an ARCHITECT review, a numbered
migration in `src/migrations/`, and a fixture regenerated by the TESTER. Adding a nullable column
is cheap; renaming or re-keying is a new table plus backfill.

## 3. Watchlist lifecycle

```
            yaml: status=watching
                     │
      ┌──────────────┼──────────────────┬───────────────────┬─────────────────┐
      ▼              ▼                  ▼                   ▼                 ▼
   MONITOR        PRIME              FENCE               LATE             EXPIRED
  DTD > 65     21 < DTD ≤ 65     14 < DTD ≤ 21         DTD ≤ 14           DTD < 0
  strike =     strike =          strike =              strike =           no polling
  typical_low  typical_low       typical_low × 1.15    ceiling × 1.00
  × 0.90       × 1.00            + FENCE_WARNING on
                                 entry (once)
      └──────────────┴──────────────────┴───────────────────┘
                                   │
                     you edit yaml: status=purchased + purchase: {…}
                                   │
                                   ▼
                             POST_PURCHASE
              poll only that itinerary_key; alert on
              CREDIT_DROP  (price ≤ purchase − threshold, and purchase.changeable)
              DOT_24H      (now − purchased_at < 24h and DTD ≥ 7 at purchase; once)
                                   │
                                   ▼  departure passes
                                EXPIRED
```

Rules the engine must honour:

* A `BUY` alert requires `risk.passed` **and** `not risk.brand_unverified`. An unverified-brand
  fare in the strike zone produces a `BUY` with the message prefixed `[brand unverified]` only
  when `fare_brand_raw` is non-null; otherwise no alert.
* `ceiling_cents` is an absolute strike in every regime: price ≤ ceiling always alerts.
* The strike reference `lowest_seen` is the min `price_cents` of **risk-passed** observations
  for that watchlist, provider-independent. It is the only internal statistic allowed.
* `alert_key` = `f"{watchlist_id}:{kind}:{itinerary_key or '-'}:{price_bucket}"` where
  `price_bucket = price_cents // 1000`. Same key within `dedup_window_hours` (default 24) is not re-sent.
* `FENCE_WARNING` fires once per watchlist, the first run after DTD crosses 21.

## 4. Risk filters (src/engine/risk_filters.py)

Order matters only for the rejection message; all checks run.

| Check | Rejection tag | Source of truth |
|---|---|---|
| `fare_brand == BASIC_ECONOMY and not allow_basic_economy` | `basic_economy` | `Constraints.allow_basic_economy` |
| `slice.stops > max_stops` (any slice) | `stops>N` | `Constraints.max_stops` |
| `slice.max_layover_minutes > max_layover_minutes` | `layover>N` | `Constraints.max_layover_minutes` |
| `slice.has_coterminal_swap and not allow_coterminal_swap` | `coterminal_swap` | `COTERMINAL_GROUPS` in models |
| `slice.has_overnight_layover and reject_overnight_layover` | `overnight_layover` | `Constraints` |
| `slice.departure_local.time() < earliest_departure_local` | `departs_before_HH:MM` | `Constraints` |
| `slice.arrival_local.time() > latest_arrival_local` | `arrives_after_HH:MM` | `Constraints` |
| carrier not in `allowed_carriers` (if non-empty) / in `excluded_carriers` | `carrier_XX` | `Constraints` |
| `fare_brand == UNKNOWN` | *(no rejection; sets `brand_unverified`)* | — |

`price` is never an input to a risk filter. Cheap is not a risk; wrong product is.

## 5. Providers (src/ingestion/)

`base.py` defines:

```python
class FlightProvider(Protocol):
    name: Provider
    def search(self, w: ResolvedWatchlist, run_id: str) -> ProviderResult: ...
    # ProviderResult = (observations: list[FareObservation], band: BenchmarkBand | None, raw: dict)
```

Each client is responsible for: mapping its raw brand strings onto `FareBrand` (table in the
client, extended as `fare_brand_raw` values show up in the DB), producing tz-aware local segment
times, and raising `ProviderError` rather than returning partial data. The runner, not the
client, logs the `ApiCall` and consults the breaker.

* **SerpAPI (Google Flights).** Primary. Supplies the benchmark band (`price_insights`). Brand
  detection is best-effort from extension strings; expect `UNKNOWN` often → the band is still used.
* **Amadeus Self-Service.** Second price source with `fareBasis`/branded-fare detail. `AMADEUS_HOST=test`
  returns fake prices and **must** be stored with `provider=mock`, never `amadeus`.
* **Mock.** Reads `tests/fixtures/*.json`. Used by the seeder and all unit tests.

## 6. Circuit breaker (src/db.py)

Two gates, both read from `.env`:

1. `MAX_CALLS_PER_RUN` (default 3): checked against `runs.calls_made` before each call.
2. `MAX_DAILY_API_CALLS` (default 4): checked against `SUM(cost_units) FROM api_calls WHERE called_at >= today_utc`.

Either gate failing → log an `api_calls` row with `status=skipped_breaker`, set the run to
`budget_tripped`, emit one `BUDGET_TRIPPED` alert, stop. No retries. The breaker is consulted
inside the same SQLite transaction that inserts the call row, so two overlapping runs cannot
both pass.

## 7. Runner and scheduling

`fares poll` (Typer CLI) runs one `Run`. Cron on the host:

```
# 06:50 and 17:20 local, with up to 9 min of jitter so you don't hit providers on the hour
50 6,17 * * *  sleep $((RANDOM % 540)); cd /opt/flight-arbitrage-agent && .venv/bin/fares poll >> ops/poll.log 2>&1
0  3   * * *  /opt/flight-arbitrage-agent/scripts/backup_db.sh
```

`fares seed` populates `fares.db` from fixtures (45 days, with a 21-day jump and a Basic Economy
trap). `fares key <watchlist_id>` prints the current cheapest risk-passed `itinerary_key` so you
can paste it into `purchase:`.

## 8. Dashboard (dashboard/app.py)

Streamlit + Plotly, read-only against `fares.db`. Three views, per watchlist:

1. **Price vs time** — risk-passed observations as points, rejected as hollow points, the
   benchmark band as a shaded ribbon, ceiling as a dashed line. No Bollinger bands.
2. **Price vs days-to-departure** — same data on a DTD x-axis, vertical lines at 65/21/14/7.
3. **Status table** — watchlist, regime, cheapest passed fare, strike reference, API calls today / budget,
   last alert.

## 9. Phase plan (revised)

| Phase | Deliverable | Owner agents | Reviewer? |
|---|---|---|---|
| 1 | this doc, `models.py`, `watchlists.yaml`, `pyproject.toml`, agent files | orchestrator | — |
| 2 | `db.py` (schema, migrations, breaker), `fares seed`, mock provider, fixtures | TESTER → BUILDER → JANITOR | yes |
| 3 | `dashboard/app.py` against seeded data | BUILDER → JANITOR | no |
| 4 | `serpapi_client.py`, `amadeus_client.py`, `risk_filters.py` | TESTER → 2× BUILDER (parallel) → JANITOR | yes |
| 5 | `rules.py` (regimes, strikes, post-purchase), `fares poll` end-to-end on mock | TESTER → BUILDER → JANITOR | yes |
| 6 | `notifier.py`, cron + backup scripts, first live run with real keys | BUILDER → JANITOR | no |
| 7? | internal statistics, only if ≥ 90 days of data across ≥ 3 routes says they add signal | ARCHITECT decides | yes |

Branch per phase (`phase-N-<slug>`), PR into `main`, CI green + REVIEWER pass before merge on the
phases marked yes.

## 10. Risk matrix (what can go wrong, what stops it)

| Failure | Mitigation | Where |
|---|---|---|
| Basic Economy reported as a price drop | brand normalization; reject by default | risk_filters |
| Unknown brand treated as Main Cabin | `brand_unverified`; cannot BUY alone | models + rules |
| Phantom fare (aggregator cache stale) | second provider; alerts carry provider + observed_at; booking link opened by a human | ingestion |
| Runaway API spend | two-gate breaker in the same transaction as the call log | db |
| Toxic schedule | stops / layover / overnight / co-terminal / time-of-day filters | risk_filters |
| Duplicate alerts | `alert_key` unique + dedup window | db + notifier |
| Stats on too little data | no internal σ at all; `lowest_seen` is the only internal number | rules |
| Test-env prices polluting history | Amadeus test host forced to `provider=mock` | amadeus_client |
| Runner host dies silently | `BUDGET_TRIPPED`/`PROVIDER_ERROR` alerts; cron log; a `fares health` check that alerts if no run in 36h | runner (Phase 6) |
| Losing the DB | nightly `backup_db.sh` to a second disk / cloud bucket | scripts |
