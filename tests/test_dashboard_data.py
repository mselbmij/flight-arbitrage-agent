"""Phase 3: dashboard/data.py (ARCHITECTURE.md sections 3, 7, 8), on `fares seed` data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from dashboard import data
from dashboard.data import DashboardSource, Loaded
from src.db import BudgetLimits, Database
from src.models import (
    AlertKind,
    ApiCall,
    ApiCallStatus,
    BenchmarkBand,
    Provider,
    Regime,
    WatchlistFile,
)
from src.runner.cli import app
from tests.conftest import make_alert, make_run

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
DAYS = 45
LIMITS = BudgetLimits(max_per_run=3, max_per_day=4)

EMPTY_YAML = """
version: 1
watchlists:
  - id: alpha-one
    origin: SEA
    destination: SJC
    depart: 2026-11-25
    return: 2026-11-29
    ceiling_cents: 32000
  - id: bought-one
    origin: SEA
    destination: SFO
    depart: 2026-12-10
    return: 2026-12-14
    ceiling_cents: 30000
    status: purchased
    purchase:
      purchased_at: 2026-10-01T12:00:00+00:00
      price_cents: 28000
      itinerary_key: abcdef1234567890
      target_id: "bought-one:SEASFO:2026-12-10:2026-12-14"
      carrier: AS
