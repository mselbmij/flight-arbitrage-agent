"""Phase 2 review fix: src/runner/demo.py (demo watchlists for `fares seed` without --config)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from src.models import Defaults, WatchlistFile
from src.runner.demo import demo_watchlist_file, write_demo_config

END = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def test_demo_has_three_watchlists_with_the_agreed_ceilings():
    wf = demo_watchlist_file(END)
    assert [(w.id, w.ceiling_cents) for w in wf.watchlists] == [
        ("demo-late", 24000),
        ("demo-prime", 26000),
        ("demo-monitor", 38000),
    ]
    assert wf.defaults == Defaults()


def test_demo_targets_are_relative_to_end():
    targets = {t.target_id: t for r in demo_watchlist_file(END).resolved() for t in r.targets}
    assert set(targets) == {
        "demo-late:SEASJC:2026-10-27:ow",
        "demo-prime:SEASFO:2026-11-16:2026-11-19",
        "demo-prime:SEAOAK:2026-11-16:2026-11-19",
        "demo-monitor:SEAORD:2026-12-31:2027-01-04",
    }
    assert {t.origin for t in targets.values()} == {"SEA"}
    dtds = {t.watchlist_id: t.days_to_departure(END) for t in targets.values()}
    assert dtds == {"demo-late": 10, "demo-prime": 30, "demo-monitor": 75}
    assert targets["demo-late:SEASJC:2026-10-27:ow"].return_ is None
    assert targets["demo-monitor:SEAORD:2026-12-31:2027-01-04"].trip_length_days == 4


def test_demo_dates_move_with_end():
    later = datetime(2027, 3, 1, 8, 0, tzinfo=UTC)
    first = demo_watchlist_file(later).resolved()[0].targets[0]
    assert first.depart == date(2027, 3, 11)


def test_written_yaml_has_the_shape_of_the_live_config(tmp_path: Path):
    path = tmp_path / "config" / "watchlists.demo.yaml"
    write_demo_config(path, demo_watchlist_file(END))
    data = yaml.safe_load(path.read_text())
    assert list(data) == ["version", "defaults", "watchlists"]
    wl = data["watchlists"][0]
    assert wl["depart"] == "2026-10-27" and isinstance(wl["depart"], str)
    assert "return" not in wl  # one-way: None is omitted, not written as null
    assert data["watchlists"][2]["return"] == "2027-01-04"
    assert "return_" not in path.read_text()  # alias, not field name


def test_written_yaml_round_trips_to_an_equal_watchlist_file(tmp_path: Path):
    wf = demo_watchlist_file(END)
    path = tmp_path / "demo.yaml"
    write_demo_config(path, wf)
    assert WatchlistFile.model_validate(yaml.safe_load(path.read_text())) == wf
