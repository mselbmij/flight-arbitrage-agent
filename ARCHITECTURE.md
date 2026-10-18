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
| `MAX_DAILY_API_CALLS = 25` | Set from the plan you pay for; default 3/day (SerpAPI free tier) | 25/day is a paid SerpAPI tier. The breaker must match the budget or it protects nothing. |
| "20th percentile" thresholds | `price <= reference × multiplier`, reference ∈ {typical_low, typical_high, lowest_seen, ceiling} | Google gives a band, not percentiles. Thresholds are per-route config, not constants. |
| Phases 1–6 as listed | Schema → DB + seeder → **dashboard on mock data** → ingestion + risk → rules → alerts → stats (maybe) | You can't write thresholds against data you haven't plotted. |
| Regimes 1–4 | Adds `LATE` (DTD ≤ 14) and `EXPIRED` | The original was silent on the last two weeks. |
| One route + one date pair per watchlist | A watchlist is a **grid** — origins × destinations × departure window × trip length — expanded into `SearchTarget`s, capped at 60, polled round-robin under the budget | "SEA to the Bay Area, leave Thu/Fri mid-Jan, 3–4 nights" is the real question. The cost is budget: see §7. |

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
                      src/alerts/notifier.py      ──► ntfy.sh topic (idempotent by alert_key)
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
│ error            │        │ target_id            │
│                  │        │ status               │
└──────────────────┘        │ cost_units           │
                            │ latency_ms, error    │
                            └──────────────────────┘

┌───────────────────────────┐      ┌──────────────────────────┐
│ observations              │      │ benchmark_bands          │
│───────────────────────────│      │──────────────────────────│
│ id              PK        │      │ id             PK        │
│ watchlist_id    (yaml id) │      │ watchlist_id   (yaml id) │
│ target_id       idx       │      │ target_id      idx       │
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
│ risk_inbound_unverified   │      │ (see below)              │
│ risk_config_hash          │      │ target_id      (nullable)│
└───────────────────────────┘      │ kind, regime             │
                                   │ observation_id FK (null) │
        ▲                          │ itinerary_key            │
        └──────────────────────────│ price_cents              │
                                   │ reference_cents          │
                                   │ message                  │
                                   │ alert_key      idx       │
                                   │ delivered_at, channel    │
                                   └──────────────────────────┘
```

Indexes that matter: `observations(target_id, observed_at)`, `observations(itinerary_key, observed_at)`,
`api_calls(target_id, called_at)` (round-robin reads "last polled" from here; the breaker sums today),
`alerts(alert_key, created_at)` (dedup lookup).

Watchlists and targets are **not** tables. Watchlists live in YAML and are the source of truth; a
`SearchTarget` is derived from the grid at load time and identified by a deterministic
`target_id = "{watchlist_id}:{ORIG}{DEST}:{depart}:{return|ow}"`, so observations survive YAML edits
that don't change the leg. `config_hash` on each observation records which rules judged it.

Storage rules: money INTEGER cents; enums as their string values; datetimes as tz-aware ISO text,
naive input rejected at the boundary; `itinerary` as JSON (`itinerary_key` excluded, it is
recomputed); `risk` flattened to five nullable columns, all-NULL when the verdict is absent.

### Why these choices

* **Integer cents.** `$418.00` is `41800`. Float money is a bug waiting for a reviewer.
  `price_cents` is the total for all `passengers` on the watchlist, as both providers quote it.
* **`itinerary_key`** = sha1 of (carrier, flight number, local departure, origin, destination) per
  segment. Two providers reporting the same flights produce the same key, so post-purchase tracking
  and cross-provider comparison are joins, not heuristics. Price is not part of identity.
* **Local times on segments, UTC on observations.** "No departures before 06:00" is about the
  passenger's clock. "When did we poll" is about ours.
* **Risk verdict stored on the row.** It is recomputable, but the dashboard needs it without
  re-running the engine, and `config_hash` says which rules produced it.
* **Raw payloads on disk** (`data/raw/<run_id>/<provider>-<target_id with ':'→'_'>.json`, written before the DB insert) rather than in the DB.
  They exist for replay and for fixing brand mappings; they are not query data.

### Schema change policy

`src/models.py` is the one-way door. A change there needs: an ARCHITECT review, a numbered
migration in `src/migrations/`, and a fixture regenerated by the TESTER. Adding a nullable column
is cheap; renaming or re-keying is a new table plus backfill.

## 3. Target lifecycle

Regime is a property of a **target** (each has its own departure date), not of the watchlist.
A flexible watchlist can have targets in MONITOR and PRIME at the same time.

```
            yaml: status=watching   →   each SearchTarget, independently:
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
              poll only purchase.target_id; alert on that itinerary_key:
              CREDIT_DROP  (price ≤ purchase − threshold, and purchase.changeable)
              DOT_24H      (now − purchased_at < 24h and DTD ≥ 7 at purchase; once)
                                   │
                                   ▼  departure passes
                                EXPIRED
