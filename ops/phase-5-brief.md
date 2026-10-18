# Phase 5 brief — rules engine + poll runner

Governing: ARCHITECTURE.md §3 (target lifecycle, BUY preconditions, alert_key, one-alert-per-target),
§6 (breaker), §7 (runner: target selection, round-robin, budget), §7a (alert titles/bodies; the
*notifier* is Phase 6 but the *message text* is composed here), §10.
Interfaces from earlier briefs: `Database` (phase-2), `FlightProvider`/`ProviderResult`/`ProviderError`
(phase-2/4), `risk_filters.apply` (phase-4).

Two modules. `src/engine/rules.py` is pure: no I/O, no clock reads, no DB. `src/runner/poll.py`
owns every side effect: DB, providers, raw files, the notifier hook. The CLI gains `fares poll`.

## src/engine/rules.py

```python
class TargetState(BaseModel):            # frozen; what the runner reads from the DB for one target
    band: BenchmarkBand | None           # Database.latest_band(target_id)
    lowest_seen_cents: int | None        # Database.lowest_seen(target_id) — BEFORE this run's rows
    already_alerted: frozenset[AlertKind]  # kinds ever inserted for this target (FENCE_WARNING / DOT_24H gating)

def strike_reference(wl, target, regime, state) -> tuple[int | None, float, str]
    # (reference_cents, multiplier, name) per wl.strike_rule(regime):
    #   typical_low / typical_high → state.band.* (None if no usable band)
    #   lowest_seen → state.lowest_seen_cents; ceiling → wl.ceiling_cents
    # MONITOR..LATE only; POST_PURCHASE / EXPIRED → (None, 0.0, "-")
    # (dashboard/data.py has an equivalent; it will be switched to call this one — do not import dashboard from src)

def evaluate(wl: ResolvedWatchlist, target: SearchTarget, observations: list[FareObservation],
             state: TargetState, *, now: datetime, run_id: str, config_hash: str) -> list[Alert]
```

`observations` are THIS run's rows for this target with `.risk` set and `.id` filled. Returns
alerts with `delivered_at=None`, `channel=None`, `id=None`, `created_at=now`.

Rules (§3), in this order; at most ONE price-driven alert per call:

1. `regime = wl.regime_for(target, now)`. `EXPIRED` → `[]`.
2. **POST_PURCHASE** (`wl.purchase` set): consider only observations with
   `itinerary_key == purchase.itinerary_key` and `risk.passed`. Let `cheapest` be the min price.
   - `CREDIT_DROP` if `purchase.changeable and cheapest.price_cents <= purchase.price_cents - post_purchase.credit_threshold_cents`.
     `reference_cents = purchase.price_cents`.
   - `DOT_24H` if `now - purchase.purchased_at < dot_window_hours` and the purchase was made ≥ 7 days
     before `target.depart` (DOT rule) and `AlertKind.DOT_24H not in state.already_alerted`.
     Not price-driven; may accompany a CREDIT_DROP. `price_cents=None`.
   - Nothing else fires in POST_PURCHASE.
3. **FENCE entry**: if `regime is FENCE and FENCE_WARNING not in state.already_alerted` → one
   `FENCE_WARNING` (`price_cents=None`, `reference_cents=None`). May accompany a BUY.
4. **Strike** (MONITOR / PRIME / FENCE / LATE): candidates = observations with `risk.passed` and
   NOT (`risk.brand_unverified and fare_brand_raw is None`). `ref, mult, name = strike_reference(...)`.
   A candidate qualifies if `price_cents <= wl.ceiling_cents` OR (`ref is not None and price_cents <= ref * mult`,
   integer compare: `price_cents * 100 <= ref * int(mult * 100)` — no float money). Pick the cheapest
   qualifying candidate. Kind: `EXTREME_LOW` in MONITOR, `BUY` otherwise. `reference_cents` = the
   bound that was hit: `wl.ceiling_cents` if that was the satisfied test (prefer the band test when
   both hold — the band is the informative one; report `ref` then).
