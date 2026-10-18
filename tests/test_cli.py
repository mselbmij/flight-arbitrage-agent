"""Phase 2: src/runner/cli.py (ARCHITECTURE.md section 7: seed, targets, budget, key)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from freezegun import freeze_time
from src.db import Database
from src.runner.cli import app
from typer.testing import CliRunner

from src.models import WatchlistFile
from tests.conftest import REPO

runner = CliRunner()
WIDE = {"COLUMNS": "220"}

BUDGET_YAML = """
version: 1
watchlists:
  - id: grid-flex
    origin: SEA
    destination: [SJC, SFO, OAK]
    depart: 2030-06-06
    return: 2030-06-10
    ceiling_cents: 30000
    poll: {times_per_day: 2, providers: [serpapi], targets_per_run: 3}
"""  # 3 targets_per_run x 1 provider x 2 per day = 6 calls/day

SEED_YAML = """
version: 1
watchlists:
  - id: alpha-one
    origin: SEA
    destination: SJC
    depart: 2026-11-25
    return: 2026-11-29
    ceiling_cents: 32000
  - id: beta-two
    origin: SEA
    destination: [SJC, SFO]
    depart: 2026-12-10
    return: 2026-12-14
    ceiling_cents: 30000
  - id: gamma-paused
    origin: SEA
    destination: ORD
    depart: 2026-12-10
    return: 2026-12-14
    ceiling_cents: 30000
    status: paused
