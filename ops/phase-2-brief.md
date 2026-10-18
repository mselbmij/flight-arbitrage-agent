# Phase 2 brief — database layer, mock provider, seeder, CLI

Governing sections of ARCHITECTURE.md: §2 (data model), §3 (target lifecycle: only the
`lowest_seen` and `alert_key` rules), §5 (provider protocol), §6 (circuit breaker), §7 (runner:
`fares seed`, `fares targets`, `fares budget`, `fares key`; round-robin "last polled" query).

Everything below is the public interface. The tester writes against it; the builders implement
it. A builder who needs to deviate reports `NEEDS ARCHITECT` and does not deviate.

## src/db.py

```python
class BudgetLimits(BaseModel):            # pydantic, frozen
    max_per_run: int                       # from MAX_CALLS_PER_RUN
    max_per_day: int                       # from MAX_DAILY_API_CALLS

class BudgetTripped(Exception):
    def __init__(self, gate: Literal["run", "day"], used: int, limit: int): ...

class Database:
    def __init__(self, path: str | Path, *, now: Callable[[], datetime] = utcnow): ...
    # context manager → connection open; WAL mode; foreign_keys=ON; closes on exit
    # Naive datetimes are rejected at this boundary: every write or comparison of a datetime (and
    # the result of `now()`, checked on each call) raises ValueError("naive datetime").
    def migrate(self) -> list[str]         # applies src/migrations/NNN_*.sql in order, records each
                                           # in schema_migrations(version, applied_at); returns the
                                           # versions applied this call; idempotent. Per migration:
                                           # BEGIN IMMEDIATE, read applied versions, apply + record,
                                           # COMMIT, so two racing processes cannot both apply it.

    # runs
    def start_run(self, run: Run) -> None
    def finish_run(self, run_id: str, status: str, error: str | None = None) -> None
    def get_run(self, run_id: str) -> Run | None

    # api calls + breaker  (§6)
    def reserve_api_call(self, call: ApiCall, limits: BudgetLimits) -> int
        # ONE transaction: read SUM(cost_units) for today (UTC) with status != skipped_breaker,
        # read runs.calls_made for call.run_id; if either gate would be exceeded by call.cost_units,
        # insert the row with status=skipped_breaker, commit, raise BudgetTripped. Otherwise insert
        # the row with status forced to ok whatever the caller passed (it is updated by
        # complete_api_call; called_at stays as supplied, runner and DB share a clock),
        # increment runs.calls_made, commit, return the row id.
    def complete_api_call(self, call_id: int, status: ApiCallStatus, latency_ms: int | None,
                          error: str | None = None) -> None
    def calls_today(self) -> int           # SUM(cost_units) where called_at >= today 00:00 UTC
                                           # and status != skipped_breaker

    # observations / bands
    def insert_observations(self, obs: list[FareObservation]) -> list[int]   # one transaction
    def insert_band(self, band: BenchmarkBand) -> int
    def observations_for(self, target_id: str, *, since: datetime | None = None,
                         passed_only: bool = False) -> list[FareObservation]
    def lowest_seen(self, target_id: str) -> int | None     # min price_cents, risk_passed=1 and
                                                             # risk_brand_unverified=0 only: an
                                                             # unverified brand is never a strike
                                                             # reference
    def latest_band(self, target_id: str) -> BenchmarkBand | None
    def last_polled(self, target_ids: list[str]) -> dict[str, datetime | None]
        # MAX(api_calls.called_at) per target_id where status=ok; missing → None

    # alerts
    def insert_alert(self, alert: Alert, *, dedup_window_hours: int = 24) -> int | None
        # None (no insert) if an alert with the same alert_key has created_at > new.created_at -
        # window (strict: exactly one window apart is allowed).
        # alerts.alert_key is NOT unique across time (ARCHITECTURE §3); dedup is enforced here.
    def alert_kinds_for(self, target_id: str) -> set[AlertKind]
        # distinct kinds ever inserted for this target_id, of any age; watchlist-level alerts
        # (target_id NULL) are excluded. Added in phase 5: gates FENCE_WARNING / DOT_24H.
    def mark_delivered(self, alert_id: int, channel: str) -> None
    def undelivered_alerts(self, *, max_age_hours: int) -> list[Alert]
        # created_at > now - max_age (strict, the same shape as the dedup window)

    # dashboard / cli helpers
    def observation(self, obs_id: int) -> FareObservation | None
    def delete_runs_by_trigger(self, trigger: str) -> int   # one transaction: the runs with this
                                           # trigger plus their api_calls, observations, bands and
                                           # the alerts attached to those observations; returns the
                                           # number of runs deleted. Used by `fares seed --replace`.
    def counts(self) -> dict[str, int]     # {"runs":…, "api_calls":…, "observations":…, "benchmark_bands":…, "alerts":…}
```

