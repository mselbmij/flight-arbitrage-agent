"""Phase 2: src/runner/cli.py (ARCHITECTURE.md section 7: seed, targets, budget, key)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from freezegun import freeze_time
from typer.testing import CliRunner

from src.db import Database
from src.models import WatchlistFile
from src.runner.cli import app
from tests.conftest import REPO, make_alert, make_obs, make_run

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
def test_seed_refuses_to_run_twice_without_replace(tmp_path: Path):
    res1, cfg, dbp = seed(tmp_path, days=3)
    assert res1.exit_code == 0, res1.output
    res2 = runner.invoke(app, ["seed", "--db", str(dbp), "--config", str(cfg), "--days", "3"])
    assert res2.exit_code == 1
    assert "--replace" in res2.output
    with Database(dbp) as d:
        assert d.counts()["runs"] == 9  # untouched


@freeze_time(NOW)
def test_seed_replace_is_idempotent(tmp_path: Path):
    _, cfg, dbp = seed(tmp_path, days=3)
    seed_counts: dict[str, int]
    with Database(dbp) as d:
        seed_counts = d.counts()
        # non-seed data that --replace must never touch
        d.start_run(make_run("cron-1", trigger="cron"))
        [cron_obs] = d.insert_observations(
            [make_obs("cron-1", price=21000, watchlist_id="alpha-one", target_id=ALPHA)]
        )
        d.insert_alert(
            make_alert("keep", watchlist_id="alpha-one", target_id=ALPHA, observation_id=cron_obs)
        )
        # an alert pointing at a seed observation must go with it
        seed_obs = d.observations_for(ALPHA)[0]
        assert seed_obs.run_id.startswith("seed-")
        d.insert_alert(
            make_alert(
                "drop", watchlist_id="alpha-one", target_id=ALPHA, observation_id=seed_obs.id
            )
        )
        before = d.counts()
    assert before["alerts"] == 2 and before["runs"] == seed_counts["runs"] + 1

    res = runner.invoke(
        app, ["seed", "--db", str(dbp), "--config", str(cfg), "--days", "3", "--replace"]
    )
    assert res.exit_code == 0, res.output
    with Database(dbp) as d:
        after = d.counts()
        assert after == {**before, "alerts": 1}  # seed rows regenerated 1:1, one alert removed
        assert d.calls_today() == 0  # seed rows never consume the real budget
        assert d.get_run("cron-1") is not None
        assert d.get_run("cron-1").trigger == "cron"
        kept = d.observations_for(ALPHA)
        assert any(o.run_id == "cron-1" and o.price_cents == 21000 for o in kept)
        [alert] = d.undelivered_alerts(max_age_hours=24 * 365)
        assert alert.alert_key == "keep" and alert.observation_id == cron_obs
        assert (
            d.con.execute("SELECT COUNT(*) FROM runs WHERE \"trigger\"='seed'").fetchone()[0]
            == seed_counts["runs"]
        )


@freeze_time(NOW)
def test_seed_implies_migrate_on_a_missing_database(tmp_path: Path):
    res, _, dbp = seed(tmp_path, days=3)
    assert res.exit_code == 0 and dbp.exists()


@freeze_time(NOW)
def test_seed_prints_counts(tmp_path: Path):
    res, *_ = seed(tmp_path, days=3)
    assert "runs=9" in res.output.split()


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


# --------------------------------------------------------------------------- #
# seed: isolation from the live database, and the demo watchlists
# --------------------------------------------------------------------------- #

END_ISO = "2026-10-17T12:00:00+00:00"
# (dip DTD, trap DTD) per demo target at --days 45 ending END_ISO. demo-monitor is 75 DTD out at
# the end, so its dip and trap are clamped to dtd_at_end + 4 and + 8.
DEMO_EXPECTED = {
    "demo-late:SEASJC:2026-10-27:ow": (34, 30),
    "demo-prime:SEASFO:2026-11-16:2026-11-19": (34, 30),
    "demo-prime:SEAOAK:2026-11-16:2026-11-19": (34, 30),
    "demo-monitor:SEAORD:2026-12-31:2027-01-04": (79, 83),
}


def seed_demo(tmp_path: Path, *extra: str):
    dbp, demo = tmp_path / "seed.db", tmp_path / "demo.yaml"
    args = ["seed", "--db", str(dbp), "--write-config", str(demo), "--end", END_ISO, *extra]
    return runner.invoke(app, args), dbp, demo


def test_seed_without_config_uses_demo_watchlists_and_writes_the_yaml(tmp_path: Path):
    res, dbp, demo = seed_demo(tmp_path)
    assert res.exit_code == 0, res.output
    assert demo.exists()
    wf = WatchlistFile.model_validate(yaml.safe_load(demo.read_text()))
    assert [w.id for w in wf.watchlists] == ["demo-late", "demo-prime", "demo-monitor"]
    with Database(dbp) as d:
        assert d.counts()["runs"] == 4 * 45  # 4 targets x 45 days


def test_seed_demo_every_target_has_exactly_one_rejected_trap_on_its_dip_day(tmp_path: Path):
    res, dbp, _ = seed_demo(tmp_path)
    assert res.exit_code == 0, res.output
    with Database(dbp) as d:
        for tid, (dip_dtd, trap_dtd) in DEMO_EXPECTED.items():
            obs = d.observations_for(tid)
            traps = [
                o
                for o in obs
                if o.fare_brand.value == "basic_economy" and o.risk and o.risk.passed is False
            ]
            assert len(traps) == 1, tid
            assert [o for o in obs if o.fare_brand.value == "basic_economy"] == traps, tid
            assert traps[0].days_to_departure == trap_dtd, tid
            rows = d.con.execute(
                "SELECT COUNT(*) FROM observations WHERE target_id=? AND risk_passed=0 "
                "AND fare_brand='basic_economy'",
                (tid,),
            ).fetchone()[0]
            assert rows == 1, tid

            passed = d.observations_for(tid, passed_only=True)
            cheapest = min(passed, key=lambda o: o.price_cents)
            assert cheapest.days_to_departure == dip_dtd, tid
            assert traps[0].price_cents < cheapest.price_cents, tid  # the trap is the lure


def test_seed_demo_late_history_covers_dip_trap_and_both_fence_steps(tmp_path: Path):
    res, dbp, _ = seed_demo(tmp_path)
    assert res.exit_code == 0, res.output
    with Database(dbp) as d:
        obs = d.observations_for("demo-late:SEASJC:2026-10-27:ow")
    assert {o.days_to_departure for o in obs} == set(range(10, 55))
    ns = {
        o.days_to_departure: o.price_cents
        for o in obs
        if o.fare_brand.value == "main_cabin" and o.itinerary.outbound.stops == 0
    }
    assert ns[20] > ns[22] * 1.1 and ns[13] > ns[15] * 1.04  # 21 and 14 day fences


def test_seed_demo_default_paths_are_seed_db_and_demo_yaml_under_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "live.db"
    monkeypatch.setenv("FARES_DB_PATH", str(live))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["seed", "--end", END_ISO])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "data" / "seed.db").exists()
    assert (tmp_path / "config" / "watchlists.demo.yaml").exists()
    assert not live.exists()  # the default never touches FARES_DB_PATH


def test_seed_demo_yaml_not_written_when_config_is_given(tmp_path: Path):
    cfg, dbp, demo = write(tmp_path, SEED_YAML), tmp_path / "seed.db", tmp_path / "demo.yaml"
    res = runner.invoke(
        app, ["seed", "--db", str(dbp), "--config", str(cfg), "--write-config", str(demo)]
    )
    assert res.exit_code == 0, res.output
    assert not demo.exists()


def test_seed_demo_yaml_not_written_when_seed_refused(tmp_path: Path):
    res, dbp, demo = seed_demo(tmp_path)
    assert res.exit_code == 0
    demo.write_text("sentinel")
    again = runner.invoke(
        app, ["seed", "--db", str(dbp), "--write-config", str(demo), "--end", END_ISO]
    )
    assert again.exit_code == 1
    assert demo.read_text() == "sentinel"


def test_seed_refuses_the_live_db_without_allow_live_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "live.db"
    monkeypatch.setenv("FARES_DB_PATH", str(live))
    cfg = write(tmp_path, SEED_YAML)
    args = ["seed", "--db", str(live), "--config", str(cfg), "--days", "3"]
    refused = runner.invoke(app, args)
    assert refused.exit_code == 1
    assert "live" in refused.output and "--allow-live-db" in refused.output
    assert not live.exists()  # nothing was created, let alone written

    allowed = runner.invoke(app, [*args, "--allow-live-db"])
    assert allowed.exit_code == 0, allowed.output
    with Database(live) as d:
        assert d.counts()["runs"] == 9


def test_seed_live_db_check_compares_resolved_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "sub").mkdir()
    monkeypatch.setenv("FARES_DB_PATH", str(tmp_path / "live.db"))
    sneaky = tmp_path / "sub" / ".." / "live.db"
    res = runner.invoke(
        app, ["seed", "--db", str(sneaky), "--config", str(write(tmp_path, SEED_YAML))]
    )
    assert res.exit_code == 1
    assert not (tmp_path / "live.db").exists()


def test_seed_live_db_check_applies_to_the_demo_config_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "live.db"
    monkeypatch.setenv("FARES_DB_PATH", str(live))
    res = runner.invoke(
        app, ["seed", "--db", str(live), "--write-config", str(tmp_path / "d.yaml")]
    )
    assert res.exit_code == 1
    assert not live.exists() and not (tmp_path / "d.yaml").exists()


def test_seed_on_the_shipped_config_gives_every_target_a_trap_and_a_dip(tmp_path: Path):
    """The shipped config, at a real 'now' end: the lesson from the first seeder review."""
    dbp = tmp_path / "seed.db"
    cfg = REPO / "config" / "watchlists.yaml"
    res = runner.invoke(
        app, ["seed", "--db", str(dbp), "--config", str(cfg), "--end", "2026-10-17T12:00:00Z"]
    )
    assert res.exit_code == 0, res.output
    wf = WatchlistFile.model_validate(yaml.safe_load(cfg.read_text()))
    with Database(dbp) as d:
        for wl in wf.resolved():
            for t in wl.pollable_targets:
                obs = d.observations_for(t.target_id)
                be = [o for o in obs if o.fare_brand.value == "basic_economy"]
                assert len(be) == 1, t.target_id
                assert be[0].risk is not None and be[0].risk.passed is False