```

Rules the engine must honour:

* A `BUY` alert requires `risk.passed` **and** `not risk.brand_unverified`. An unverified-brand
  fare in the strike zone produces a `BUY` with the message prefixed `[brand unverified]` only
  when `fare_brand_raw` is non-null; otherwise no alert. A row with `risk.inbound_unverified`
  (SerpAPI round trip, outbound slice only) gets the prefix `[return not checked]`; both prefixes
  may stack.
* One alert per target per run: among qualifying observations the cheapest **verified** fare wins;
  an unverified-brand fare (with a raw label) wins only when no verified fare qualifies, and when
  a verified fare wins over a cheaper unverified one the body says `also seen: $190 "Weird"
  (brand unverified)`. Five fares in the strike zone are one BUY, not five.
* `CREDIT_DROP` candidates: same predicate as BUY, plus `fare_brand is not BASIC_ECONOMY` (even
  when the watchlist allows it — you cannot re-fare into a BE ticket) and `changeable is not
  False`. The DOT 7-day test and the "closes HH:MM" title use `purchased_at`'s own timezone, and
  the title carries the zone label. Write `purchased_at` with your local offset.
* `PROVIDER_ERROR` key: `{watchlist_id}:provider_error:{target_id}:{provider}` (stored with
  `target_id` NULL so it never gates FENCE/DOT); `BUDGET_TRIPPED` key `{watchlist_id}:budget_tripped:-:0`,
  text `API budget tripped (used/limit run|day)`.
* Deferred (named phase 7): the grid-BUY line naming siblings polled in the last 48h. `evaluate`
  has no sibling data today.
* `ceiling_cents` is an absolute strike in every regime: price ≤ ceiling always alerts.
* The strike reference `lowest_seen` is the min `price_cents` of **risk-passed** observations
  with a **verified brand** (`risk_brand_unverified = 0`) for that **target**, provider-independent.
  It is the only internal statistic allowed. It is provider-independent, which is why synthetic and
  test-host data must never share a DB with live data (see §7 seed isolation).
* `alert_key` = `f"{target_id}:{kind}:{itinerary_key or '-'}:{price_bucket}"` where
  `price_bucket = price_cents // 1000`. Same key within `dedup_window_hours` (default 24) is not
  inserted (`Database.insert_alert` returns None). The window is measured from the new alert's
  `created_at`; a prior alert exactly `dedup_window_hours` old does **not** suppress a repeat
  (strict `>`), and §7a's retry window uses the same boundary. The key is not unique over all
  time — the same fare can legitimately re-alert a day later.
  Watchlist-level kinds (`BUDGET_TRIPPED`) use `watchlist_id` in place of `target_id`.
* `FENCE_WARNING` fires once per target, the first run after that target's DTD crosses 21. For a
  grid, the alert names the target and how many siblings are still in PRIME.
* A `BUY` on a grid target says which siblings were polled in the last 48h and their cheapest
  passed fare, so "SJC Thu $187" can be judged against "SFO Fri $203 (yesterday)" without opening
  the dashboard.

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
times, and raising `ProviderError` rather than returning partial data. Any parse or validation
failure surfaces as `ProviderError(retryable=False)`; a raw exception after a paid call would
leave the ledger row `ok` and crash the run. Clients never pre-fill `risk` — the verdict belongs to
`risk_filters.py` — and always stamp `watchlist_id`/`target_id` from the requested target. The runner, not the
client, logs the `ApiCall` and consults the breaker.

* **SerpAPI (Google Flights).** Primary. Supplies the benchmark band (`price_insights`). We send
  `exclude_basic=true` whenever the watchlist disallows Basic Economy. **Evidence beats the echo:**
  any extension string containing "basic" → `BASIC_ECONOMY` regardless of the flag; the echoed
  flag maps to `MAIN_CABIN` / `google:exclude_basic` only when both endpoints are US airports
  (the filter is US-only); everything else is `UNKNOWN`. Time-of-day and layover constraints are
  pushed into the request (`outbound_times`, `return_times`, `layover_duration`) so the unverified
  return leg of a round trip at least obeys them. A zero-result search is HTTP 200 + `Success` + an
  `error` string saying no results; it is empty, not an error. A single itinerary that fails
  validation is skipped and counted (`raw["_skipped_invalid"]`), never the whole paid call. The
  api key is scrubbed from `raw` before it is persisted. **Round trips
  return outbound legs only** — the return legs cost a second call via `departure_token`, which the
  free tier can't spare — so a SerpAPI round-trip observation has a one-slice itinerary and a
  round-trip total price. Its `itinerary_key` therefore differs from an Amadeus key for the same
  flights; cross-provider joins are per-slice work for a later phase.