"""
ALPHA = "alpha-one:SEASJC:2026-11-25:2026-11-29"
BETA = ["beta-two:SEASJC:2026-12-10:2026-12-14", "beta-two:SEASFO:2026-12-10:2026-12-14"]
NOW = "2026-10-17 12:00:00"


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "watchlists.yaml"
    p.write_text(text)
    return p


def seed(tmp_path: Path, days: int = 10):
    cfg, dbp = write(tmp_path, SEED_YAML), tmp_path / "fares.db"
    res = runner.invoke(app, ["seed", "--db", str(dbp), "--config", str(cfg), "--days", str(days)])
    return res, cfg, dbp


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


def test_budget_config_fixture_is_six_calls_per_day():
    wf = WatchlistFile.model_validate(yaml.safe_load(BUDGET_YAML))
    assert wf.calls_per_day() == 6


def test_budget_exits_1_when_over_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_DAILY_API_CALLS", "5")
    res = runner.invoke(app, ["budget", "--config", str(write(tmp_path, BUDGET_YAML))])
    assert res.exit_code == 1
    assert "calls/day: 6" in res.output and "limit: 5" in res.output
    assert "OVER" in res.output


def test_budget_exits_0_when_exactly_at_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_DAILY_API_CALLS", "6")
    res = runner.invoke(app, ["budget", "--config", str(write(tmp_path, BUDGET_YAML))])
    assert res.exit_code == 0
    assert "calls/day: 6" in res.output and "limit: 6" in res.output
    assert "OVER" not in res.output and "ok" in res.output


def test_budget_exits_0_when_well_under_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_DAILY_API_CALLS", "50")
    res = runner.invoke(app, ["budget", "--config", str(write(tmp_path, BUDGET_YAML))])
    assert res.exit_code == 0 and "OVER" not in res.output


def test_budget_on_shipped_example_config_reports_its_calls(monkeypatch: pytest.MonkeyPatch):
    cfg = REPO / "config" / "watchlists.yaml"
    wf = WatchlistFile.model_validate(yaml.safe_load(cfg.read_text()))
    monkeypatch.setenv("MAX_DAILY_API_CALLS", "4")
    res = runner.invoke(app, ["budget", "--config", str(cfg)])
    assert f"calls/day: {wf.calls_per_day()}" in res.output
    assert res.exit_code == (1 if wf.calls_per_day() > 4 else 0)


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #


def test_migrate_creates_db_and_reports_versions_then_nothing(tmp_path: Path):
    dbp = tmp_path / "fares.db"
    first = runner.invoke(app, ["migrate", "--db", str(dbp)])
    second = runner.invoke(app, ["migrate", "--db", str(dbp)])
    assert first.exit_code == 0 and dbp.exists()
    assert "001" in first.output
    assert second.exit_code == 0 and "001" not in second.output


# --------------------------------------------------------------------------- #
# seed
# --------------------------------------------------------------------------- #


@freeze_time(NOW)
def test_seed_populates_every_pollable_target_of_every_watchlist(tmp_path: Path):
    res, _, dbp = seed(tmp_path)
    assert res.exit_code == 0, res.output
    with Database(dbp) as d:
        counts = d.counts()
    # alpha (1 target) + beta (2 targets); the paused watchlist is skipped
    assert counts["runs"] == 30
    assert counts["api_calls"] == 30
    assert counts["benchmark_bands"] == 30
    assert 60 <= counts["observations"] <= 90 + 3


@freeze_time(NOW)
def test_seed_implies_migrate_on_a_missing_database(tmp_path: Path):
    res, _, dbp = seed(tmp_path, days=3)
    assert res.exit_code == 0 and dbp.exists()


@freeze_time(NOW)
def test_seed_prints_counts(tmp_path: Path):
    res, *_ = seed(tmp_path, days=3)
    assert "runs" in res.output and "9" in res.output


@freeze_time(NOW)
def test_seed_skips_paused_watchlists(tmp_path: Path):
    _, _, dbp = seed(tmp_path, days=4)
    with Database(dbp) as d:
        assert d.last_polled(["gamma-paused:SEAORD:2026-12-10:2026-12-14"]) == {
            "gamma-paused:SEAORD:2026-12-10:2026-12-14": None
        }


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #


@freeze_time(NOW)
def test_targets_shows_dash_before_seed_and_timestamp_after(tmp_path: Path):
    cfg, dbp = write(tmp_path, SEED_YAML), tmp_path / "fares.db"
    args = ["targets", "--config", str(cfg), "--db", str(dbp)]
    before = runner.invoke(app, args, env=WIDE)
    assert before.exit_code == 0, before.output
    line = next(ln for ln in before.output.splitlines() if ALPHA in ln)
    assert "2026-10-17" not in line

    seeded, *_ = seed(tmp_path)
    assert seeded.exit_code == 0
    after = runner.invoke(app, args, env=WIDE)
    line = next(ln for ln in after.output.splitlines() if ALPHA in ln)
    assert "2026-10-17" in line


@freeze_time(NOW)
def test_targets_lists_expanded_grid_with_regime_and_dtd(tmp_path: Path):
    cfg, dbp = write(tmp_path, SEED_YAML), tmp_path / "fares.db"
    res = runner.invoke(app, ["targets", "--config", str(cfg), "--db", str(dbp)], env=WIDE)
    for tid in [ALPHA, *BETA]:
        assert tid in res.output
    line = next(ln for ln in res.output.splitlines() if ALPHA in ln)
    assert "prime" in line.lower() and "39" in line


@freeze_time(NOW)
def test_targets_filters_to_one_watchlist(tmp_path: Path):
    cfg, dbp = write(tmp_path, SEED_YAML), tmp_path / "fares.db"
    args = ["targets", "beta-two", "--config", str(cfg), "--db", str(dbp)]
    res = runner.invoke(app, args, env=WIDE)
    assert res.exit_code == 0
    assert all(t in res.output for t in BETA)
    assert ALPHA not in res.output


# --------------------------------------------------------------------------- #
# key
# --------------------------------------------------------------------------- #


@freeze_time(NOW)
def test_key_prints_itinerary_key_and_target_id_of_a_seeded_observation(tmp_path: Path):
    _, _, dbp = seed(tmp_path, days=3)
    with Database(dbp) as d:
        obs = d.observations_for(ALPHA)[0]
    res = runner.invoke(app, ["key", str(obs.id), "--db", str(dbp)], env=WIDE)
    assert res.exit_code == 0, res.output
    assert obs.itinerary_key in res.output
    assert ALPHA in res.output
