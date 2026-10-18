"""Demo watchlists for `fares seed` without `--config` (ARCHITECTURE.md section 7).

Seed data lives in its own database (`data/seed.db`) and is described by its own config
(`config/watchlists.demo.yaml`), never by the live `config/watchlists.yaml`. Targets are placed
relative to the seed's last observation so every interesting feature of the generator lands
inside the generated history, whatever day the command is run on.

With the default 45 days of history ending at `end`:

* demo-late    one-way, DTD 10 at `end`: history covers DTD 10..54, so the dip (34), the Basic
               Economy trap (30) and both fence steps (21, 14) all appear.
* demo-prime   DTD 30 at `end`: covers 30..74, dip and trap, no fence steps.
* demo-monitor DTD 75 at `end`: covers 75..119; the generator clamps the dip and trap into range.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from src.models import Defaults, Watchlist, WatchlistFile

HEADER = (
    "# Demo watchlists written by `fares seed` to go with data/seed.db. Not the live config:\n"
    "# regenerated on every seed, so edit config/watchlists.yaml instead.\n"
)


def demo_watchlist_file(end: datetime) -> WatchlistFile:
    """Three demo watchlists from SEA, departures relative to `end.date()`."""
    day = end.date()

    def at(offset: int) -> date:
        return day + timedelta(days=offset)

    specs: list[dict[str, Any]] = [
        {
            "id": "demo-late",
            "origin": "SEA",
            "destination": "SJC",
            "depart": at(10),
            "ceiling_cents": 24000,
        },
        {
            "id": "demo-prime",
            "origin": "SEA",
            "destination": ["SFO", "OAK"],
            "depart": at(30),
            "trip_length": {"min_days": 3, "max_days": 3},
            "ceiling_cents": 26000,
        },
        {
            "id": "demo-monitor",
            "origin": "SEA",
            "destination": "ORD",
            "depart": at(75),
            "return": at(79),
            "ceiling_cents": 38000,
        },
    ]
    return WatchlistFile(
        version=1,
        defaults=Defaults(),
        watchlists=[Watchlist.model_validate(s) for s in specs],
    )


def write_demo_config(path: Path, wf: WatchlistFile) -> None:
    """Dump `wf` as YAML in the shape of config/watchlists.yaml (dates as ISO strings)."""
    data = wf.model_dump(mode="json", by_alias=True, exclude_none=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False))