* Segment times from every provider are local wall-clock with no offset; `src/ingestion/airports.py`
  localizes them via the `airportsdata` IATA→IANA table. An unknown airport skips that itinerary
  (counted in `raw["_skipped_unknown_airport"]`), never the whole call.
* **Amadeus Self-Service.** Second price source with `fareBasis`/branded-fare detail. `AMADEUS_HOST=test`
  returns fake prices and **must** be stored with `provider=mock`, never `amadeus`. The brand is the
  **worst across every segment of every traveler** (any Basic → Basic; any unmapped → Unknown);
  carry-on / changeable / refundable are true only if true on every segment. Brand keywords that
  are Basic-Economy equivalents map to `BASIC_ECONOMY`: `LIGHT` on any carrier, `SAVER` on Alaska
  (`AS`); `SAVER` elsewhere is `UNKNOWN`. A label that is just the cabin name is not brand
  evidence. False rejects cost a missed alert; false accepts cost an unchangeable ticket, so every
  ambiguity resolves toward rejection.
* **Mock.** Reads `tests/fixtures/*.json`. Used by the seeder and all unit tests.

## 6. Circuit breaker (src/db.py)

Two gates, both read from `.env`:

1. `MAX_CALLS_PER_RUN` (default 2): checked against `runs.calls_made` before each call.
2. `MAX_DAILY_API_CALLS` (default 3): checked against `SUM(cost_units) FROM api_calls WHERE called_at >= today_utc`.

Either gate failing → log an `api_calls` row with `status=skipped_breaker`, set the run to
`budget_tripped`, emit one `BUDGET_TRIPPED` alert, stop. No retries. The breaker is consulted
inside the same SQLite transaction that inserts the call row, so two overlapping runs cannot
both pass.

## 7. Runner and scheduling

`fares poll` (Typer CLI) runs one `Run`. Cron on the host:

```
# 06:50 and 17:20 local, with up to 9 min of jitter so you don't hit providers on the hour
50 6,17 * * *  sleep $(awk 'BEGIN{srand(); print int(rand()*540)}'); cd /opt/flight-arbitrage-agent && .venv/bin/fares poll --trigger cron >> ops/poll.log 2>&1
0  3   * * *  /opt/flight-arbitrage-agent/scripts/backup_db.sh
0  9,21 * * * cd /opt/flight-arbitrage-agent && .venv/bin/fares health >> ops/health.log 2>&1
```

Inside one run, for each watchlist with status `watching`:

1. If `poll.scan_provider` is set: one parametric call per origin-destination pair (Amadeus
   *Flight Cheapest Date Search* takes a departure range + duration range and returns a date
   grid). Its cheapest dates that match existing targets are queued first. The scan costs one
   call per O-D pair and never produces an observation — scan prices are cached, unbranded and
   not comparable; they only choose *which* targets to spend exact-date calls on. (Production
   coverage of that endpoint is route-limited; the scan tier degrades to no-op on a 4xx.)
2. Remaining budget: `targets_per_run` targets, ordered by least-recently-polled
   (`MAX(api_calls.called_at) GROUP BY target_id`, nulls first), then by DTD ascending so
   near-departure targets never starve. One exact-date call per provider per target.
3. `status=purchased`: only `purchase.target_id`, every run, regardless of `targets_per_run`; a
   purchased target whose departure has passed is skipped. Purchased watchlists are polled
   **first** so a wide grid cannot starve a credit-drop check.
4. "Polled" means any ledger row that is not `skipped_breaker` — an erroring target still rotates
   to the back; otherwise one deterministic failure pins the round-robin and burns the budget.
5. `poll.times_per_day` is **enforced**: a watchlist that already has that many runs today (UTC,
   distinct `run_id`s with a non-skipped call) is skipped with a log line, so `fares budget`'s
   arithmetic is what the runner actually spends.
6. A breaker trip between two providers of one target still evaluates the rows already paid for
   before the run stops.
7. `fares poll` on the live DB refuses to register an Amadeus client on a non-production host, and
   refuses to run with no providers at all; `--allow-live-db` overrides the former only.
8. Do not schedule cron before the notifier exists (Phase 6): once-only alerts (FENCE_WARNING,
   DOT_24H) are gated on *insertion*, and an undelivered insert still spends the gate.

