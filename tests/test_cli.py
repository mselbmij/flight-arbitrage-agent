"""Phase 2: src/runner/cli.py (ARCHITECTURE.md section 7: seed, targets, budget, key)."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from freezegun import freeze_time
from typer.testing import CliRunner

from src.db import Database
from src.ingestion.mock_client import MockProvider
from src.models import Alert, AlertKind, Provider, Regime, Run, WatchlistFile
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


def test_budget_default_daily_limit_is_three_like_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MAX_DAILY_API_CALLS", raising=False)
    res = runner.invoke(app, ["budget", "--config", str(write(tmp_path, BUDGET_YAML))])
    assert res.exit_code == 1  # 6 calls/day against the default
    assert "limit: 3" in res.output and "OVER" in res.output


def test_budget_default_limit_comes_from_the_same_constant_as_poll():
    from src.runner import cli

    assert (cli.DEFAULT_MAX_CALLS_PER_RUN, cli.DEFAULT_MAX_DAILY_API_CALLS) == (2, 3)


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


# --------------------------------------------------------------------------- #
# poll (phase 5): `fares poll` end to end on the mock provider
# --------------------------------------------------------------------------- #

FX = REPO / "tests" / "fixtures" / "mock"
THANKS = "sea-sjc-thanksgiving:SEASJC:2026-11-25:2026-11-29"
POLL_YAML = """
version: 1
watchlists:
  - id: sea-sjc-thanksgiving
    origin: SEA
    destination: SJC
    depart: 2026-11-25
    return: 2026-11-29
    ceiling_cents: 32000
    poll: {providers: [serpapi], targets_per_run: 1}
"""
TWO_TARGET_YAML = """
version: 1
watchlists:
  - id: sea-sjc-thanksgiving
    origin: SEA
    destination: [SJC, SFO]
    depart: 2026-11-25
    return: 2026-11-29
    ceiling_cents: 32000
    poll: {providers: [serpapi], targets_per_run: 2}