5. Message prefixes, in this order, space-separated, each only if applicable:
   `[brand unverified]` when `risk.brand_unverified`; `[return not checked]` when `risk.inbound_unverified`.
6. `alert_key` (§3): `f"{target.target_id}:{kind.value}:{itinerary_key or '-'}:{price_cents // 1000 if price_cents else 0}"`.
   `BUDGET_TRIPPED` / `PROVIDER_ERROR` are not produced here (runner) and use `watchlist_id` for the first field.

Message text (§7a), title + body; the notifier (Phase 6) splits on the first newline:
```
BUY SEA→SJC $287 (DTD 34, prime)
Nov 25 – Nov 29 · AS 1234 nonstop · Main Cabin · 7:15a–9:25a
$287 vs typical-low $310 × 1.00 (ceiling $320) · serpapi 06:52 UTC
```
Helpers: `fmt_money(cents) -> "$287"` (no cents when `.00`, else `$287.50`), `fmt_route`, `fmt_itin`
(carrier+number per segment, "nonstop"/"1 stop via SLC"), local times `%-I:%M%p` lower-case.
Titles exactly as the §7a table (BUY / Anomaly / 21-day fence / Re-fare / DOT 24h window / etc.).

## src/runner/poll.py

```python
class Notifier(Protocol):
    def send(self, alert: Alert) -> str | None   # returns channel name on success, None on failure/no-op
class NullNotifier:                              # Phase 5 default: logs, returns None (alert stays undelivered)
class ProviderRegistry:                          # name -> FlightProvider; built from env in cli; injectable
def select_targets(wl: ResolvedWatchlist, last_polled: dict[str, datetime | None], *, now) -> list[SearchTarget]
    # §7: purchased → [that one target]; else pollable, sorted by (last_polled is not None, last_polled, dtd), take wl.poll.targets_per_run
    # scan_provider: if set, log "scan tier not implemented" and ignore (honest no-op)
def run_poll(db: Database, wf: WatchlistFile, providers: ProviderRegistry, *, limits: BudgetLimits,
             notifier: Notifier, raw_dir: Path, trigger: Literal["cron","manual"], now: Callable[[], datetime],
             run_id: str | None = None) -> Run
```

`run_poll`, per ARCHITECTURE §6/§7, in this order:
1. `db.migrate()`; `start_run(Run(run_id=uuid4-hex, started_at=now(), trigger))`; `config_hash = wf.config_hash()`.
2. For each `wl in wf.resolved()` with status `watching` or `purchased`, for each `target in select_targets(...)`, for each `provider_name in wl.poll.providers` (skip with a logged warning if the registry lacks it):
   a. `call = ApiCall(run_id, called_at=now(), provider, endpoint=f"search:{origin}{dest}", watchlist_id, target_id, status=OK, cost_units=1)`;
      `call_id = db.reserve_api_call(call, limits)`. On `BudgetTripped`: insert ONE `BUDGET_TRIPPED` alert
      (`watchlist_id`-keyed, `message=f"API budget tripped ({e.used}/{e.limit} {e.gate})"`), notify it,
      `finish_run(status="budget_tripped")`, **return** — nothing else is polled this run.
   b. `t0 = now(); result = provider.search(target, wl, run_id, observed_at=t0)`.
      `ProviderError` → `complete_api_call(call_id, ERROR, latency, error=str(e))`; insert a `PROVIDER_ERROR`
      alert (`watchlist_id`-keyed, message includes provider + target + error; dedup handles repeats);
      notify; **continue** to the next target/provider. Any other exception → same ledger update, then re-raise
      after `finish_run(status="error", error=repr)` (a bug must not look like a provider outage).
   c. Success → `complete_api_call(call_id, OK, latency_ms)`. Write `result.raw` to
      `raw_dir / run_id / f"{provider}-{target_id.replace(':', '_')}.json"` (mkdir -p); set
      `raw_payload_ref` on each observation to that path relative to `raw_dir`.
   d. `state = TargetState(band=db.latest_band(target_id), lowest_seen_cents=db.lowest_seen(target_id), already_alerted=kinds from db)` — read BEFORE inserting this run's rows (`lowest_seen` must not include them).
   e. `obs = risk_filters.apply(result.observations, wl, config_hash=...)`; `ids = db.insert_observations(obs)`;
      set `.id` on each (model_copy); `db.insert_band(result.band)` if present.
   f. `alerts = rules.evaluate(wl, target, obs_with_ids, state, now=t0, run_id, config_hash)`; for each:
      `aid = db.insert_alert(a, dedup_window_hours=24)`; if `aid` is not None → `channel = notifier.send(a)`;
      if channel → `db.mark_delivered(aid, channel)`.