Storage rules: `itinerary` is stored as JSON text (`itinerary_json`) and rebuilt via
`Itinerary.model_validate_json`; `risk` is flattened to the four `risk_*` columns; enums are stored
as their string values; datetimes are ISO-8601 text with offset; money is INTEGER. Reading back must
yield a model equal to the one inserted (minus `id`, which is filled).

Migrations: `src/migrations/001_initial.sql` creates all five tables + `schema_migrations` + the
indexes in §2. SQL only; the Python runner is in `db.py`.

## src/ingestion/base.py

```python
class ProviderError(Exception):
    def __init__(self, provider: Provider, message: str, *, status_code: int | None = None,
                 retryable: bool = False): ...

class ProviderResult(BaseModel):          # frozen
    observations: list[FareObservation]
    band: BenchmarkBand | None
    raw: dict[str, Any]                   # the payload as received; runner writes it to data/raw/

class FlightProvider(Protocol):
    name: Provider
    def search(self, target: SearchTarget, watchlist: ResolvedWatchlist, run_id: str,
               observed_at: datetime) -> ProviderResult: ...
```

`observed_at` is passed in so one run stamps every observation with the same instant.

## src/ingestion/mock_client.py

```python
class MockProvider:
    name = Provider.MOCK
    def __init__(self, fixture_dir: Path): ...
    def search(self, target, watchlist, run_id, observed_at) -> ProviderResult
        # loads <fixture_dir>/<target_id with ':' → '_'>.json; if absent, raises
        # ProviderError(MOCK, "no fixture", retryable=False).
        # Fixture shape: {"observations": [FareObservation-without-id/run_id/observed_at …],
        #                 "band": BenchmarkBand-without-id/run_id/observed_at | null,
        #                 "raw": {...}}
        # The client fills run_id and observed_at, and sets days_to_departure from target.depart
        # and observed_at. provider on every row is forced to MOCK regardless of fixture.
        # watchlist_id / target_id are forced from the requested target (fixtures may be stale)
        # and risk is forced to None on every observation (the verdict is the engine's job).
        # A fixture that is malformed (bad JSON, missing key, fails model validation, unreadable)
        # raises ProviderError(MOCK, "bad fixture: ...", retryable=False).
```

## src/runner/seed.py  (synthetic history generator — pure, no DB)

```python
class SeedScenario(BaseModel):            # frozen
    target: SearchTarget
    days: int = 45                         # observations: one per day, ending at `end`
    end: datetime                          # UTC; last observation timestamp
    base_price_cents: int                  # price at DTD=65
    typical_low_cents: int
    typical_high_cents: int
    fence_jump_pct: float = 0.18           # price step up when DTD crosses 21 (and again at 14, half)
    dip_at_dtd: int | None = 34            # one-day dip to 0.78 × curve → the "buy" day
    basic_economy_trap_at_dtd: int | None = 30   # a BE fare 22% cheaper that must be risk-rejected
    seed: int = 7                          # deterministic noise
    # The CLI's _scenario clamps dip_at_dtd / basic_economy_trap_at_dtd into the DTD range the
    # history covers (dtd_at_end .. dtd_at_end + days - 1): a configured value outside that range
    # becomes dtd_at_end + 4 (dip) / + 8 (trap), capped at the range end, so a far-out real target
    # still gets both.

def generate(scenario: SeedScenario, *, watchlist: ResolvedWatchlist, run_id_prefix: str = "seed") \
        -> tuple[list[Run], list[ApiCall], list[FareObservation], list[BenchmarkBand]]
    # One Run + one ApiCall (provider=MOCK, status=ok, cost_units=0 — never consumes real budget) per day.
    # Each day: 2–3 observations (one nonstop main_cabin, one 1-stop main_cabin cheaper, and on the
    # trap day a basic_economy). Itineraries are built with real-looking Segments (tz-aware local
    # times, 2-letter carriers from a fixed table keyed on origin/destination).
    # Price curve: base × (1 + 0.004 × (65 − dtd)) for dtd ≤ 65, i.e. slow drift up; step at 21 and 14.
    # ±3% deterministic noise from `seed`.
    # Every observation gets a RiskVerdict computed by a LOCAL, minimal rule (BE → rejected,
    # everything else passed) because risk_filters.py does not exist yet; config_hash = "seed".
    # One BenchmarkBand per day with lowest_price = that day's cheapest passed, typical_* from scenario.
```

