# Phase 3 brief — dashboard on seed data

Governing: ARCHITECTURE.md §8 (dashboard), §3 (regimes, `lowest_seen`), §7 (seed isolation,
budget). Reads a DB + watchlist YAML pair; during development that is `data/seed.db` +
`config/watchlists.demo.yaml` (run `fares seed` first).

Two files. `dashboard/data.py` is pure (DB + yaml in, DataFrames/records out) and fully tested.
`dashboard/app.py` is the Streamlit shell and is thin: layout, widgets, Plotly figures built
from `data.py` output. Nothing in `app.py` runs SQL.

## dashboard/data.py

```python
class DashboardSource(BaseModel):          # frozen
    db_path: Path
    config_path: Path

def load(source: DashboardSource) -> "Loaded"   # opens Database, loads WatchlistFile, returns both
class Loaded:                                   # plain dataclass
    wf: WatchlistFile
    resolved: list[ResolvedWatchlist]
    db: Database

def target_table(loaded, now: datetime) -> pd.DataFrame
    # one row per target: watchlist_id, target_id, origin, destination, depart, return_, regime,
    # dtd, cheapest_passed_cents (lowest_seen), strike_reference_cents (per §3 using latest band,
    # None if no band), strike_multiplier, in_strike_zone (bool|None), last_polled, n_obs, n_rejected
def price_series(loaded, target_id) -> pd.DataFrame
    # one row per observation: observed_at, dtd, price_cents, fare_brand, stops, provider,
    # risk_passed, risk_rejections (joined str), brand_unverified, itinerary_key
def band_series(loaded, target_id) -> pd.DataFrame
    # observed_at, typical_low_cents, typical_high_cents, lowest_price_cents, price_level
def budget_today(loaded, limits: BudgetLimits, now) -> dict   # {"used": int, "limit": int, "calls_per_day": int}
def alerts_recent(loaded, *, limit: int = 20) -> pd.DataFrame
def strike_reference(wl: ResolvedWatchlist, target, band: BenchmarkBand | None, lowest_seen: int | None) -> tuple[int | None, float, str]
    # (reference_cents, multiplier, reference_name) for the target's current regime; POST_PURCHASE
    # and EXPIRED → (None, 0, "-"). `ceiling` reference → wl.ceiling_cents. `lowest_seen` → the arg.
```

All money columns are int cents in the frame; formatting to `$` happens in app.py only.

## dashboard/app.py

Sidebar: DB path + config path (defaults `data/seed.db`, `config/watchlists.demo.yaml`; env
`FARES_DB_PATH`/`FARES_CONFIG_PATH` override), watchlist selector, target selector (within the
watchlist), "show rejected fares" toggle (default on), theme follows Streamlit.

Main, top to bottom:
1. **Status strip** — four stat tiles: targets watched, cheapest passed fare for the selected
   target (with its regime), API budget today `used / limit`, last run time. Plain numbers, no
   sparkline.
2. **Chart 1 — price vs time** for the selected target. Points = observations; passed fares are
   filled markers, rejected are hollow (open) markers of the same hue; the benchmark band
   (`typical_low..typical_high`) is a shaded ribbon; `ceiling` a dashed horizontal line; the
   current strike reference × multiplier a dotted horizontal line. Hover shows price, brand,
   stops, provider, rejection reasons.
3. **Chart 2 — price vs days-to-departure**, same data, x reversed (DTD decreasing to the
   right), vertical dashed lines at 65 / 21 / 14 / 7 labeled with the regime names.
4. **Target table** — `target_table()` for the selected watchlist, sortable, cents formatted as
   dollars, `in_strike_zone` as ✓ / – / ?.
5. **Recent alerts** — `alerts_recent()` (empty in Phase 3; render the empty state cleanly).

## Chart rules (from the dataviz skill; not negotiable)

* One y-axis per chart. Never a secondary axis.
* Color by **entity**, fixed slots, never cycled: passed fares = slot 1 blue `#2a78d6`, rejected
  fares = same blue, hollow; benchmark ribbon = slot 1 at 12% alpha; ceiling line = status
  `critical` `#d03b3b` dashed; strike line = slot 3 aqua `#1baf7a` dotted; regime fences =
  `#52514e` (text-secondary) dashed. Dark mode values: blue `#3987e5`, aqua `#199e70`; detect via
  `st.get_option("theme.base")` and pick the set.
* Text (labels, legend, tick marks) is ink, never series color.
* Markers ≥ 8px; lines 2px; grid recessive (light gray, no zero-line emphasis).
* Legend present whenever ≥ 2 series are drawn (passed / rejected / band / ceiling / strike).
* Direct-label only the dip (global min passed) on Chart 1 — not every point.
* Hover layer on by default (Plotly's unified hover is fine).
* No rainbow, no gradient fills, no 3D, no dual axis, no pie.
* `plotly_white` template with the grid dimmed; Plotly's default qualitative palette is NOT used.

## Tests (tests/test_dashboard_data.py)

Seed a tmp DB with `fares seed --db tmp --write-config tmp.yaml` via `CliRunner` in a fixture
(or call `generate()` directly) and assert on `data.py`:
* `target_table` has one row per target, regimes match `SearchTarget.regime(now)`,
  `cheapest_passed_cents` equals `Database.lowest_seen`, `n_rejected` ≥ 1 per target.
* `strike_reference` for each regime picks the right reference and multiplier from the
  watchlist's `StrikeConfig`; `ceiling` resolves to `wl.ceiling_cents`; POST_PURCHASE → None.
* `price_series` is sorted by `observed_at`, has exactly one `basic_economy` row per target,
  and `risk_passed` is False for it.
* `band_series` row count equals days seeded.
* `budget_today` with seed data is `used == 0` (seed calls cost nothing).
* Money columns are integer dtype.
No Streamlit tests. `app.py` is checked by the janitor rendering it headless once
(`streamlit run dashboard/app.py --server.headless true` for ~10s, then kill, check the log for
tracebacks) and by me opening a screenshot.