3. `finish_run(status="ok")`. Return the `Run` (re-read via `get_run`).

`Database` needs one new read: `alert_kinds_for(target_id) -> set[AlertKind]` (distinct kinds where
`target_id = ?`). Add it to `src/db.py` (and the phase-2 brief). No other DB changes.

CLI (`src/runner/cli.py`): `fares poll [--db] [--config] [--trigger cron|manual=manual] [--provider-override mock --fixtures DIR] [--raw-dir data/raw]`.
Provider registry from env: `SERPAPI_KEY` → `SerpApiProvider`; `AMADEUS_CLIENT_ID/SECRET/HOST` → `AmadeusProvider`;
`--provider-override mock --fixtures DIR` replaces EVERY provider name in every watchlist with a
`MockProvider(DIR)` (so an end-to-end run on fixtures spends nothing — but it still goes through the
breaker, so run it against a scratch DB or seed.db, never the live DB: refuse `FARES_DB_PATH` under
override unless `--allow-live-db`). Limits from `MAX_CALLS_PER_RUN` / `MAX_DAILY_API_CALLS`.
Prints: run id, status, calls made, observations inserted, alerts inserted/delivered, and the
per-target one-liners. Exit 0 on ok, 2 on budget_tripped, 1 on error.

## Tests (tester)

* `tests/test_rules.py` — every rule above with `freezegun`/explicit `now`: regime routing; strike
  on typical_low × mult; ceiling always strikes; integer compare at the exact boundary (price ==
  ref×mult passes; +1 cent fails; mult 1.15 on 31000 → 35650 boundary); cheapest-wins with 3
  qualifying; EXTREME_LOW vs BUY by regime; unverified brand with raw → prefixed BUY; unverified
  brand with raw None → no alert; inbound_unverified prefix; both prefixes stack in order;
  FENCE_WARNING once (gated by `already_alerted`), accompanies a BUY; POST_PURCHASE: CREDIT_DROP at
  exactly threshold / not at threshold−1 / not when `changeable=False` / only for the purchased
  itinerary_key; DOT_24H inside/outside window, <7 days-before-departure purchase → none, gated
  by `already_alerted`; no BUY in POST_PURCHASE; no band + no ceiling hit → none; `alert_key`
  format; message title/body format for each kind (exact strings for 2–3, substrings for the rest);
  `fmt_money` cases.
* `tests/test_poll.py` — `run_poll` with `MockProvider` on a fixture dir and a tmp DB: happy path
  inserts runs/api_calls/observations/band/alerts and writes raw files; `lowest_seen` passed to
  rules excludes this run's rows (construct a case where including them would suppress a BUY);
  breaker trips mid-run → BUDGET_TRIPPED once, status `budget_tripped`, later targets untouched,
  `calls_made` correct; `ProviderError` → ledger ERROR row + PROVIDER_ERROR alert + run continues +
  status ok; unexpected exception → status `error`, re-raised; dedup: second run same prices →
  no new alert rows; notifier returning a channel → `delivered_at` set, returning None → not;
  `select_targets` ordering (never-polled first, then oldest, then DTD asc; purchased → pinned
  target only; paused → none; targets_per_run honoured); raw file path/contents; `config_hash`
  stamped on verdicts.
* `tests/test_cli.py` additions — `fares poll --provider-override mock --fixtures tests/fixtures/mock`
  against a tmp DB: exit 0, prints counts; refuses live DB under override; `--trigger cron` recorded.