## src/runner/demo.py  (demo watchlists for `fares seed` without --config)

```python
def demo_watchlist_file(end: datetime) -> WatchlistFile
    # origin SEA, departures relative to end.date(), everything else Defaults():
    #   demo-late    → SJC, one-way, depart end+10, ceiling 24000 (45 days of history covers
    #                  DTD 10..54: dip 34, trap 30 and both fence steps)
    #   demo-prime   → [SFO, OAK], depart end+30, trip_length 3..3, ceiling 26000
    #   demo-monitor → ORD, depart end+75, return end+79, ceiling 38000 (dip/trap clamped)
def write_demo_config(path: Path, wf: WatchlistFile) -> None
    # YAML in the shape of config/watchlists.yaml (version, defaults, watchlists; dates as ISO
    # strings; model_dump(mode="json", by_alias=True, exclude_none=True)); re-loads to an equal
    # WatchlistFile, so the Phase 3 dashboard can load the db + config pair.
```

## src/runner/cli.py  (Typer app; entry point `fares`)

```
fares migrate [--db PATH]                      → applies migrations, prints versions applied
fares seed    [--db PATH] [--config PATH] [--days 45] [--end ISO] [--replace]
              [--write-config PATH] [--allow-live-db]
                                               → generate() for every pollable target of every
                                                 watchlist, insert, print counts. --db defaults to
                                                 data/seed.db (NOT FARES_DB_PATH). --config is
                                                 optional: omitted → demo_watchlist_file(end),
                                                 also written to config/watchlists.demo.yaml
                                                 (--write-config); given → that grid is seeded and
                                                 no demo file is written. Exits 1 if the resolved
                                                 --db equals the resolved FARES_DB_PATH (default
                                                 data/fares.db) unless --allow-live-db: synthetic
                                                 rows must never become a live strike reference.
fares targets [WATCHLIST_ID] [--config PATH] [--db PATH]
                                               → table: target_id, regime, DTD, last_polled (or "-")
fares budget  [--config PATH]                  → "calls/day: N  limit: M  (OVER|ok)"  exit 1 if over
fares key OBS_ID [--db PATH]                   → prints itinerary_key and target_id of that observation
```

Defaults: `--db` from `FARES_DB_PATH` (env, via python-dotenv, default `data/fares.db`) for every
command except `seed`, whose `--db` defaults to `data/seed.db`; `--config` default
`config/watchlists.yaml` for every command except `seed`, which falls back to the demo watchlists. Limits from `MAX_CALLS_PER_RUN`, `MAX_DAILY_API_CALLS`.
`fares migrate` is implied by every other command (call `migrate()` before use).

## Test file plan (tester owns)

* `tests/test_db.py` — migrations idempotent; round-trip every model; breaker: run gate, day gate,
  skipped row written and `BudgetTripped` raised, `calls_today` ignores skipped rows, day boundary
  at 00:00 UTC (freezegun); `lowest_seen` ignores rejected; `last_polled` nulls; `insert_alert` dedup
  inside / outside window; `undelivered_alerts`.
* `tests/test_mock_provider.py` — fixture load, forced provider=MOCK, days_to_departure computed,
  missing fixture → ProviderError.
* `tests/test_seed.py` — determinism (same seed → same prices); exactly one BE row, rejected;
  dip day is the min passed price; price at DTD 20 > price at DTD 22 by ≥ fence_jump_pct − noise;
  all datetimes aware; band per day; counts.
* `tests/test_cli.py` — Typer `CliRunner`: `budget` exit codes; `seed` then `targets` shows
  last_polled; `key` on a seeded observation; `seed` without `--config` yields one rejected BE
  observation per target with the min passed price on the dip day; live-db refusal.
* `tests/test_demo.py` — demo watchlists, relative dates, YAML round trip.
* `tests/fixtures/mock/` — at least one fixture matching a target_id from `config/watchlists.yaml`
  (the Thanksgiving one), and `tests/fixtures/README.md` entries.