**Providers and the breaker.** Every call to any provider is one unit under the single breaker;
adding `amadeus` to a watchlist's `providers` doubles that watchlist's calls. Amadeus's free
production quota is separate from SerpAPI's; a per-provider breaker is a Phase 5 option if it is
turned on.

**Budget arithmetic.** `fares budget` prints `WatchlistFile.calls_per_day()` next to
`MAX_DAILY_API_CALLS`. A grid of *N* targets at `targets_per_run = k`, `times_per_day = 2`
refreshes each target every `ceil(N / 2k)` days and costs `2k` calls/day per provider. The
example config asks for 12/day; SerpAPI's free tier (100/month) covers 3. Flexible grids are
a paid-tier feature, or a one-target-at-a-time feature. Pick before Phase 4.

**Seed isolation.** `fares seed` writes synthetic history (45 days per target, with a dip day, a
Basic Economy trap and the 21/14 fence steps, clamped into whatever DTD range the target has) to
**`data/seed.db`**, never `FARES_DB_PATH`, and refuses the live path without `--allow-live-db`.
With no `--config` it generates three demo watchlists whose departures sit 10, 30 and 75 days out
from `--end` (so all regimes appear) and writes them to `config/watchlists.demo.yaml`; the Phase 3
dashboard is pointed at that pair. Seed rows carry `provider=mock`, `trigger=seed`,
`cost_units=0`; `fares seed --replace` deletes them (cascade) before regenerating, and `fares seed`
refuses to run on top of existing seed rows without it.

`fares targets [watchlist_id]` prints the expanded grid with each target's regime and
last-polled time. `fares key <observation_id>` prints that observation's `itinerary_key` and
`target_id` so you can paste both into `purchase:`.

**Clocks.** The DB boundary rejects naive datetimes (`ValueError`); every stamp is tz-aware and
stored as UTC ISO text. `days_to_departure` is computed from the **UTC** date, so an evening
Pacific poll (00:20Z next day) reads one day lower than the morning poll on the same local day.
That errs early on the 65/21/14 fences, which is the conservative direction; it is documented
here rather than fixed with a per-airport timezone table.

## 7a. Alerts (src/alerts/notifier.py)