"""


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory):
    d = tmp_path_factory.mktemp("dash")
    db_path, cfg = d / "seed.db", d / "demo.yaml"
    result = CliRunner().invoke(
        app,
        ["seed", "--db", str(db_path), "--write-config", str(cfg), "--end", NOW.isoformat()],
    )
    assert result.exit_code == 0, result.output
    loaded = data.load(DashboardSource(db_path=db_path, config_path=cfg))
    yield loaded
    loaded.close()


@pytest.fixture
def empty(tmp_path: Path):
    db_path, cfg = tmp_path / "fares.db", tmp_path / "w.yaml"
    with Database(db_path) as d:
        d.migrate()
    cfg.write_text(EMPTY_YAML)
    loaded = data.load(DashboardSource(db_path=db_path, config_path=cfg))
    yield loaded
    loaded.close()


def _targets(loaded: Loaded):
    return [(w, t) for w in loaded.resolved for t in w.targets]


def assert_integer(df: pd.DataFrame, *cols: str) -> None:
    for c in cols:
        assert pd.api.types.is_integer_dtype(df[c]), (c, df[c].dtype)
        assert df[c].dtype != object


# --------------------------------------------------------------------------- #
# load
# --------------------------------------------------------------------------- #


def test_load_missing_paths_raise(tmp_path: Path, empty: Loaded):
    with pytest.raises(FileNotFoundError):
        data.load(DashboardSource(db_path=tmp_path / "nope.db", config_path=tmp_path / "w.yaml"))
    with pytest.raises(FileNotFoundError):
        data.load(DashboardSource(db_path=empty.db.path, config_path=tmp_path / "nope.yaml"))
    assert not (tmp_path / "nope.db").exists()  # a typo must not create an empty database


def test_load_returns_wf_and_resolved(seeded: Loaded):
    assert [w.id for w in seeded.resolved] == [w.id for w in seeded.wf.watchlists]
    assert len(seeded.resolved) == 3


# --------------------------------------------------------------------------- #
# target_table
# --------------------------------------------------------------------------- #


def test_target_table_one_row_per_target(seeded: Loaded):
    df = data.target_table(seeded, NOW)
    expected = [t.target_id for _, t in _targets(seeded)]
    assert len(expected) == 4  # demo-late 1, demo-prime 2, demo-monitor 1
    assert list(df["target_id"]) == expected
    assert df["target_id"].is_unique


def test_target_table_regimes_match_search_target(seeded: Loaded):
    df = data.target_table(seeded, NOW).set_index("target_id")
    for _, t in _targets(seeded):
        assert df.loc[t.target_id, "regime"] == t.regime(NOW).value
        assert df.loc[t.target_id, "dtd"] == t.days_to_departure(NOW)
    assert set(df["regime"]) == {"late", "prime", "monitor"}


def test_target_table_cheapest_passed_is_lowest_seen(seeded: Loaded):
    df = data.target_table(seeded, NOW).set_index("target_id")
    for _, t in _targets(seeded):
        lowest = seeded.db.lowest_seen(t.target_id)
        assert lowest is not None
        assert df.loc[t.target_id, "cheapest_passed_cents"] == lowest


def test_target_table_counts_and_rejections(seeded: Loaded):
    df = data.target_table(seeded, NOW).set_index("target_id")
    for _, t in _targets(seeded):
        obs = seeded.db.observations_for(t.target_id)
        assert df.loc[t.target_id, "n_obs"] == len(obs)
        assert df.loc[t.target_id, "n_rejected"] == sum(1 for o in obs if not o.risk.passed)
        assert df.loc[t.target_id, "n_rejected"] >= 1
        assert df.loc[t.target_id, "last_polled"] is not pd.NaT


def test_target_table_strike_columns_follow_strike_reference(seeded: Loaded):
    df = data.target_table(seeded, NOW).set_index("target_id")
    for wl, t in _targets(seeded):
        ref, mult, name = data.strike_reference(
            wl, t, seeded.db.latest_band(t.target_id), seeded.db.lowest_seen(t.target_id), now=NOW
        )
        row = df.loc[t.target_id]
        assert row["strike_reference_cents"] == ref
        assert row["strike_multiplier"] == pytest.approx(mult)
        assert row["strike_reference_name"] == name
        assert ref is not None  # seeded bands are usable and ceiling is always set
        assert row["in_strike_zone"] == (row["cheapest_passed_cents"] <= ref * mult)


def test_target_table_money_columns_are_integer(seeded: Loaded):
    df = data.target_table(seeded, NOW)
    assert_integer(df, "cheapest_passed_cents", "strike_reference_cents", "dtd", "n_obs")


def test_target_table_on_empty_db_renders_empty_cells(empty: Loaded):
    df = data.target_table(empty, NOW)
    assert len(df) == 2
    assert (df["n_obs"] == 0).all()
    assert df["cheapest_passed_cents"].isna().all()
    assert df["last_polled"].isna().all()
    assert_integer(df, "cheapest_passed_cents", "strike_reference_cents")
    alpha = df[df["watchlist_id"] == "alpha-one"].iloc[0]
    assert alpha["regime"] == "prime"
    assert alpha["strike_reference_cents"] is pd.NA  # no band yet: typical_low unknown
    assert alpha["in_strike_zone"] is pd.NA


def test_target_table_purchased_watchlist_has_no_strike(empty: Loaded):
    row = data.target_table(empty, NOW).query("watchlist_id == 'bought-one'").iloc[0]
    assert row["regime"] == "post_purchase"
    assert row["strike_reference_cents"] is pd.NA
    assert row["strike_multiplier"] == 0
    assert row["strike_reference_name"] == "-"
    assert row["in_strike_zone"] is pd.NA


# --------------------------------------------------------------------------- #
# strike_reference
# --------------------------------------------------------------------------- #

STRIKE_YAML = {
    "version": 1,
    "defaults": {
        "strike": {
            "monitor": {"reference": "typical_low", "multiplier": 0.9},
            "prime": {"reference": "typical_high", "multiplier": 0.95},
            "fence": {"reference": "lowest_seen", "multiplier": 1.1},
            "late": {"reference": "ceiling", "multiplier": 1.0},
        }
    },
    "watchlists": [
        {
            "id": "strike-test",
            "origin": "SEA",
            "destination": "SJC",
            "depart": {"start": "2026-10-16", "end": "2027-02-01", "weekdays": [0]},
            "ceiling_cents": 31000,
        }
    ],
}


def _target_with_dtd(dtd: int):
    wl = WatchlistFile.model_validate(STRIKE_YAML).resolved()[0]
    # departure = today + dtd, any weekday: build the target directly
    t = wl.targets[0].model_copy(update={"depart": (NOW + timedelta(days=dtd)).date()})
    return wl, t


def _band(low: int | None = 20000, high: int | None = 30000) -> BenchmarkBand:
    return BenchmarkBand(
        watchlist_id="strike-test", target_id="x", observed_at=NOW, provider=Provider.MOCK,
        run_id="r", typical_low_cents=low, typical_high_cents=high, currency="USD",
    )  # fmt: skip


@pytest.mark.parametrize(
    ("dtd", "regime", "expected"),
    [
        (100, Regime.MONITOR, (20000, 0.9, "typical_low")),
        (40, Regime.PRIME, (30000, 0.95, "typical_high")),
        (18, Regime.FENCE, (27500, 1.1, "lowest_seen")),
        (5, Regime.LATE, (31000, 1.0, "ceiling")),
    ],
)
def test_strike_reference_picks_rule_for_regime(dtd, regime, expected):
    wl, t = _target_with_dtd(dtd)
    assert wl.regime_for(t, NOW) is regime
    assert data.strike_reference(wl, t, _band(), 27500, now=NOW) == expected


def test_strike_reference_ceiling_needs_no_band_or_history():
    wl, t = _target_with_dtd(5)
    assert data.strike_reference(wl, t, None, None, now=NOW) == (wl.ceiling_cents, 1.0, "ceiling")


def test_strike_reference_missing_inputs_give_none_with_rule_kept():
    wl, t = _target_with_dtd(100)
    assert data.strike_reference(wl, t, None, 1, now=NOW) == (None, 0.9, "typical_low")
    assert data.strike_reference(wl, t, _band(low=None, high=None), 1, now=NOW) == (
        None, 0.9, "typical_low",
    )  # fmt: skip
    wl, t = _target_with_dtd(18)
    assert data.strike_reference(wl, t, _band(), None, now=NOW) == (None, 1.1, "lowest_seen")
    wl, t = _target_with_dtd(40)
    assert data.strike_reference(wl, t, None, 5, now=NOW) == (None, 0.95, "typical_high")


def test_strike_reference_expired_is_none():
    wl, t = _target_with_dtd(-1)
    assert data.strike_reference(wl, t, _band(), 27500, now=NOW) == (None, 0, "-")


def test_strike_reference_post_purchase_is_none(empty: Loaded):
    wl = next(w for w in empty.resolved if w.id == "bought-one")
    assert data.strike_reference(wl, wl.targets[0], _band(), 27500, now=NOW) == (None, 0, "-")


def test_strike_reference_defaults_to_the_real_clock():
    wl, t = _target_with_dtd(5)
    far = t.model_copy(update={"depart": datetime.now(UTC).date() + timedelta(days=400)})
    assert data.strike_reference(wl, far, _band(), None) == (20000, 0.9, "typical_low")


# --------------------------------------------------------------------------- #
# price_series / band_series
# --------------------------------------------------------------------------- #


def test_price_series_sorted_with_expected_columns_and_dtypes(seeded: Loaded):
    for _, t in _targets(seeded):
        df = data.price_series(seeded, t.target_id)
        assert len(df) == len(seeded.db.observations_for(t.target_id))
        assert list(df.columns) == [
            "observed_at", "dtd", "price_cents", "fare_brand", "stops", "provider",
            "risk_passed", "risk_rejections", "brand_unverified", "itinerary_key",
        ]  # fmt: skip
        assert df["observed_at"].is_monotonic_increasing
        assert str(df["observed_at"].dt.tz) == "UTC"
        assert df["price_cents"].dtype == "int64"
        assert_integer(df, "dtd", "stops")
        assert set(df["provider"]) == {"mock"}
        assert df["risk_passed"].dtype == bool


def test_price_series_one_basic_economy_row_per_target_and_it_is_rejected(seeded: Loaded):
    for _, t in _targets(seeded):
        df = data.price_series(seeded, t.target_id)
        be = df[df["fare_brand"] == "basic_economy"]
        assert len(be) == 1
        assert not be["risk_passed"].iloc[0]
        assert "basic_economy" in be["risk_rejections"].iloc[0]
        assert df["risk_passed"].sum() == len(df) - 1


def test_price_series_min_passed_matches_lowest_seen(seeded: Loaded):
    for _, t in _targets(seeded):
        df = data.price_series(seeded, t.target_id)
        assert df.loc[df["risk_passed"], "price_cents"].min() == seeded.db.lowest_seen(t.target_id)


def test_price_series_empty_target_keeps_schema(empty: Loaded):
    df = data.price_series(empty, "alpha-one:SEASJC:2026-11-25:2026-11-29")
    assert df.empty
    assert df["price_cents"].dtype == "int64"
    assert df["risk_passed"].dtype == bool
    assert str(df["observed_at"].dt.tz) == "UTC"


def test_band_series_one_row_per_seeded_day(seeded: Loaded):
    for _, t in _targets(seeded):
        df = data.band_series(seeded, t.target_id)
        assert len(df) == DAYS
        assert df["observed_at"].is_monotonic_increasing
        assert (df["typical_low_cents"] <= df["typical_high_cents"]).all()
        assert_integer(df, "typical_low_cents", "typical_high_cents", "lowest_price_cents")
        assert set(df["price_level"]) <= {"low", "typical", "high", "unknown"}


def test_band_series_empty_keeps_schema(empty: Loaded):
    df = data.band_series(empty, "alpha-one:SEASJC:2026-11-25:2026-11-29")
    assert df.empty
    assert list(df.columns) == [
        "observed_at", "typical_low_cents", "typical_high_cents", "lowest_price_cents",
        "price_level",
    ]  # fmt: skip
    assert_integer(df, "typical_low_cents", "typical_high_cents", "lowest_price_cents")


# --------------------------------------------------------------------------- #
# budget_today / last_run_at
# --------------------------------------------------------------------------- #


def test_budget_today_with_seed_data_is_zero(seeded: Loaded):
    out = data.budget_today(seeded, LIMITS, NOW)
    assert out == {"used": 0, "limit": 4, "calls_per_day": seeded.wf.calls_per_day()}


def _call(at: datetime, cost: int = 1) -> ApiCall:
    return ApiCall(
        run_id="live-1", called_at=at, provider=Provider.SERPAPI, endpoint="search",
        status=ApiCallStatus.OK, cost_units=cost,
    )  # fmt: skip


def test_budget_today_counts_utc_day_of_now_and_restores_clock(empty: Loaded):
    db = empty.db
    big = BudgetLimits(max_per_run=10, max_per_day=10)
    db.start_run(make_run("live-1", trigger="cron"))
    db.reserve_api_call(_call(NOW - timedelta(days=1)), big)  # yesterday: not counted
    db.reserve_api_call(_call(NOW.replace(hour=0, minute=1), cost=2), big)
    db.reserve_api_call(_call(NOW), big)
    before = db.now
    assert data.budget_today(empty, LIMITS, NOW)["used"] == 3
    assert data.budget_today(empty, LIMITS, NOW + timedelta(days=1))["used"] == 0
    assert db.now is before


def test_last_run_at(empty: Loaded, seeded: Loaded):
    assert data.last_run_at(empty) is None
    empty.db.start_run(make_run("a", started_at=NOW - timedelta(hours=5)))
    empty.db.start_run(make_run("b", started_at=NOW - timedelta(hours=1)))
    assert data.last_run_at(empty) == NOW - timedelta(hours=1)
    last = data.last_run_at(seeded)
    assert last is not None and last.tzinfo is not None
    assert last <= NOW


# --------------------------------------------------------------------------- #
# alerts_recent
# --------------------------------------------------------------------------- #


def test_alerts_recent_empty_state(seeded: Loaded):
    df = data.alerts_recent(seeded)
    assert df.empty
    assert "message" in df.columns and "kind" in df.columns
    assert_integer(df, "price_cents", "reference_cents")


def test_alerts_recent_newest_first_and_limited(empty: Loaded):
    db = empty.db
    for i in range(5):
        db.insert_alert(make_alert(f"k{i}", NOW - timedelta(hours=10 - i), price_cents=20000 + i))
    db.insert_alert(
        make_alert("kd", NOW, kind=AlertKind.FENCE_WARNING, regime=Regime.FENCE, price_cents=None)
    )
    db.mark_delivered(db.con.execute("SELECT MAX(id) FROM alerts").fetchone()[0], "ntfy")
    df = data.alerts_recent(empty, limit=3)
    assert len(df) == 3
    assert df["created_at"].is_monotonic_decreasing
    assert list(df["kind"])[0] == "fence_warning"
    assert list(df["delivered"]) == [True, False, False]
    assert df["price_cents"].isna().iloc[0]
    assert_integer(df, "price_cents", "reference_cents")
    assert len(data.alerts_recent(empty)) == 6
