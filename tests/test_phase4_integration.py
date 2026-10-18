"""Fixture -> parse -> risk filter: the SerpAPI fixtures were built to trip specific rules."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.engine.risk_filters import apply
from src.ingestion.serpapi_client import parse
from src.models import Constraints, Watchlist, WatchlistFile
from tests.conftest import T0, thanksgiving

FIX = Path(__file__).parent / "fixtures" / "serpapi"
HASH = "int-hash-1"


def parsed_and_filtered(name: str, parse_wl=None, risk_wl=None):
    parse_wl = parse_wl or thanksgiving()
    payload = json.loads((FIX / name).read_text())
    res = parse(
        payload, target=parse_wl.targets[0], watchlist=parse_wl, run_id="r1", observed_at=T0
    )
    return apply(res.observations, risk_wl or parse_wl, config_hash=HASH)


def test_rt_fixture_verdicts_match_the_scenarios_it_encodes():
    out = parsed_and_filtered("serpapi_sea_sjc_rt_exclude_basic.json")
    assert [(o.risk.passed, o.risk.rejections) for o in out] == [
        (True, []),  # AS nonstop 07:15
        (True, []),  # DL via SLC, 95 min layover
        (False, ["departs_before_06:00"]),  # AS 05:40
        (False, ["layover>180", "overnight_layover"]),  # UA via SFO, 14h overnight
    ]


def test_coterminal_fixture_rejects_only_the_swap_itinerary():
    w = Watchlist(id="sea-bos-march", origin="SEA", destination="BOS", depart=date(2027, 3, 12),
                  ceiling_cents=45000)  # fmt: skip
    wl = WatchlistFile(version=1, watchlists=[w]).resolved()[0]
    out = parsed_and_filtered("serpapi_sea_jfk_coterminal.json", wl)
    assert [o.risk.rejections for o in out] == [[], ["coterminal_swap"]]


def test_basic_extension_fixture_is_rejected_and_unknown_is_flagged_not_rejected():
    allow = thanksgiving().model_copy(update={"constraints": Constraints(allow_basic_economy=True)})
    out = parsed_and_filtered(
        "serpapi_no_basic_filter_with_basic_extension.json", parse_wl=allow, risk_wl=thanksgiving()
    )
    by_price = {o.price_cents: o.risk for o in out}
    assert by_price[12900].rejections == ["basic_economy"]
    assert by_price[13900].rejections == ["basic_economy"]
    assert (by_price[24900].passed, by_price[24900].brand_unverified) == (True, True)