One channel: an [ntfy](https://ntfy.sh) topic, configured by `NTFY_SERVER` / `NTFY_TOPIC` /
`NTFY_TOKEN`. One `httpx.post` per alert. No bots, no OAuth, phone app subscribes to the topic.

Mapping from `Alert` to the ntfy request:

| `AlertKind` | priority | tags | title |
|---|---|---|---|
| `BUY` | 5 (urgent) | `airplane,moneybag` | `BUY SEA→SJC $287 (DTD 34, prime)` |
| `EXTREME_LOW` | 4 (high) | `airplane,eyes` | `Anomaly SEA→ORD $212 (DTD 88, monitor)` |
| `FENCE_WARNING` | 3 | `hourglass` | `21-day fence SEA→SJC — buy window closing` |
| `CREDIT_DROP` | 4 | `airplane,receipt` | `Re-fare SEA→JFK: now $368, paid $418` |
| `DOT_24H` | 4 | `alarm_clock` | `DOT 24h window on SEA→JFK closes 09:30 tomorrow` |
| `BUDGET_TRIPPED` | 3 | `warning` | `API budget tripped (4/4 today)` |
| `PROVIDER_ERROR` | 2 | `x` | `serpapi failed for sea-sjc-thanksgiving` |

`Title` header = the first line of `Alert.message`, with `→` and dashes transliterated and any
other non-latin-1 character dropped (HTTP headers are latin-1). Body = everything after the first
line (the title again for a one-line message), UTF-8: route, dates, price vs reference, brand,
stops, provider + observed time. `Click` header = the `booking_url` of the observation behind the
alert (`Notifier.send(alert, click=…)`; the runner looks it up through `alert.observation_id`). `Markdown: yes` is
**not** used — the iOS app renders it inconsistently.

Rules:
* `delivered_at` and `channel="ntfy"` are written in the same transaction as the alert row,
  after a 2xx. A non-2xx leaves `delivered_at` null; the start of the next run retries undelivered
  alerts younger than 24 h (`RETRY_WINDOW_HOURS`), oldest first, then gives up and logs the count.
* The notifier never composes a message; `rules.py` does. The notifier formats and ships.
* `fares alert-test` sends one `PROVIDER_ERROR`-priority ping (touching no DB) so you can confirm
  the phone subscription before Phase 6 goes live. `fares health` is the external watchdog
  (`src/runner/health.py`): no non-seed run within `--max-age-hours` (default 36), newest run
  errored, a run `running` for over 2 h, every API call in the window failed, calls succeeded but
  stored no fares, undelivered alerts in the window, newest backup older than 30 h, under 200 MB
  free. It sends one priority-4 message listing every problem with a `Fix:` line and exits 1; it
  stores nothing.
* `fares poll` pings `fare tracker: poll failed` (priority 4, with a fix) when it cannot load the
  config, has no providers, refuses the live DB, or raises. Operational pings pass `priority=` to
  `Notifier.send`; stored alerts never do. `PROVIDER_ERROR` alert bodies end with a `Fix:` line
  (`poll.provider_fix`: credentials, quota, transient, bad request).
* `scripts/notify.sh` is the shell fallback for what Python cannot report: `backup_db.sh` failing,
  or `fares` exiting with anything but 0/1/2 from cron (the venv is gone).
* `fares poll --notifier ntfy|log|null`; the default is ntfy when `NTFY_TOPIC` is set, else null
  with a warning.
* Public ntfy topics are world-readable by name. Alerts carry route, dates, prices — nothing
  secret — so a public topic is acceptable. Rotate to a reserved topic + token if that changes.

## 8. Dashboard (dashboard/app.py)

Streamlit + Plotly, read-only against a DB + watchlist YAML pair (`data/seed.db` +
`config/watchlists.demo.yaml` during development, `data/fares.db` + `config/watchlists.yaml` live).
Three views, per watchlist:

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
| 4 | `airports.py`, `serpapi_client.py`, `amadeus_client.py`, `risk_filters.py` | TESTER → 3× BUILDER (parallel worktrees) → JANITOR | yes |
| 5 | `rules.py` (regimes, strikes, post-purchase), `fares poll` end-to-end on mock | ARCHITECT (entry gate, below) → TESTER → BUILDER → JANITOR | yes |
| 6 | `notifier.py` (ntfy), `fares alert-test`, cron + backup scripts, first live run with real keys | BUILDER → JANITOR | no |
| 7? | internal statistics, only if ≥ 90 days of data across ≥ 3 routes says they add signal | ARCHITECT decides | yes |

Branch per phase (`phase-N-<slug>`), PR into `main`, CI green + REVIEWER pass before merge on the
phases marked yes.

**Phase 5 entry gate (resolved):** `RiskVerdict.inbound_unverified` (migration `002`, one nullable
column, no backfill) is set by `risk_filters` when `target.is_round_trip and len(itinerary.slices)
== 1`. §3: a BUY on such a row is prefixed `[return not checked]`. Decided by the orchestrator
rather than the architect because a nullable add is a two-way door (architect role file, "what
counts as a one-way door").

## 10. Risk matrix (what can go wrong, what stops it)

| Failure | Mitigation | Where |
|---|---|---|
| Basic Economy reported as a price drop | brand normalization; reject by default | risk_filters |
| Unknown brand treated as Main Cabin | `brand_unverified`; cannot BUY alone | models + rules |
| Phantom fare (aggregator cache stale) | second provider; alerts carry provider + observed_at; booking link opened by a human | ingestion |
| Runaway API spend | two-gate breaker in the same transaction as the call log | db |
| Toxic schedule | stops / layover / overnight / co-terminal / time-of-day filters | risk_filters |
| Duplicate alerts | `insert_alert` refuses a repeat `alert_key` inside `dedup_window_hours`; index on `(alert_key, created_at)` | db + notifier |
| Stats on too little data | no internal σ at all; `lowest_seen` is the only internal number | rules |
| Grid explosion starves the budget | `MAX_TARGETS_PER_WATCHLIST = 60` at load; `fares budget` before commit; round-robin guarantees every target is eventually polled | models + runner |
| Flexible grid buys the wrong leg | `purchase.target_id` must be one of the watchlist's targets (validated); post-purchase polls only it | models |
| Test-env prices polluting history | Amadeus test host forced to `provider=mock` | amadeus_client |
| Runner host dies silently | `BUDGET_TRIPPED`/`PROVIDER_ERROR` alerts; cron log; `fares health` alerts on no run in 36h, errored or killed runs, all calls failing, no fares, undelivered alerts, stale backups, low disk | runner (Phase 6) |
| Poll or backup fails outright | `fares poll` pings `poll failed` with a fix; `backup_db.sh` and cron fall back to `scripts/notify.sh` | runner + scripts |
| Alert sent but never seen | ntfy priority ≥ 4 for anything price-driven; `fares alert-test` before go-live | notifier |
| Losing the DB | nightly `backup_db.sh` to a second disk / cloud bucket | scripts |