"""


@pytest.fixture
def poll_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No provider credentials, roomy limits, and a FARES_DB_PATH that is a tmp 'live' file."""
    live = tmp_path / "live.db"
    monkeypatch.setenv("FARES_DB_PATH", str(live))
    monkeypatch.setenv("MAX_CALLS_PER_RUN", "10")
    monkeypatch.setenv("MAX_DAILY_API_CALLS", "100")
    for var in (
        "SERPAPI_KEY",
        "AMADEUS_CLIENT_ID",
        "AMADEUS_CLIENT_SECRET",
        "AMADEUS_HOST",
        "NTFY_SERVER",
        "NTFY_TOPIC",
        "NTFY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    return live


def poll_args(tmp_path: Path, cfg_text: str = POLL_YAML) -> tuple[list[str], Path]:
    dbp = tmp_path / "scratch.db"
    args = [
        "poll",
        "--db", str(dbp),
        "--config", str(write(tmp_path, cfg_text)),
        "--raw-dir", str(tmp_path / "raw"),
        "--provider-override", "mock",
        "--fixtures", str(FX),
    ]  # fmt: skip
    return args, dbp


def rows(dbp: Path, sql: str) -> list[dict]:
    with Database(dbp) as d:
        return [dict(r) for r in d.con.execute(sql)]


@freeze_time(NOW)
def test_poll_override_mock_runs_end_to_end_and_exits_0(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    [run] = rows(dbp, "SELECT * FROM runs")
    assert run["status"] == "ok" and run["calls_made"] == 1 and run["finished_at"] is not None
    [call] = rows(dbp, "SELECT * FROM api_calls")
    assert (call["provider"], call["status"], call["target_id"]) == ("mock", "ok", THANKS)
    assert len(rows(dbp, "SELECT * FROM observations")) == 3
    assert len(rows(dbp, "SELECT * FROM benchmark_bands")) == 1
    [alert] = rows(dbp, "SELECT * FROM alerts")
    # AS 327 31800 and DL via SLC 26900 are clean; the 22900 Basic Economy trap is rejected
    assert (alert["kind"], alert["price_cents"]) == ("buy", 26900)
    assert alert["delivered_at"] is None  # phase 5 ships the NullNotifier: undelivered, not lost


@freeze_time(NOW)
def test_poll_prints_run_id_status_counts_and_a_line_per_target(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, args, env=WIDE)
    assert res.exit_code == 0, res.output
    [run] = rows(dbp, "SELECT run_id FROM runs")
    out = res.output
    assert run["run_id"] in out
    assert re.search(r"\bok\b", out)
    assert re.search(r"calls\D{0,12}1\b", out), out
    assert re.search(r"observations\D{0,12}3\b", out), out
    assert re.search(r"alerts\D{0,12}1\b", out), out
    assert THANKS in out


@freeze_time(NOW)
def test_poll_trigger_defaults_to_manual_and_cron_is_recorded(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    assert runner.invoke(app, args).exit_code == 0
    assert rows(dbp, 'SELECT "trigger" AS t FROM runs') == [{"t": "manual"}]

    (tmp_path / "second").mkdir()
    args2, dbp2 = poll_args(tmp_path / "second")
    res = runner.invoke(app, [*args2, "--trigger", "cron"])
    assert res.exit_code == 0, res.output
    assert rows(dbp2, 'SELECT "trigger" AS t FROM runs') == [{"t": "cron"}]


def test_poll_rejects_an_unknown_trigger_and_writes_nothing(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, [*args, "--trigger", "weekly"])
    assert res.exit_code != 0
    assert not dbp.exists() or rows(dbp, "SELECT * FROM runs") == []


@freeze_time(NOW)
def test_poll_writes_raw_payloads_under_raw_dir_run_id_provider_target(
    tmp_path: Path, poll_env: Path
):
    args, dbp = poll_args(tmp_path)
    assert runner.invoke(app, args).exit_code == 0
    [run] = rows(dbp, "SELECT run_id FROM runs")
    raw = tmp_path / "raw" / run["run_id"] / f"mock-{THANKS.replace(':', '_')}.json"
    assert raw.is_file()
    refs = rows(dbp, "SELECT DISTINCT raw_payload_ref AS r FROM observations")
    assert refs == [{"r": str(Path(run["run_id"]) / raw.name)}]


@freeze_time(NOW)
def test_poll_raw_dir_defaults_to_data_raw_under_the_working_directory(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    args, dbp = poll_args(tmp_path)
    i = args.index("--raw-dir")
    del args[i : i + 2]
    assert runner.invoke(app, args).exit_code == 0
    [run] = rows(dbp, "SELECT run_id FROM runs")
    assert (tmp_path / "data" / "raw" / run["run_id"]).is_dir()


@freeze_time(NOW)
def test_poll_works_on_the_shipped_config_and_the_shipped_mock_fixture(
    tmp_path: Path, poll_env: Path
):
    """The documented invocation. Watchlists without a fixture are PROVIDER_ERRORs, not a crash."""
    dbp = tmp_path / "scratch.db"
    res = runner.invoke(
        app,
        [
            "poll", "--db", str(dbp), "--config", str(REPO / "config" / "watchlists.yaml"),
            "--provider-override", "mock", "--fixtures", str(FX),
            "--raw-dir", str(tmp_path / "raw"),
        ],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    [run] = rows(dbp, "SELECT * FROM runs")
    assert run["status"] == "ok"
    assert len(rows(dbp, f"SELECT * FROM observations WHERE target_id='{THANKS}'")) == 3
    errors = rows(dbp, "SELECT * FROM alerts WHERE kind='provider_error'")
    assert {e["watchlist_id"] for e in errors} <= {"sea-bayarea-jan-flex"}


# -- the live database is off limits under --provider-override -------------------------------


def test_poll_override_refuses_an_explicit_live_db_without_allow_live_db(
    tmp_path: Path, poll_env: Path
):
    args, _ = poll_args(tmp_path)
    args[args.index("--db") + 1] = str(poll_env)
    res = runner.invoke(app, args)
    assert res.exit_code == 1
    assert "--allow-live-db" in res.output
    assert not poll_env.exists()  # nothing created, let alone written


def test_poll_override_refuses_the_default_db_when_it_is_the_live_one(
    tmp_path: Path, poll_env: Path
):
    args, _ = poll_args(tmp_path)
    i = args.index("--db")
    del args[i : i + 2]  # fall back to $FARES_DB_PATH
    res = runner.invoke(app, args)
    assert res.exit_code == 1
    assert "--allow-live-db" in res.output
    assert not poll_env.exists()


def test_poll_override_live_db_check_compares_resolved_paths(tmp_path: Path, poll_env: Path):
    (tmp_path / "sub").mkdir()
    args, _ = poll_args(tmp_path)
    args[args.index("--db") + 1] = str(tmp_path / "sub" / ".." / "live.db")
    assert runner.invoke(app, args).exit_code == 1
    assert not poll_env.exists()


@freeze_time(NOW)
def test_poll_override_on_the_live_db_proceeds_with_allow_live_db(tmp_path: Path, poll_env: Path):
    args, _ = poll_args(tmp_path)
    args[args.index("--db") + 1] = str(poll_env)
    res = runner.invoke(app, [*args, "--allow-live-db"])
    assert res.exit_code == 0, res.output
    assert len(rows(poll_env, "SELECT * FROM runs")) == 1


def test_poll_override_without_fixtures_is_a_usage_error(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    i = args.index("--fixtures")
    del args[i : i + 2]
    res = runner.invoke(app, args)
    assert res.exit_code != 0
    assert not dbp.exists() or rows(dbp, "SELECT * FROM runs") == []


# -- exit codes ----------------------------------------------------------------------------------


@freeze_time(NOW)
def test_poll_exits_2_when_the_breaker_trips(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MAX_CALLS_PER_RUN", "1")
    args, dbp = poll_args(tmp_path, TWO_TARGET_YAML)
    res = runner.invoke(app, args)
    assert res.exit_code == 2, res.output
    assert "budget_tripped" in res.output
    [run] = rows(dbp, "SELECT * FROM runs")
    assert run["status"] == "budget_tripped" and run["calls_made"] == 1
    assert len(rows(dbp, "SELECT * FROM alerts WHERE kind='budget_tripped'")) == 1
    assert [r["status"] for r in rows(dbp, "SELECT status FROM api_calls ORDER BY id")] == [
        "ok",
        "skipped_breaker",
    ]


@freeze_time(NOW)
def test_poll_daily_limit_comes_from_the_environment(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MAX_DAILY_API_CALLS", "1")
    args, dbp = poll_args(tmp_path, TWO_TARGET_YAML)
    res = runner.invoke(app, args)
    assert res.exit_code == 2, res.output
    [tripped] = rows(dbp, "SELECT message FROM alerts WHERE kind='budget_tripped'")
    assert tripped["message"] == "API budget tripped (1/1 day)"


@freeze_time(NOW)
def test_poll_exits_1_and_marks_the_run_errored_on_a_bug(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(self, target, watchlist, run_id, observed_at):
        raise RuntimeError("boom")

    monkeypatch.setattr(MockProvider, "search", boom)
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, args)
    assert res.exit_code == 1
    [run] = rows(dbp, "SELECT * FROM runs")
    assert run["status"] == "error" and "boom" in run["error"]
    assert rows(dbp, "SELECT * FROM alerts") == []  # a bug is not a PROVIDER_ERROR


# -- defaults, no providers, and the Amadeus test host on the live database --------------------

THREE_TARGET_YAML = """
version: 1
watchlists:
  - id: sea-sjc-thanksgiving
    origin: SEA
    destination: [SJC, SFO, OAK]
    depart: 2026-11-25
    return: 2026-11-29
    ceiling_cents: 32000
    poll: {providers: [serpapi], targets_per_run: 3}
"""
AMADEUS_YAML = POLL_YAML.replace("[serpapi]", "[amadeus]")


@freeze_time(NOW)
def test_poll_default_per_run_limit_is_two(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MAX_CALLS_PER_RUN")
    monkeypatch.delenv("MAX_DAILY_API_CALLS")
    args, dbp = poll_args(tmp_path, THREE_TARGET_YAML)
    res = runner.invoke(app, args)
    assert res.exit_code == 2, res.output
    [run] = rows(dbp, "SELECT * FROM runs")
    assert run["status"] == "budget_tripped" and run["calls_made"] == 2
    [tripped] = rows(dbp, "SELECT message FROM alerts WHERE kind='budget_tripped'")
    assert tripped["message"] == "API budget tripped (2/2 run)"


@freeze_time(NOW)
def test_poll_default_daily_limit_is_three(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MAX_CALLS_PER_RUN")
    monkeypatch.delenv("MAX_DAILY_API_CALLS")
    args, dbp = poll_args(tmp_path, THREE_TARGET_YAML)
    assert runner.invoke(app, args).exit_code == 2  # run 1: two calls, then the run gate
    res = runner.invoke(app, args)  # run 2: its first call is the day's third; the next trips
    assert res.exit_code == 2, res.output
    first, second = rows(dbp, "SELECT calls_made FROM runs ORDER BY started_at, rowid")
    assert (first["calls_made"], second["calls_made"]) == (2, 1)
    statuses = [r["status"] for r in rows(dbp, "SELECT status FROM api_calls ORDER BY id")]
    assert statuses.count("skipped_breaker") == 2 and len(statuses) - 2 == 3


def test_poll_without_any_provider_configured_exits_1_before_touching_the_db(
    tmp_path: Path, poll_env: Path
):
    dbp = tmp_path / "scratch.db"
    cfg = write(tmp_path, POLL_YAML)
    res = runner.invoke(app, ["poll", "--db", str(dbp), "--config", str(cfg)])
    assert res.exit_code == 1
    assert "no providers configured" in res.output and "SERPAPI_KEY" in res.output
    assert not dbp.exists()


class _Spy:
    """Records provider calls; every call fails like an outage so a run still finishes."""

    calls = 0

    @classmethod
    def install(cls, monkeypatch: pytest.MonkeyPatch) -> type[_Spy]:
        from src.ingestion.amadeus_client import AmadeusProvider
        from src.ingestion.base import ProviderError

        cls.calls = 0

        def search(self, target, watchlist, run_id, observed_at):
            cls.calls += 1
            raise ProviderError(Provider.MOCK, "spy: no network")

        monkeypatch.setattr(AmadeusProvider, "search", search)
        return cls


@pytest.fixture
def amadeus_env(poll_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AMADEUS_CLIENT_ID", "id")
    monkeypatch.setenv("AMADEUS_CLIENT_SECRET", "secret")
    return poll_env


def live_poll_args(tmp_path: Path, live: Path, *extra: str) -> list[str]:
    return ["poll", "--db", str(live), "--config", str(write(tmp_path, AMADEUS_YAML)),
            "--raw-dir", str(tmp_path / "raw"), *extra]  # fmt: skip


@pytest.mark.parametrize("host", [None, "test", "sandbox"])
def test_poll_refuses_the_live_db_with_amadeus_credentials_on_a_non_production_host(
    tmp_path: Path, amadeus_env: Path, monkeypatch: pytest.MonkeyPatch, host: str | None
):
    if host is not None:
        monkeypatch.setenv("AMADEUS_HOST", host)
    spy = _Spy.install(monkeypatch)
    res = runner.invoke(app, live_poll_args(tmp_path, amadeus_env))
    assert res.exit_code == 1
    assert "AMADEUS_HOST" in res.output and "--allow-live-db" in res.output
    assert "live" in res.output
    assert spy.calls == 0  # refused before any provider call
    assert not amadeus_env.exists()  # and before anything was written


def test_poll_refuses_the_implicit_live_db_too(
    tmp_path: Path, amadeus_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AMADEUS_HOST", "test")
    spy = _Spy.install(monkeypatch)
    args = live_poll_args(tmp_path, amadeus_env)
    i = args.index("--db")
    del args[i : i + 2]  # falls back to $FARES_DB_PATH
    assert runner.invoke(app, args).exit_code == 1
    assert spy.calls == 0 and not amadeus_env.exists()


@freeze_time(NOW)
def test_poll_amadeus_test_host_on_the_live_db_proceeds_with_allow_live_db(
    tmp_path: Path, amadeus_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AMADEUS_HOST", "test")
    spy = _Spy.install(monkeypatch)
    res = runner.invoke(app, live_poll_args(tmp_path, amadeus_env, "--allow-live-db"))
    assert res.exit_code == 0, res.output
    assert spy.calls == 1
    assert len(rows(amadeus_env, "SELECT * FROM runs")) == 1


@freeze_time(NOW)
def test_poll_amadeus_production_host_on_the_live_db_needs_no_override(
    tmp_path: Path, amadeus_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AMADEUS_HOST", "production")
    spy = _Spy.install(monkeypatch)
    res = runner.invoke(app, live_poll_args(tmp_path, amadeus_env))
    assert res.exit_code == 0, res.output
    assert spy.calls == 1


@freeze_time(NOW)
def test_poll_amadeus_test_host_on_a_scratch_db_needs_no_override(
    tmp_path: Path, amadeus_env: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AMADEUS_HOST", "test")
    spy = _Spy.install(monkeypatch)
    scratch = tmp_path / "scratch.db"
    res = runner.invoke(app, live_poll_args(tmp_path, scratch))
    assert res.exit_code == 0, res.output
    assert spy.calls == 1 and not amadeus_env.exists()


@freeze_time(NOW)
def test_poll_serpapi_only_on_the_live_db_is_not_blocked_by_the_amadeus_host_rule(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    """No Amadeus credentials: AMADEUS_HOST is irrelevant."""
    from src.ingestion.base import ProviderError
    from src.ingestion.serpapi_client import SerpApiProvider

    monkeypatch.setenv("SERPAPI_KEY", "k")
    monkeypatch.setenv("AMADEUS_HOST", "test")
    seen = []

    def search(self, target, watchlist, run_id, observed_at):
        seen.append(target.target_id)
        raise ProviderError(Provider.MOCK, "spy: no network")

    monkeypatch.setattr(SerpApiProvider, "search", search)
    res = runner.invoke(
        app,
        ["poll", "--db", str(poll_env), "--config", str(write(tmp_path, POLL_YAML)),
         "--raw-dir", str(tmp_path / "raw")],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert seen == [THANKS]


# --------------------------------------------------------------------------- #
# Phase 6: --notifier, alert-test, health (ARCHITECTURE.md sections 7, 7a)
# --------------------------------------------------------------------------- #

NTFY = "https://ntfy.example.test"
TOPIC_URL = f"{NTFY}/fares-test"


@pytest.fixture
def ntfy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_SERVER", NTFY)
    monkeypatch.setenv("NTFY_TOPIC", "fares-test")
    monkeypatch.delenv("NTFY_TOKEN", raising=False)


@pytest.fixture
def no_ntfy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NTFY_SERVER", "NTFY_TOPIC", "NTFY_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@freeze_time(NOW)
def test_poll_notifier_log_prints_the_buy_title_and_marks_the_alert_delivered(
    tmp_path: Path, poll_env: Path
):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, [*args, "--notifier", "log"])
    assert res.exit_code == 0, res.output
    assert re.search(r"^BUY SEA→SJC \$269 ", res.output, re.M), res.output
    [alert] = rows(dbp, "SELECT * FROM alerts")
    assert alert["channel"] == "log" and alert["delivered_at"] is not None


@freeze_time(NOW)
def test_poll_notifier_null_leaves_the_alert_undelivered_without_a_warning(
    tmp_path: Path, poll_env: Path
):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, [*args, "--notifier", "null"])
    assert res.exit_code == 0, res.output
    assert "NTFY_TOPIC" not in res.output
    [alert] = rows(dbp, "SELECT * FROM alerts")
    assert alert["delivered_at"] is None


@freeze_time(NOW)
def test_poll_without_a_topic_defaults_to_null_and_warns(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert "NTFY_TOPIC is not set" in res.output
    [alert] = rows(dbp, "SELECT * FROM alerts")
    assert alert["delivered_at"] is None


@freeze_time(NOW)
def test_poll_with_a_topic_defaults_to_ntfy_and_marks_delivered(
    tmp_path: Path, poll_env: Path, ntfy_env: None
):
    args, dbp = poll_args(tmp_path)
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert route.call_count == 1
    req = route.calls.last.request
    assert req.headers["Priority"] == "5" and req.headers["Title"].startswith("BUY SEA->SJC")
    assert "Authorization" not in req.headers
    [alert] = rows(dbp, "SELECT * FROM alerts")
    assert alert["channel"] == "ntfy" and alert["delivered_at"] is not None


@freeze_time(NOW)
def test_poll_ntfy_failure_leaves_the_alert_undelivered_and_the_run_ok(
    tmp_path: Path, poll_env: Path, ntfy_env: None, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("NTFY_TOKEN", "tk_secret")
    args, dbp = poll_args(tmp_path)
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(503)
        res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert route.calls.last.request.headers["Authorization"] == "Bearer tk_secret"
    assert "tk_secret" not in res.output
    [alert] = rows(dbp, "SELECT * FROM alerts")
    assert alert["delivered_at"] is None


@freeze_time(NOW)
def test_poll_explicit_ntfy_without_a_topic_is_an_error_before_the_db_is_touched(
    tmp_path: Path, poll_env: Path
):
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, [*args, "--notifier", "ntfy"])
    assert res.exit_code == 1 and "NTFY_TOPIC" in res.output
    assert not dbp.exists()


def test_poll_rejects_an_unknown_notifier(tmp_path: Path, poll_env: Path):
    args, dbp = poll_args(tmp_path)
    assert runner.invoke(app, [*args, "--notifier", "carrier-pigeon"]).exit_code == 2
    assert not dbp.exists()


# -- alert-test -------------------------------------------------------------------------------- #


def test_alert_test_with_the_log_notifier_exits_0_prints_and_creates_no_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_ntfy_env: None
):
    live = tmp_path / "data" / "live.db"
    monkeypatch.setenv("FARES_DB_PATH", str(live))
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["alert-test", "--notifier", "log"])
    assert res.exit_code == 0, res.output
    assert "fare tracker: test ping" in res.output and res.output.strip().endswith("log")
    assert not live.exists() and not (tmp_path / "data").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_alert_test_over_ntfy_sends_a_provider_error_priority_ping(tmp_path: Path, ntfy_env: None):
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, ["alert-test"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == "ntfy"
    req = route.calls.last.request
    assert req.headers["Priority"] == "2" and req.headers["Tags"] == "x"
    assert req.headers["Title"] == "fare tracker: test ping"
    assert req.content == b"If you can read this, the topic works."


def test_alert_test_exits_1_and_says_undelivered_when_ntfy_rejects_it(ntfy_env: None):
    with respx.mock(assert_all_called=False) as m:
        m.post(TOPIC_URL).respond(403)
        res = runner.invoke(app, ["alert-test"])
    assert res.exit_code == 1 and res.output.strip() == "undelivered"


def test_alert_test_exits_1_on_a_transport_error(ntfy_env: None):
    with respx.mock(assert_all_called=False) as m:
        m.post(TOPIC_URL).mock(side_effect=httpx.ConnectError("down"))
        res = runner.invoke(app, ["alert-test"])
    assert res.exit_code == 1 and "undelivered" in res.output


def test_alert_test_without_a_topic_exits_1(no_ntfy_env: None):
    res = runner.invoke(app, ["alert-test"])
    assert res.exit_code == 1 and "NTFY_TOPIC" in res.output


# -- health ------------------------------------------------------------------------------------ #


def run_at(
    dbp: Path,
    hours_ago: float,
    run_id: str = "r1",
    trigger: str = "cron",
    status: str | None = "ok",
    error: str | None = None,
) -> None:
    """A run started `hours_ago`; finished with `status` unless it is None (still running)."""
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    with Database(dbp) as d:
        d.migrate()
        d.start_run(Run(run_id=run_id, started_at=when, trigger=trigger))  # type: ignore[arg-type]
        if status is not None:
            d.finish_run(run_id, status, error)


def test_health_fresh_run_exits_0_prints_the_age_and_sends_nothing(tmp_path: Path, ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 2)
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, ["health", "--db", str(dbp)])
    assert res.exit_code == 0, res.output
    assert re.search(r"last run 2\.0h ago", res.output)
    assert not route.called


def test_health_stale_run_alerts_and_exits_1_without_storing_an_alert(
    tmp_path: Path, ntfy_env: None
):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 40)
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, ["health", "--db", str(dbp)])
    assert res.exit_code == 1, res.output
    req = route.calls.last.request
    assert req.headers["Title"] == "fare tracker: no run in 36h"
    assert req.headers["Priority"] == "4"
    assert rows(dbp, "SELECT * FROM alerts") == []


def test_health_max_age_hours_is_respected_and_named_in_the_title(
    tmp_path: Path, no_ntfy_env: None
):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 5)
    assert runner.invoke(app, ["health", "--db", str(dbp), "--max-age-hours", "6"]).exit_code == 0
    res = runner.invoke(
        app, ["health", "--db", str(dbp), "--max-age-hours", "4", "--notifier", "log"]
    )
    assert res.exit_code == 1
    assert "fare tracker: no run in 4h" in res.output


def fresh_backup(tmp_path: Path) -> Path:
    backups = tmp_path / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "fares-20261018-0300.db").write_bytes(b"x")
    return backups


def test_health_uses_the_newest_run(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 100, "old")
    run_at(dbp, 1, "new")
    res = runner.invoke(
        app, ["health", "--db", str(dbp), "--backup-dir", str(fresh_backup(tmp_path))]
    )
    assert res.exit_code == 0, res.output


def test_health_with_no_runs_alerts_and_exits_1(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    with Database(dbp) as d:
        d.migrate()
    res = runner.invoke(app, ["health", "--db", str(dbp), "--notifier", "log"])
    assert res.exit_code == 1
    assert "fare tracker: no run in 36h" in res.output and "no runs recorded" in res.output


def test_health_with_a_missing_database_alerts_exits_1_and_does_not_create_it(
    tmp_path: Path, no_ntfy_env: None
):
    dbp = tmp_path / "nope.db"
    res = runner.invoke(app, ["health", "--db", str(dbp), "--notifier", "log"])
    assert res.exit_code == 1 and "fare tracker: no run in 36h" in res.output
    assert not dbp.exists()


def test_health_ignores_seed_runs(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 1, "seed-1", trigger="seed")
    res = runner.invoke(app, ["health", "--db", str(dbp), "--notifier", "log"])
    assert res.exit_code == 1


def test_health_stale_without_a_topic_still_exits_1(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 99)
    res = runner.invoke(app, ["health", "--db", str(dbp)])
    assert res.exit_code == 1 and "NTFY_TOPIC" in res.output


def test_health_still_exits_1_when_ntfy_is_down(tmp_path: Path, ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 99)
    with respx.mock(assert_all_called=False) as m:
        m.post(TOPIC_URL).respond(500)
        res = runner.invoke(app, ["health", "--db", str(dbp)])
    assert res.exit_code == 1


# -- health: the failure modes beyond "no run" ------------------------------------------------- #


def health_log(dbp: Path, *extra: str):
    return runner.invoke(app, ["health", "--db", str(dbp), "--notifier", "log", *extra])


def api_call_at(dbp: Path, run_id: str, status: str, error: str | None = None) -> None:
    with Database(dbp) as d:
        d.con.execute(
            "INSERT INTO api_calls (run_id, called_at, provider, endpoint, status, error) "
            "VALUES (?, ?, 'serpapi', 'search:SEASJC', ?, ?)",
            (run_id, datetime.now(UTC).isoformat(), status, error),
        )
        d.con.commit()


def test_health_flags_a_failed_last_run_with_its_error(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 1, status="error", error="OperationalError('database is locked')")
    res = health_log(dbp)
    assert res.exit_code == 1
    assert "fare tracker: last poll failed" in res.output
    assert "database is locked" in res.output


def test_health_flags_a_run_killed_mid_poll(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 3, status=None)
    res = health_log(dbp)
    assert res.exit_code == 1 and "poll killed mid-run" in res.output


def test_health_does_not_flag_a_run_still_in_progress(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 0.1, status=None)
    assert health_log(dbp).exit_code == 0


def test_health_flags_every_api_call_failing_with_the_latest_error(
    tmp_path: Path, no_ntfy_env: None
):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 1)
    api_call_at(dbp, "r1", "error", "SerpAPI HTTP 401: Invalid API key")
    res = health_log(dbp)
    assert res.exit_code == 1
    assert "every API call failed in 36h" in res.output and "Invalid API key" in res.output
    assert "fix the key in .env" in res.output


def test_health_flags_successful_calls_that_stored_no_fares(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 1)
    api_call_at(dbp, "r1", "ok")
    res = health_log(dbp)
    assert res.exit_code == 1 and "no fares stored in 36h" in res.output


def test_health_flags_undelivered_alerts_by_title(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 1)
    with Database(dbp) as d:
        d.insert_alert(
            Alert(
                created_at=datetime.now(UTC),
                watchlist_id="sea-sjc",
                kind=AlertKind.BUY,
                regime=Regime.PRIME,
                message="BUY SEA→SJC $269\nbody",
                alert_key="k",
            )
        )
    res = health_log(dbp)
    assert res.exit_code == 1
    assert "1 alert(s) not delivered" in res.output and "BUY SEA→SJC $269" in res.output


def test_health_flags_missing_and_stale_backups_once_one_is_owed(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    backups = tmp_path / "backups"
    run_at(dbp, 40, "old")
    run_at(dbp, 1, "new")
    res = health_log(dbp, "--backup-dir", str(backups))
    assert res.exit_code == 1 and "database backup is stale" in res.output
    assert "no backups in" in res.output

    old = fresh_backup(tmp_path) / "fares-20261018-0300.db"
    stamp = (datetime.now(UTC) - timedelta(hours=50)).timestamp()
    os.utime(old, (stamp, stamp))
    res = health_log(dbp, "--backup-dir", str(backups))
    assert res.exit_code == 1 and "is 50h old" in res.output

    os.utime(old)
    assert health_log(dbp, "--backup-dir", str(backups)).exit_code == 0


def test_health_does_not_ask_for_a_backup_before_one_is_due(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 5)
    assert health_log(dbp, "--backup-dir", str(tmp_path / "none")).exit_code == 0


def test_health_flags_low_disk(tmp_path: Path, no_ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 1)
    res = health_log(dbp, "--min-free-mb", str(10**9))
    assert res.exit_code == 1 and "disk almost full" in res.output


def test_health_sends_one_ping_listing_every_problem_with_its_fix(tmp_path: Path, ntfy_env: None):
    dbp = tmp_path / "fares.db"
    run_at(dbp, 40, status="error", error="boom")
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, ["health", "--db", str(dbp)])
    assert res.exit_code == 1
    assert route.call_count == 1
    req = route.calls.last.request
    assert req.headers["Title"] == "fare tracker: no run in 36h"
    assert req.headers["Priority"] == "4"
    body = req.content.decode()
    assert "last poll failed: boom" in body and "database backup is stale" in body
    assert body.count("Fix: ") == 3


# -- poll: failures before or during a run ping the phone -------------------------------------- #


def test_poll_with_no_providers_pings_with_the_fix(
    tmp_path: Path, poll_env: Path, ntfy_env: None, monkeypatch: pytest.MonkeyPatch
):
    for var in ("SERPAPI_KEY", "AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    cfg = write(tmp_path, POLL_YAML)
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, ["poll", "--db", str(tmp_path / "x.db"), "--config", str(cfg)])
    assert res.exit_code == 1 and "no providers configured" in res.output
    req = route.calls.last.request
    assert req.headers["Title"] == "fare tracker: poll failed"
    assert req.headers["Priority"] == "4"
    assert "Fix: set SERPAPI_KEY" in req.content.decode()


def test_poll_with_an_invalid_config_pings_and_exits_1(
    tmp_path: Path, poll_env: Path, ntfy_env: None
):
    cfg = write(tmp_path, "watchlists: [ {id: 1")
    with respx.mock(assert_all_called=False) as m:
        route = m.post(TOPIC_URL).respond(200)
        res = runner.invoke(app, ["poll", "--db", str(tmp_path / "x.db"), "--config", str(cfg)])
    assert res.exit_code == 1 and "cannot load" in res.output
    assert "watchlists.yaml is invalid" in route.calls.last.request.content.decode()


@freeze_time(NOW)
def test_poll_crash_marks_the_run_errored_and_pings(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("src.runner.poll._Poller.run", boom)
    args, dbp = poll_args(tmp_path)
    res = runner.invoke(app, [*args, "--notifier", "log"])
    assert res.exit_code == 1
    assert "fare tracker: poll failed" in res.output
    [run] = rows(dbp, "SELECT * FROM runs")
    assert run["status"] == "error" and "locked" in run["error"]


def test_poll_failure_with_the_null_notifier_sends_nothing(
    tmp_path: Path, poll_env: Path, monkeypatch: pytest.MonkeyPatch
):
    for var in ("SERPAPI_KEY", "AMADEUS_CLIENT_ID", "AMADEUS_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    cfg = write(tmp_path, POLL_YAML)
    res = runner.invoke(
        app, ["poll", "--db", str(tmp_path / "x.db"), "--config", str(cfg), "--notifier", "null"]
    )
    assert res.exit_code == 1 and "poll failed" not in res.output
