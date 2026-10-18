"""Phase 5: src/runner/poll.py (ARCHITECTURE.md sections 3, 6, 7, 7a; ops/phase-5-brief.md).

`run_poll` is driven end to end with MockProvider on a tmp fixture dir and a tmp DB. The injected
`now` is a mutable Clock set in 2030 so that the real wall clock never matters (no freezegun: the
DB rejects naive datetimes and every stamp here is tz-aware). Fixtures are built by constructing
models and dumping JSON, one file per target, named `<target_id with ':' -> '_'>.json`.

Every target gets three observations, all round trips:
  * a cheap Basic Economy trap (nonstop AS 331, $199.00)  -> risk-rejected, must never alert
  * a mid Main Cabin one-stop DL 1553 via SLC ($280.00)   -> the cheapest clean fare
  * an expensive Main Cabin nonstop AS 327 ($340.00)
and a band typical-low $300 / typical-high $400. With the default PRIME strike (typical_low x 1.00)
and ceiling $290 the mid fare is the BUY.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.db import BudgetLimits, Database
from src.ingestion.base import ProviderResult
from src.ingestion.mock_client import MockProvider
from src.models import (
    Alert,
    AlertKind,
    BenchmarkBand,
    FareBrand,
    FareObservation,
    Itinerary,
    PriceLevel,
    Provider,
    ResolvedWatchlist,
    SearchTarget,
    Segment,
    Slice,
    WatchlistFile,
)
from src.runner.poll import (
    Notifier,
    NullNotifier,
    ProviderRegistry,
    run_poll,
    select_targets,
)
from tests.conftest import Clock, make_alert, make_band, make_call, make_obs, make_run

PT = ZoneInfo("America/Los_Angeles")
MT = ZoneInfo("America/Denver")
BASE = datetime(2030, 3, 1, 12, 0, tzinfo=UTC)
LIMITS = BudgetLimits(max_per_run=10, max_per_day=100)

TRAP, MID, TOP = 19900, 28000, 34000
BAND = (30000, 40000)


def strike_all(reference: str, mult: float) -> dict:
    rule = {"reference": reference, "multiplier": mult}
    return {"monitor": rule, "prime": rule, "fence": rule, "late": rule}


# 2030-03-01 + 39 days = 2030-04-09 (PRIME); + 54 = 2030-04-24 (PRIME); + 20 = 2030-03-21 (FENCE)
RT = {
    "id": "rt-one",
    "origin": "SEA",
    "destination": "SJC",
    "depart": date(2030, 4, 9),
    "return": date(2030, 4, 13),
    "ceiling_cents": 29000,
    "poll": {"providers": ["mock"], "targets_per_run": 1},
}
GRID = {
    "id": "grid-three",
    "origin": "SEA",
    "destination": ["SJC", "SFO", "OAK"],
    "depart": date(2030, 4, 24),
    "return": date(2030, 4, 28),
    "ceiling_cents": 29000,
    "poll": {"providers": ["mock"], "targets_per_run": 2},
}
FOUR = {
    **GRID,
    "id": "trip-four",
    "destination": ["SJC", "SFO", "OAK", "LAS"],
    "poll": {"providers": ["mock"], "targets_per_run": 4},
}
FENCE_ONE = {
    **RT,
    "id": "fence-one",
    "depart": date(2030, 3, 21),
    "return": date(2030, 3, 25),
}
RT_TID = "rt-one:SEASJC:2030-04-09:2030-04-13"


def wl_dict(base: dict, **over) -> dict:
    merged = {**base, **over}
    if "poll" in over:
        merged["poll"] = {**base["poll"], **over["poll"]}
    return merged


def wf_of(*watchlists: dict) -> WatchlistFile:
    return WatchlistFile.model_validate({"version": 1, "watchlists": list(watchlists)})


# --------------------------------------------------------------------------- #
# fixtures-on-disk builders
# --------------------------------------------------------------------------- #


def itin(t: SearchTarget, flight: str, stops: int) -> Itinerary:
    d, r = t.depart, t.return_
    assert r is not None

    def at(day: date, h: int, m: int, tz: ZoneInfo = PT) -> datetime:
        return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)

    if stops == 0:
        out = [
            Segment(carrier="AS", flight_number=flight, origin=t.origin, destination=t.destination,
                    departure_local=at(d, 7, 0), arrival_local=at(d, 9, 25)),
        ]  # fmt: skip
    else:
        out = [
            Segment(carrier="DL", flight_number=flight, origin=t.origin, destination="SLC",
                    departure_local=at(d, 6, 30), arrival_local=at(d, 9, 15, MT)),
            Segment(carrier="DL", flight_number="2711", origin="SLC", destination=t.destination,
                    departure_local=at(d, 10, 30, MT), arrival_local=at(d, 11, 50)),
        ]  # fmt: skip
    back = Segment(carrier="AS", flight_number="328", origin=t.destination, destination=t.origin,
                   departure_local=at(r, 18, 0), arrival_local=at(r, 20, 30))  # fmt: skip
    return Itinerary(slices=[Slice(segments=out), Slice(segments=[back])])


def fare(
    t: SearchTarget, flight: str, stops: int, price: int, brand: FareBrand, *, url: bool = False
):
    return FareObservation(
        watchlist_id=t.watchlist_id,
        target_id=t.target_id,
        observed_at=BASE,
        provider=Provider.MOCK,
        run_id="fixture",
        itinerary=itin(t, flight, stops),
        price_cents=price,
        currency="USD",
        fare_brand=brand,
        fare_brand_raw="Basic" if brand is FareBrand.BASIC_ECONOMY else "Main",
        changeable=brand is not FareBrand.BASIC_ECONOMY,
        days_to_departure=0,
        booking_url=f"https://book.example/{flight}" if url else None,
    )


def dump(model) -> dict:
    return model.model_dump(
        mode="json", exclude={"itinerary_key": True, "itinerary": {"itinerary_key": True}}
    )


def write_fixture(
    fx: Path,
    t: SearchTarget,
    *,
    trap: int = TRAP,
    mid: int = MID,
    top: int = TOP,
    band: tuple[int, int] | None = BAND,
    urls: bool = False,
) -> None:
    obs = [
        fare(t, "331", 0, trap, FareBrand.BASIC_ECONOMY, url=urls),
        fare(t, "1553", 1, mid, FareBrand.MAIN_CABIN, url=urls),
        fare(t, "327", 0, top, FareBrand.MAIN_CABIN, url=urls),
    ]
    payload = {
        "observations": [dump(o) for o in obs],
        "band": None
        if band is None
        else dump(
            BenchmarkBand(
                watchlist_id=t.watchlist_id,
                target_id=t.target_id,
                observed_at=BASE,
                provider=Provider.MOCK,
                run_id="fixture",
                typical_low_cents=band[0],
                typical_high_cents=band[1],
                price_level=PriceLevel.LOW,
                currency="USD",
            )
        ),
        "raw": {"source": "test", "target": t.target_id},
    }
    (fx / (t.target_id.replace(":", "_") + ".json")).write_text(json.dumps(payload))


def write_fixtures(fx: Path, wf: WatchlistFile, *, skip: frozenset[str] = frozenset(), **kw):
    for wl in wf.resolved():
        for t in wl.targets:
            if t.target_id not in skip:
                write_fixture(fx, t, **kw)


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class RecordingNotifier:
    """Returns `channel` (None = delivery failed) and remembers every alert it was handed."""

    def __init__(self, channel: str | None = "ntfy") -> None:
        self.channel = channel
        self.alerts: list[Alert] = []
        self.clicks: list[str | None] = []

    def send(self, alert: Alert, *, click: str | None = None) -> str | None:
        self.alerts.append(alert)
        self.clicks.append(click)
        return self.channel


class RaisingProvider:
    """A provider with a bug: raises something that is not a ProviderError."""

    name = Provider.MOCK

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def search(self, target, watchlist, run_id, observed_at):
        self.calls += 1
        raise self.exc


class RelabelProvider:
    """MockProvider output re-stamped as another provider at a shifted price."""

    def __init__(self, inner: MockProvider, name: Provider, shift: int) -> None:
        self.inner, self.name, self.shift = inner, name, shift

    def search(self, target, watchlist, run_id, observed_at):
        r = self.inner.search(target, watchlist, run_id, observed_at)
        obs = [
            o.model_copy(update={"provider": self.name, "price_cents": o.price_cents + self.shift})
            for o in r.observations
        ]
        band = None if r.band is None else r.band.model_copy(update={"provider": self.name})
        return ProviderResult(observations=obs, band=band, raw=r.raw)


@pytest.fixture
def clock() -> Clock:  # overrides conftest's: Database(now=clock) and run_poll(now=clock) agree
    return Clock(BASE)


class Harness:
    def __init__(self, db: Database, clock: Clock, tmp_path: Path) -> None:
        self.db, self.clock = db, clock
        self.fx = tmp_path / "fixtures"
        self.fx.mkdir(exist_ok=True)
        self.raw = tmp_path / "raw"
        self.notifier = RecordingNotifier()

    def fixtures(self, wf: WatchlistFile, **kw) -> None:
        write_fixtures(self.fx, wf, **kw)

    def registry(self) -> ProviderRegistry:
        return ProviderRegistry({Provider.MOCK: MockProvider(self.fx)})

    def poll(
        self,
        wf: WatchlistFile,
        *,
        run_id: str | None = None,
        trigger: str = "manual",
        limits: BudgetLimits = LIMITS,
        notifier: Notifier | None = None,
        providers: ProviderRegistry | None = None,
    ):
        return run_poll(
            self.db,
            wf,
            providers or self.registry(),
            limits=limits,
            notifier=notifier or self.notifier,
            raw_dir=self.raw,
            trigger=trigger,
            now=self.clock,
            run_id=run_id,
        )

    def q(self, sql: str, *args) -> list[dict]:
        return [dict(r) for r in self.db.con.execute(sql, args)]

    def alerts(self, kind: AlertKind | None = None) -> list[dict]:
        if kind is None:
            return self.q("SELECT * FROM alerts ORDER BY id")
        return self.q("SELECT * FROM alerts WHERE kind=? ORDER BY id", kind.value)


@pytest.fixture
def h(db: Database, clock: Clock, tmp_path: Path) -> Harness:
    return Harness(db, clock, tmp_path)


def seed_prior(h: Harness, target_id: str, watchlist_id: str, price: int, *, passed: bool = True):
    """A finished earlier run with one observation of `target_id` at `price`."""
    when = BASE - timedelta(days=1)
    h.db.start_run(make_run("prior", started_at=when))
    h.db.insert_observations(
        [
            make_obs(
                "prior",
                price=price,
                passed=passed,
                watchlist_id=watchlist_id,
                target_id=target_id,
                observed_at=when,
            )
        ]
    )


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


@pytest.fixture
def happy(h: Harness):
    wf = wf_of(RT, GRID)
    h.fixtures(wf)
    return h, wf, h.poll(wf)


def test_poll_happy_path_returns_a_finished_ok_run(happy):
    h, _, run = happy
    assert run.status == "ok"
    assert run.calls_made == 3  # 1 rt-one target + targets_per_run=2 of the 3 grid targets
    assert run.trigger == "manual"
    assert run.started_at == BASE
    assert run.finished_at is not None
    assert run.error is None
    assert h.db.get_run(run.run_id) == run


def test_poll_happy_path_row_counts(happy):
    h, _, _ = happy
    assert h.db.counts() == {
        "runs": 1,
        "api_calls": 3,
        "observations": 9,
        "benchmark_bands": 3,
        "alerts": 3,
    }


def test_poll_writes_one_ok_ledger_row_per_call_with_endpoint_and_latency(happy):
    h, _, run = happy
    rows = h.q("SELECT * FROM api_calls ORDER BY id")
    assert [r["status"] for r in rows] == ["ok"] * 3
    assert {r["run_id"] for r in rows} == {run.run_id}
    assert {r["provider"] for r in rows} == {"mock"}
    assert {r["cost_units"] for r in rows} == {1}
    assert all(r["latency_ms"] is not None for r in rows)
    assert all(r["called_at"].startswith("2030-03-01T12:00:00") for r in rows)
    by_wl = Counter(r["watchlist_id"] for r in rows)
    assert by_wl == {"rt-one": 1, "grid-three": 2}
    for r in rows:
        assert r["endpoint"] == "search:" + r["target_id"].split(":")[1]  # e.g. search:SEASJC


def test_poll_honours_targets_per_run_for_the_grid(happy):
    h, _, _ = happy
    grid_targets = {
        r["target_id"]
        for r in h.q("SELECT target_id FROM api_calls WHERE watchlist_id='grid-three'")
    }
    assert len(grid_targets) == 2


def test_poll_buys_the_cheapest_clean_fare_for_every_polled_target_and_never_the_trap(happy):
    h, _, _ = happy
    alerts = h.alerts()
    assert [a["kind"] for a in alerts] == ["buy"] * 3
    assert {a["price_cents"] for a in alerts} == {MID}
    assert len({a["target_id"] for a in alerts}) == 3
    for a in alerts:
        obs = h.db.observation(a["observation_id"])
        assert obs is not None
        assert obs.price_cents == MID
        assert obs.risk is not None and obs.risk.passed


def test_poll_stores_the_risk_verdict_of_the_trap_and_stamps_the_config_hash(happy):
    h, wf, run = happy
    traps = h.q("SELECT * FROM observations WHERE fare_brand='basic_economy'")
    assert len(traps) == 3
    assert {r["risk_passed"] for r in traps} == {0}
    assert {r["risk_rejections_json"] for r in traps} == {'["basic_economy"]'}
    rows = h.q("SELECT * FROM observations")
    assert {r["risk_config_hash"] for r in rows} == {wf.config_hash()}
    assert {r["run_id"] for r in rows} == {run.run_id}
    assert {r["provider"] for r in rows} == {"mock"}
    assert all(r["risk_passed"] is not None for r in rows)


def test_poll_stores_a_band_per_polled_target(happy):
    h, _, _ = happy
    bands = h.q("SELECT * FROM benchmark_bands")
    assert {(b["typical_low_cents"], b["typical_high_cents"]) for b in bands} == {BAND}


def test_poll_delivers_every_new_alert_to_the_notifier(happy):
    h, _, _ = happy
    assert sorted(a.alert_key for a in h.notifier.alerts) == sorted(
        r["alert_key"] for r in h.alerts()
    )


def test_poll_generates_a_hex_run_id_unless_one_is_given(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf)
    assert re.fullmatch(r"[0-9a-f]{32}", h.poll(wf).run_id)
    assert h.poll(wf, run_id="my-run").run_id == "my-run"


def test_poll_records_the_trigger(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf)
    run = h.poll(wf, trigger="cron")
    assert run.trigger == "cron"
    assert h.q('SELECT "trigger" FROM runs WHERE run_id=?', run.run_id)[0]["trigger"] == "cron"


def test_poll_migrates_a_fresh_database_itself(tmp_path: Path, clock: Clock):
    wf = wf_of(RT)
    with Database(tmp_path / "fresh.db", now=clock) as fresh:  # note: no migrate()
        hh = Harness(fresh, clock, tmp_path)
        hh.fixtures(wf)
        assert hh.poll(wf).status == "ok"
        assert fresh.counts()["observations"] == 3


def test_poll_skips_paused_watchlists(h: Harness):
    wf = wf_of(RT, {**RT, "id": "rt-paused", "status": "paused"})
    h.fixtures(wf)
    run = h.poll(wf)
    assert run.calls_made == 1
    assert {r["watchlist_id"] for r in h.q("SELECT watchlist_id FROM api_calls")} == {"rt-one"}


# --------------------------------------------------------------------------- #
# raw payloads
# --------------------------------------------------------------------------- #


def test_poll_writes_the_raw_payload_under_run_id_provider_and_target(happy):
    h, _, run = happy
    path = h.raw / run.run_id / f"mock-{RT_TID.replace(':', '_')}.json"
    assert path.is_file()
    assert json.loads(path.read_text()) == {"source": "test", "target": RT_TID}
    assert len(list((h.raw / run.run_id).glob("mock-*.json"))) == 3


def test_poll_points_each_observation_at_its_raw_file_relative_to_raw_dir(happy):
    h, _, run = happy
    refs = h.q("SELECT target_id, raw_payload_ref FROM observations")
    assert len(refs) == 9
    for r in refs:
        assert Path(r["raw_payload_ref"]) == Path(run.run_id) / (
            "mock-" + r["target_id"].replace(":", "_") + ".json"
        )
        assert (h.raw / r["raw_payload_ref"]).is_file()


# --------------------------------------------------------------------------- #
# lowest_seen is read BEFORE this run's rows are inserted
# --------------------------------------------------------------------------- #


def lowest_seen_wf() -> WatchlistFile:
    return wf_of(wl_dict(RT, ceiling_cents=10000, strike=strike_all("lowest_seen", 1.0)))


@pytest.mark.parametrize(
    ("prior", "mid", "mult", "fires"),
    [
        (40000, 30000, 1.0, True),  # this run is a new low: 30000 <= 40000
        (30000, 30000, 1.0, True),  # equal to history
        (30000, 31000, 1.0, False),  # dearer than history
        (None, 28000, 1.0, False),  # no history: nothing to be below (this run is not history)
        (40000, 38000, 0.95, True),  # 38000 <= 40000 x 0.95 exactly, vs the prior low only
        (40000, 38001, 0.95, False),  # one cent over; if this run's rows counted, ref=38001 too
    ],
)
def test_poll_lowest_seen_reference_excludes_this_runs_own_rows(
    h: Harness, prior: int | None, mid: int, mult: float, fires: bool
):
    wf = wf_of(
        wl_dict(RT, ceiling_cents=10000, strike=strike_all("lowest_seen", mult)),
    )
    h.fixtures(wf, mid=mid, top=99000)
    if prior is not None:
        seed_prior(h, RT_TID, "rt-one", prior)
    h.poll(wf)
    alerts = h.alerts()
    if fires:
        assert [(a["kind"], a["price_cents"], a["reference_cents"]) for a in alerts] == [
            ("buy", mid, prior)
        ]
    else:
        assert alerts == []


def test_poll_no_history_and_no_ceiling_hit_never_buys_on_lowest_seen(h: Harness):
    """The trap: if this run's rows counted, the cheapest clean fare would be its own reference."""
    wf = lowest_seen_wf()
    h.fixtures(wf)
    h.poll(wf)
    assert h.alerts() == []
    assert h.db.counts()["observations"] == 3


def test_poll_no_history_still_buys_when_the_ceiling_is_hit(h: Harness):
    wf = wf_of(wl_dict(RT, ceiling_cents=MID, strike=strike_all("lowest_seen", 1.0)))
    h.fixtures(wf)
    h.poll(wf)
    [alert] = h.alerts()
    assert (alert["kind"], alert["price_cents"], alert["reference_cents"]) == ("buy", MID, MID)


def test_poll_lowest_seen_ignores_prior_rejected_rows(h: Harness):
    wf = lowest_seen_wf()
    h.fixtures(wf)
    seed_prior(h, RT_TID, "rt-one", 20000, passed=False)  # a past trap: never a reference
    h.poll(wf)
    assert h.alerts() == []


# --------------------------------------------------------------------------- #
# the band used for typical_low strikes
# --------------------------------------------------------------------------- #


def band_wf() -> WatchlistFile:
    return wf_of(wl_dict(RT, ceiling_cents=10000))  # only the band can strike


def test_poll_first_ever_poll_strikes_against_the_band_that_call_returned(h: Harness):
    wf = band_wf()
    h.fixtures(wf)
    h.poll(wf)
    [alert] = h.alerts()
    assert (alert["kind"], alert["price_cents"], alert["reference_cents"]) == ("buy", MID, BAND[0])


def test_poll_this_calls_band_supersedes_a_stale_stored_band(h: Harness):
    wf = band_wf()
    h.fixtures(wf)
    h.db.start_run(make_run("prior", started_at=BASE - timedelta(days=1)))
    h.db.insert_band(
        make_band(
            "prior",
            watchlist_id="rt-one",
            target_id=RT_TID,
            observed_at=BASE - timedelta(days=1),
            typical_low_cents=20000,
            typical_high_cents=25000,
        )
    )
    h.poll(wf)
    [alert] = h.alerts()
    assert (alert["price_cents"], alert["reference_cents"]) == (MID, BAND[0])


def test_poll_falls_back_to_the_stored_band_when_the_provider_returns_none(h: Harness):
    wf = band_wf()
    h.fixtures(wf, band=None)
    h.db.start_run(make_run("prior", started_at=BASE - timedelta(days=1)))
    h.db.insert_band(
        make_band(
            "prior",
            watchlist_id="rt-one",
            target_id=RT_TID,
            observed_at=BASE - timedelta(days=1),
            typical_low_cents=30000,
            typical_high_cents=40000,
        )
    )
    h.poll(wf)
    [alert] = h.alerts()
    assert (alert["price_cents"], alert["reference_cents"]) == (MID, 30000)
    assert h.db.counts()["benchmark_bands"] == 1  # nothing new to store


def test_poll_no_band_anywhere_and_no_ceiling_hit_is_silent(h: Harness):
    wf = band_wf()
    h.fixtures(wf, band=None)
    run = h.poll(wf)
    assert run.status == "ok"
    assert h.alerts() == []
    assert h.db.counts()["benchmark_bands"] == 0


# --------------------------------------------------------------------------- #
# circuit breaker mid-run (ARCHITECTURE section 6)
# --------------------------------------------------------------------------- #


def test_poll_run_gate_trips_mid_run_and_stops_everything_after(h: Harness):
    wf = wf_of(FOUR)  # four targets selected, two calls allowed
    h.fixtures(wf)
    run = h.poll(wf, limits=BudgetLimits(max_per_run=2, max_per_day=100))
    assert run.status == "budget_tripped"
    assert run.calls_made == 2
    assert run.finished_at is not None
    assert h.db.get_run(run.run_id) == run

    ledger = h.q("SELECT status, target_id FROM api_calls ORDER BY id")
    assert [r["status"] for r in ledger] == ["ok", "ok", "skipped_breaker"]
    assert len({r["target_id"] for r in ledger}) == 3  # the fourth target was never touched
    polled = {r["target_id"] for r in ledger if r["status"] == "ok"}
    assert {r["target_id"] for r in h.q("SELECT target_id FROM observations")} == polled
    assert h.db.counts()["observations"] == 6

    assert Counter(a["kind"] for a in h.alerts()) == {"buy": 2, "budget_tripped": 1}
    [tripped] = h.alerts(AlertKind.BUDGET_TRIPPED)
    assert tripped["message"] == "API budget tripped (2/2 run)"
    assert tripped["watchlist_id"] == "trip-four"
    assert tripped["target_id"] is None
    assert tripped["alert_key"].startswith("trip-four:budget_tripped:")
    assert len(h.notifier.alerts) == 3


def test_poll_breaker_stops_later_watchlists_too(h: Harness):
    wf = wf_of(RT, GRID)
    h.fixtures(wf)
    run = h.poll(wf, limits=BudgetLimits(max_per_run=1, max_per_day=100))
    assert run.status == "budget_tripped"
    assert run.calls_made == 1
    ledger = h.q("SELECT status, watchlist_id FROM api_calls ORDER BY id")
    assert [(r["status"], r["watchlist_id"]) for r in ledger] == [
        ("ok", "rt-one"),
        ("skipped_breaker", "grid-three"),
    ]
    assert {r["watchlist_id"] for r in h.q("SELECT watchlist_id FROM observations")} == {"rt-one"}
    [tripped] = h.alerts(AlertKind.BUDGET_TRIPPED)
    assert tripped["message"] == "API budget tripped (1/1 run)"
    assert tripped["watchlist_id"] == "grid-three"


def test_poll_daily_gate_trips_with_the_day_gate_in_the_message_and_dedups_across_runs(
    h: Harness,
):
    wf = wf_of(RT)
    h.fixtures(wf)
    spent = BASE - timedelta(hours=1)
    h.db.start_run(make_run("earlier", started_at=spent))
    h.db.reserve_api_call(
        make_call("earlier", at=spent), BudgetLimits(max_per_run=9, max_per_day=9)
    )
    tight = BudgetLimits(max_per_run=10, max_per_day=1)

    first = h.poll(wf, limits=tight)
    assert first.status == "budget_tripped"
    assert first.calls_made == 0
    assert h.db.counts()["observations"] == 0
    [tripped] = h.alerts(AlertKind.BUDGET_TRIPPED)
    assert tripped["message"] == "API budget tripped (1/1 day)"

    h.clock.t = BASE + timedelta(hours=1)  # same UTC day, still over budget
    second = h.poll(wf, limits=tight)
    assert second.status == "budget_tripped"
    assert len(h.alerts(AlertKind.BUDGET_TRIPPED)) == 1  # one alert per 24h, not one per run
    assert len(h.notifier.alerts) == 1


# --------------------------------------------------------------------------- #
# ProviderError: a bad call is a ledger row and an alert, not a dead run
# --------------------------------------------------------------------------- #


def test_poll_provider_error_is_logged_alerted_and_the_run_carries_on(h: Harness):
    wf = wf_of(RT, GRID)
    h.fixtures(wf, skip=frozenset({RT_TID}))  # MockProvider raises ProviderError("no fixture ...")
    run = h.poll(wf)

    assert run.status == "ok"
    assert run.calls_made == 3  # the failed call was still a paid-for unit
    ledger = {r["target_id"]: r for r in h.q("SELECT * FROM api_calls")}
    assert ledger[RT_TID]["status"] == "error"
    assert "no fixture" in ledger[RT_TID]["error"]
    assert ledger[RT_TID]["latency_ms"] is not None
    assert Counter(r["status"] for r in ledger.values()) == {"error": 1, "ok": 2}

    [err] = h.alerts(AlertKind.PROVIDER_ERROR)
    assert err["watchlist_id"] == "rt-one"
    assert err["alert_key"] == f"rt-one:provider_error:{RT_TID}:mock"
    assert "mock" in err["message"]
    assert RT_TID in err["message"]
    assert "no fixture" in err["message"]

    assert Counter(a["kind"] for a in h.alerts()) == {"buy": 2, "provider_error": 1}
    assert h.db.counts()["observations"] == 6  # both grid targets were still polled
    assert len(h.notifier.alerts) == 3


def test_poll_repeated_provider_error_is_not_re_alerted_inside_the_window(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf, skip=frozenset({RT_TID}))
    h.poll(wf)
    h.clock.t = BASE + timedelta(hours=6)
    h.poll(wf)
    assert len(h.alerts(AlertKind.PROVIDER_ERROR)) == 1
    assert len(h.notifier.alerts) == 1
    assert h.q("SELECT COUNT(*) AS n FROM api_calls WHERE status='error'")[0]["n"] == 2


# --------------------------------------------------------------------------- #
# an unexpected exception is a bug: surface it, do not dress it up as an outage
# --------------------------------------------------------------------------- #


def test_poll_unexpected_exception_marks_the_run_errored_and_is_re_raised(h: Harness):
    wf = wf_of(RT, GRID)
    boom = RaisingProvider(RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        h.poll(wf, run_id="run-boom", providers=ProviderRegistry({Provider.MOCK: boom}))

    run = h.db.get_run("run-boom")
    assert run is not None
    assert run.status == "error"
    assert run.error is not None and "boom" in run.error
    assert run.finished_at is not None
    assert boom.calls == 1  # nothing after the bug was attempted

    [call] = h.q("SELECT * FROM api_calls")
    assert call["status"] == "error"
    assert "boom" in call["error"]
    assert h.alerts() == []  # no PROVIDER_ERROR: a crash must not look like a provider outage
    assert h.notifier.alerts == []


# --------------------------------------------------------------------------- #
# dedup across runs
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("hours", "new_alerts"), [(1, 0), (23, 0), (24, 1)])
def test_poll_same_prices_again_alerts_only_once_the_dedup_window_has_passed(
    h: Harness, hours: int, new_alerts: int
):
    wf = wf_of(RT)
    h.fixtures(wf)
    h.poll(wf)
    assert len(h.alerts()) == 1

    h.clock.t = BASE + timedelta(hours=hours)
    run = h.poll(wf)
    assert run.status == "ok"
    assert len(h.alerts()) == 1 + new_alerts  # exactly 24h old no longer suppresses (strict >)
    assert len(h.notifier.alerts) == 1 + new_alerts
    assert h.db.counts()["observations"] == 6  # the data is stored either way


# --------------------------------------------------------------------------- #
# notifier contract
# --------------------------------------------------------------------------- #


def test_poll_marks_delivered_when_the_notifier_returns_a_channel(h: Harness):
    wf = wf_of(RT, GRID)
    h.fixtures(wf)
    h.poll(wf, notifier=RecordingNotifier("ntfy"))
    rows = h.alerts()
    assert len(rows) == 3
    assert {r["channel"] for r in rows} == {"ntfy"}
    assert all(r["delivered_at"] is not None for r in rows)
    assert h.db.undelivered_alerts(max_age_hours=48) == []


def test_poll_leaves_alerts_undelivered_when_the_notifier_returns_none(h: Harness):
    wf = wf_of(RT, GRID)
    h.fixtures(wf)
    h.poll(wf, notifier=RecordingNotifier(None))
    rows = h.alerts()
    assert len(rows) == 3
    assert all(r["delivered_at"] is None and r["channel"] is None for r in rows)
    assert len(h.db.undelivered_alerts(max_age_hours=48)) == 3


def test_poll_with_the_null_notifier_stores_alerts_undelivered(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf)
    run = h.poll(wf, notifier=NullNotifier())
    assert run.status == "ok"
    [row] = h.alerts()
    assert row["delivered_at"] is None
    assert len(h.db.undelivered_alerts(max_age_hours=48)) == 1


def test_null_notifier_send_is_a_no_op_that_reports_no_channel():
    assert NullNotifier().send(make_alert()) is None


# --------------------------------------------------------------------------- #
# providers: registry, one breaker unit per provider call
# --------------------------------------------------------------------------- #


def test_poll_skips_a_provider_the_registry_lacks_with_a_warning(
    h: Harness, caplog: pytest.LogCaptureFixture
):
    wf = wf_of(wl_dict(RT, poll={"providers": ["serpapi", "mock"]}))
    h.fixtures(wf)
    with caplog.at_level(logging.DEBUG):
        run = h.poll(wf)  # registry holds only mock
    assert run.status == "ok"
    assert run.calls_made == 1
    assert {r["provider"] for r in h.q("SELECT provider FROM api_calls")} == {"mock"}
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("serpapi" in r.getMessage().lower() for r in warnings)


def test_poll_each_provider_of_a_watchlist_is_its_own_call_and_raw_file(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"providers": ["mock", "serpapi"]}))
    h.fixtures(wf)
    reg = ProviderRegistry(
        {
            Provider.MOCK: MockProvider(h.fx),
            Provider.SERPAPI: RelabelProvider(MockProvider(h.fx), Provider.SERPAPI, -1000),
        }
    )
    run = h.poll(wf, providers=reg)
    assert run.calls_made == 2
    assert Counter(r["provider"] for r in h.q("SELECT provider FROM api_calls")) == {
        "mock": 1,
        "serpapi": 1,
    }
    assert {r["provider"] for r in h.q("SELECT provider FROM observations")} == {"mock", "serpapi"}
    assert h.db.counts()["observations"] == 6
    stem = RT_TID.replace(":", "_")
    assert (h.raw / run.run_id / f"mock-{stem}.json").is_file()
    assert (h.raw / run.run_id / f"serpapi-{stem}.json").is_file()
    [buy] = h.alerts(AlertKind.BUY)  # one evaluation over both providers' rows: one alert
    assert buy["price_cents"] == MID - 1000
    assert h.db.observation(buy["observation_id"]).provider is Provider.SERPAPI  # type: ignore[union-attr]


def test_poll_run_gate_counts_every_provider_call_not_every_target(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"providers": ["mock", "serpapi"]}))
    h.fixtures(wf)
    reg = ProviderRegistry(
        {
            Provider.MOCK: MockProvider(h.fx),
            Provider.SERPAPI: RelabelProvider(MockProvider(h.fx), Provider.SERPAPI, 0),
        }
    )
    run = h.poll(wf, providers=reg, limits=BudgetLimits(max_per_run=1, max_per_day=100))
    assert run.status == "budget_tripped"
    assert run.calls_made == 1


# --------------------------------------------------------------------------- #
# round robin across runs
# --------------------------------------------------------------------------- #


def test_poll_second_run_picks_the_unpolled_target_and_the_oldest(h: Harness):
    wf = wf_of(GRID)
    h.fixtures(wf)
    first = h.poll(wf)
    first_ids = {
        r["target_id"] for r in h.q("SELECT target_id FROM api_calls WHERE run_id=?", first.run_id)
    }
    all_ids = {t.target_id for t in wf.resolved()[0].targets}
    assert len(first_ids) == 2

    h.clock.t = BASE + timedelta(hours=12)
    second = h.poll(wf)
    second_ids = {
        r["target_id"] for r in h.q("SELECT target_id FROM api_calls WHERE run_id=?", second.run_id)
    }
    assert len(second_ids) == 2
    assert all_ids - first_ids <= second_ids  # the never-polled target goes first
    assert None not in h.db.last_polled(sorted(all_ids)).values()


# --------------------------------------------------------------------------- #
# review fixes: breaker trip between providers, failing targets, times_per_day, ordering
# --------------------------------------------------------------------------- #


def two_provider_registry(h: Harness, shift: int = 0) -> ProviderRegistry:
    return ProviderRegistry(
        {
            Provider.MOCK: MockProvider(h.fx),
            Provider.SERPAPI: RelabelProvider(MockProvider(h.fx), Provider.SERPAPI, shift),
        }
    )


def test_poll_breaker_trip_between_providers_still_evaluates_the_rows_already_paid_for(
    h: Harness,
):
    wf = wf_of(wl_dict(RT, poll={"providers": ["mock", "serpapi"]}))
    h.fixtures(wf)
    run = h.poll(
        wf,
        providers=two_provider_registry(h),
        limits=BudgetLimits(max_per_run=1, max_per_day=100),
    )
    assert run.status == "budget_tripped"
    assert [r["status"] for r in h.q("SELECT status FROM api_calls ORDER BY id")] == [
        "ok",
        "skipped_breaker",
    ]
    assert {r["provider"] for r in h.q("SELECT provider FROM observations")} == {"mock"}
    [buy] = h.alerts(AlertKind.BUY)  # the first provider's BUY must not be discarded
    assert (buy["price_cents"], buy["target_id"]) == (MID, RT_TID)
    [tripped] = h.alerts(AlertKind.BUDGET_TRIPPED)
    assert tripped["id"] > buy["id"]  # judged first, then the trip is reported
    assert [a.kind for a in h.notifier.alerts] == [AlertKind.BUY, AlertKind.BUDGET_TRIPPED]


def test_poll_breaker_trip_on_the_first_provider_evaluates_nothing(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"providers": ["mock", "serpapi"]}))
    h.fixtures(wf)
    run = h.poll(
        wf,
        providers=two_provider_registry(h),
        limits=BudgetLimits(max_per_run=0, max_per_day=100),
    )
    assert run.status == "budget_tripped"
    assert [a["kind"] for a in h.alerts()] == ["budget_tripped"]


def test_poll_a_target_that_keeps_failing_does_not_pin_the_round_robin(h: Harness):
    """ERROR rows count as polled, so the broken target goes to the back of the line."""
    wf = wf_of(wl_dict(GRID, poll={"targets_per_run": 1}))
    wl = wf.resolved()[0]
    broken = wl.targets[0].target_id
    h.fixtures(wf, skip=frozenset({broken}))
    seen: list[str] = []
    for day in range(3):
        h.clock.t = BASE + timedelta(days=day)
        run = h.poll(wf)
        assert run.status == "ok" and run.calls_made == 1
        [call] = h.q("SELECT target_id, status FROM api_calls WHERE run_id=?", run.run_id)
        seen.append(call["target_id"])
    assert seen[0] == broken  # never polled, earliest in the grid: goes first and fails
    assert sorted(seen) == sorted(t.target_id for t in wl.targets)  # then the other two, in turn
    assert None not in h.db.last_polled(seen).values()


def test_poll_provider_error_alerts_are_keyed_by_provider(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"providers": ["mock", "serpapi"]}))
    h.fixtures(wf, skip=frozenset({RT_TID}))
    h.poll(wf, providers=two_provider_registry(h))
    keys = sorted(a["alert_key"] for a in h.alerts(AlertKind.PROVIDER_ERROR))
    assert keys == [
        f"rt-one:provider_error:{RT_TID}:mock",
        f"rt-one:provider_error:{RT_TID}:serpapi",
    ]  # one outage per provider, not one swallowed by dedup


# -- times_per_day ----------------------------------------------------------------------------


def test_poll_times_per_day_caps_the_runs_that_may_poll_a_watchlist_per_utc_day(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"times_per_day": 1}))
    h.fixtures(wf)
    first = h.poll(wf)
    assert first.calls_made == 1

    h.clock.t = BASE + timedelta(hours=3)  # same UTC day
    second = h.poll(wf)
    assert second.status == "ok"
    assert second.calls_made == 0
    assert h.q("SELECT COUNT(*) AS n FROM api_calls")[0]["n"] == 1

    h.clock.t = BASE + timedelta(days=1)  # 2030-03-02 12:00 UTC
    third = h.poll(wf)
    assert third.calls_made == 1


def test_poll_times_per_day_allows_exactly_that_many_runs(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"times_per_day": 2}))
    h.fixtures(wf)
    calls = []
    for hours in (0, 1, 2):
        h.clock.t = BASE + timedelta(hours=hours)
        calls.append(h.poll(wf).calls_made)
    assert calls == [1, 1, 0]


def test_poll_times_per_day_is_per_watchlist_and_logs_the_skip(
    h: Harness, caplog: pytest.LogCaptureFixture
):
    wf = wf_of(wl_dict(RT, poll={"times_per_day": 1}), wl_dict(GRID, poll={"targets_per_run": 1}))
    h.fixtures(wf)
    h.poll(wf)
    h.clock.t = BASE + timedelta(hours=1)
    with caplog.at_level(logging.INFO):
        run = h.poll(wf)
    assert run.calls_made == 1  # grid-three (times_per_day=2) still polls, rt-one does not
    assert [
        r["watchlist_id"]
        for r in h.q("SELECT watchlist_id FROM api_calls WHERE run_id=?", run.run_id)
    ] == ["grid-three"]
    skips = [r for r in caplog.records if r.levelno == logging.INFO and "rt-one" in r.getMessage()]
    assert skips and "times_per_day" in skips[0].getMessage()


def test_poll_times_per_day_applies_to_a_purchased_watchlist_too(h: Harness):
    wf = wf_of(wl_dict(PAID, poll={"times_per_day": 1}))
    h.fixtures(wf)
    assert h.poll(wf).calls_made == 1
    h.clock.t = BASE + timedelta(hours=2)
    assert h.poll(wf).calls_made == 0


def test_poll_breaker_skips_do_not_count_against_times_per_day(h: Harness):
    wf = wf_of(wl_dict(RT, poll={"times_per_day": 1}))
    h.fixtures(wf)
    tripped = h.poll(wf, limits=BudgetLimits(max_per_run=0, max_per_day=100))
    assert tripped.status == "budget_tripped" and tripped.calls_made == 0
    h.clock.t = BASE + timedelta(hours=1)
    assert h.poll(wf).calls_made == 1  # the skipped run never polled anything


# -- purchased watchlists are polled first --------------------------------------------------


def test_poll_purchased_watchlist_goes_before_watching_ones_when_the_run_limit_bites(h: Harness):
    wf = wf_of(RT, PAID)  # RT is listed first, PAID second
    h.fixtures(wf)
    run = h.poll(wf, limits=BudgetLimits(max_per_run=1, max_per_day=100))
    assert run.status == "budget_tripped"
    ledger = h.q("SELECT status, watchlist_id FROM api_calls ORDER BY id")
    assert [(r["status"], r["watchlist_id"]) for r in ledger] == [
        ("ok", "paid-grid"),
        ("skipped_breaker", "rt-one"),
    ]
    assert Counter(a["kind"] for a in h.alerts()) == {
        "credit_drop": 1,
        "dot_24h": 1,
        "budget_tripped": 1,
    }


# -- [return not checked] end to end ----------------------------------------------------------


def write_outbound_only_fixture(fx: Path, t: SearchTarget, price: int = MID) -> None:
    full = fare(t, "1553", 1, price, FareBrand.MAIN_CABIN)
    outbound_only = full.model_copy(
        update={"itinerary": Itinerary(slices=full.itinerary.slices[:1])}
    )
    payload = {"observations": [dump(outbound_only)], "band": None, "raw": {"source": "test"}}
    (fx / (t.target_id.replace(":", "_") + ".json")).write_text(json.dumps(payload))


def test_poll_outbound_only_round_trip_fixture_buys_with_the_return_not_checked_prefix(
    h: Harness,
):
    wf = wf_of(RT)
    [target] = wf.resolved()[0].targets
    assert target.is_round_trip
    write_outbound_only_fixture(h.fx, target)
    h.poll(wf)
    [buy] = h.alerts(AlertKind.BUY)
    assert buy["message"].startswith("[return not checked] BUY ")
    [row] = h.q("SELECT risk_passed, risk_inbound_unverified FROM observations")
    assert (row["risk_passed"], row["risk_inbound_unverified"]) == (1, 1)


def test_poll_one_way_target_with_one_slice_buys_without_a_prefix(h: Harness):
    one_way = {k: v for k, v in RT.items() if k != "return"} | {"id": "one-way"}
    wf = wf_of(one_way)
    [target] = wf.resolved()[0].targets
    assert not target.is_round_trip
    outbound = fare(
        target.model_copy(update={"return_": date(2030, 4, 13)}),
        "1553",
        1,
        MID,
        FareBrand.MAIN_CABIN,
    )
    outbound = outbound.model_copy(
        update={
            "target_id": target.target_id,
            "watchlist_id": target.watchlist_id,
            "itinerary": Itinerary(slices=outbound.itinerary.slices[:1]),
        }
    )
    payload = {"observations": [dump(outbound)], "band": None, "raw": {"source": "test"}}
    (h.fx / (target.target_id.replace(":", "_") + ".json")).write_text(json.dumps(payload))
    h.poll(wf)
    [buy] = h.alerts(AlertKind.BUY)
    assert buy["message"].startswith("BUY ")
    assert "[" not in buy["message"].split("\n", 1)[0]
    [row] = h.q("SELECT risk_inbound_unverified FROM observations")
    assert row["risk_inbound_unverified"] == 0


# --------------------------------------------------------------------------- #
# post-purchase and fence history flow through the DB (alert_kinds_for)
# --------------------------------------------------------------------------- #

PAID_TARGET = SearchTarget(
    watchlist_id="paid-grid",
    origin="SEA",
    destination="SFO",
    depart=date(2030, 4, 24),
    return_=date(2030, 4, 28),
)
PAID = {
    **GRID,
    "id": "paid-grid",
    "status": "purchased",
    "post_purchase": {"dot_window_hours": 48},
    "purchase": {
        "purchased_at": BASE - timedelta(hours=1),
        "price_cents": 40000,
        "itinerary_key": itin(PAID_TARGET, "327", 0).itinerary_key,
        "target_id": PAID_TARGET.target_id,
        "carrier": "AS",
        "changeable": True,
    },
}


def test_poll_purchased_watchlist_polls_only_the_purchased_target_and_never_buys(h: Harness):
    wf = wf_of(PAID)
    h.fixtures(wf)
    run = h.poll(wf)
    assert run.status == "ok"
    assert run.calls_made == 1  # targets_per_run=2 is ignored after purchase
    assert [r["target_id"] for r in h.q("SELECT target_id FROM api_calls")] == [
        PAID_TARGET.target_id
    ]
    kinds = Counter(a["kind"] for a in h.alerts())
    assert kinds == {"credit_drop": 1, "dot_24h": 1}  # the $280 one-stop is a BUY nowhere
    [drop] = h.alerts(AlertKind.CREDIT_DROP)
    assert (drop["price_cents"], drop["reference_cents"]) == (TOP, 40000)
    assert drop["itinerary_key"] == PAID["purchase"]["itinerary_key"]
    assert len(h.notifier.alerts) == 2


def test_poll_dot_24h_is_gated_by_history_read_from_the_db_not_just_the_dedup_window(h: Harness):
    wf = wf_of(PAID)
    h.fixtures(wf)
    h.poll(wf)
    # 30h later: past the 24h dedup window for the same alert_key, still inside the 48h DOT window
    h.clock.t = BASE + timedelta(hours=30)
    h.poll(wf)
    assert len(h.alerts(AlertKind.DOT_24H)) == 1
    assert len(h.alerts(AlertKind.CREDIT_DROP)) == 2


def test_poll_fence_warning_fires_once_per_target_across_runs(h: Harness):
    wf = wf_of(FENCE_ONE)
    h.fixtures(wf)
    h.poll(wf)
    assert Counter(a["kind"] for a in h.alerts()) == {"fence_warning": 1, "buy": 1}

    h.clock.t = BASE + timedelta(hours=25)  # DTD 19, still FENCE, outside the dedup window
    h.poll(wf)
    assert len(h.alerts(AlertKind.FENCE_WARNING)) == 1
    assert len(h.alerts(AlertKind.BUY)) == 2


# --------------------------------------------------------------------------- #
# Database.alert_kinds_for (new in phase 5)
# --------------------------------------------------------------------------- #


def test_alert_kinds_for_is_empty_for_a_target_with_no_alerts(db: Database):
    assert db.alert_kinds_for(RT_TID) == set()


def test_alert_kinds_for_returns_the_distinct_kinds_of_that_target_only(db: Database):
    mine = {"watchlist_id": "rt-one", "target_id": RT_TID}
    db.insert_alert(make_alert("a1", kind=AlertKind.BUY, **mine))
    db.insert_alert(make_alert("a2", kind=AlertKind.BUY, **mine))
    db.insert_alert(make_alert("a3", kind=AlertKind.FENCE_WARNING, **mine))
    db.insert_alert(make_alert("a4", kind=AlertKind.DOT_24H, target_id="other:SEASFO:x:y"))
    db.insert_alert(
        make_alert("a5", kind=AlertKind.BUDGET_TRIPPED, watchlist_id="rt-one", target_id=None)
    )
    got = db.alert_kinds_for(RT_TID)
    assert isinstance(got, set)
    assert got == {AlertKind.BUY, AlertKind.FENCE_WARNING}


def test_alert_kinds_for_sees_alerts_of_any_age(db: Database):
    db.insert_alert(
        make_alert("old", at=BASE - timedelta(days=400), kind=AlertKind.FENCE_WARNING,
                   watchlist_id="rt-one", target_id=RT_TID)
    )  # fmt: skip
    assert db.alert_kinds_for(RT_TID) == {AlertKind.FENCE_WARNING}


# --------------------------------------------------------------------------- #
# select_targets (ARCHITECTURE section 7)
# --------------------------------------------------------------------------- #

DATES = {
    "id": "dates-grid",
    "origin": "SEA",
    "destination": "SJC",
    "depart": {"start": date(2030, 4, 24), "end": date(2030, 4, 26)},
    "return": date(2030, 5, 2),
    "ceiling_cents": 29000,
    "poll": {"providers": ["mock"], "targets_per_run": 3},
}
D24, D25, D26 = (f"dates-grid:SEASJC:2030-04-{d}:2030-05-02" for d in (24, 25, 26))


def one_wl(raw: dict) -> ResolvedWatchlist:
    return wf_of(raw).resolved()[0]


def ids(targets: list[SearchTarget]) -> list[str]:
    return [t.target_id for t in targets]


def test_select_targets_never_polled_first_then_dtd_ascending_then_the_polled_ones():
    wl = one_wl(DATES)
    last = {D26: None, D25: None, D24: BASE - timedelta(days=1)}
    assert ids(select_targets(wl, last, now=BASE)) == [D25, D26, D24]


def test_select_targets_oldest_poll_first_regardless_of_departure():
    wl = one_wl(DATES)
    last = {
        D24: BASE - timedelta(hours=1),
        D25: BASE - timedelta(days=3),
        D26: BASE - timedelta(days=2),
    }
    assert ids(select_targets(wl, last, now=BASE)) == [D25, D26, D24]


def test_select_targets_breaks_ties_by_days_to_departure_ascending():
    wl = one_wl(DATES)
    same = BASE - timedelta(days=1)
    for last in (
        {D24: same, D25: same, D26: same},
        {D26: same, D25: same, D24: same},  # dict order must not matter
        {D26: None, D25: None, D24: None},
    ):
        assert ids(select_targets(wl, last, now=BASE)) == [D24, D25, D26]


def test_select_targets_takes_targets_per_run_from_the_head_of_that_order():
    wl = one_wl(wl_dict(DATES, poll={"targets_per_run": 2}))
    last = {D26: None, D25: None, D24: BASE - timedelta(days=1)}
    assert ids(select_targets(wl, last, now=BASE)) == [D25, D26]
    one = one_wl(wl_dict(DATES, poll={"targets_per_run": 1}))
    assert ids(select_targets(one, last, now=BASE)) == [D25]


def test_select_targets_returns_everything_when_per_run_exceeds_the_grid():
    wl = one_wl(wl_dict(DATES, poll={"targets_per_run": 20}))
    assert len(select_targets(wl, dict.fromkeys([D24, D25, D26]), now=BASE)) == 3


def test_select_targets_purchased_is_pinned_to_the_purchased_target():
    purchase = {
        "purchased_at": BASE - timedelta(days=5),
        "price_cents": 30000,
        "itinerary_key": "0123456789abcdef",
        "target_id": D25,
        "carrier": "AS",
    }
    wl = one_wl(wl_dict(DATES, status="purchased", purchase=purchase))
    last = {D24: None, D25: BASE - timedelta(minutes=1), D26: None}
    assert ids(select_targets(wl, last, now=BASE)) == [D25]


@pytest.mark.parametrize("status", ["paused", "expired"])
def test_select_targets_selects_nothing_for_a_watchlist_that_is_not_watching(status: str):
    wl = one_wl(wl_dict(DATES, status=status))
    assert select_targets(wl, dict.fromkeys([D24, D25, D26]), now=BASE) == []


def test_select_targets_judges_expiry_by_the_given_now_not_the_wall_clock():
    wl = one_wl(wl_dict(DATES, depart={"start": date(2030, 2, 27), "end": date(2030, 3, 1)}))
    # now = 2030-03-01: 02-27 and 02-28 have left, 03-01 departs today (DTD 0)
    last = dict.fromkeys(t.target_id for t in wl.targets)
    assert ids(select_targets(wl, last, now=BASE)) == ["dates-grid:SEASJC:2030-03-01:2030-05-02"]


def test_select_targets_ignores_scan_provider_but_says_so(caplog: pytest.LogCaptureFixture):
    wl = one_wl(wl_dict(DATES, poll={"scan_provider": "amadeus"}))
    last = {D26: None, D25: None, D24: BASE - timedelta(days=1)}
    with caplog.at_level(logging.DEBUG):
        got = select_targets(wl, last, now=BASE)
    assert ids(got) == [D25, D26, D24]
    assert "scan tier not implemented" in caplog.text


# --------------------------------------------------------------------------- #
# Phase 6: undelivered retry at run start, click URL plumbing (section 7a)
# --------------------------------------------------------------------------- #

PAUSED = wl_dict(RT, id="rt-paused", status="paused")


def old_alert(h: Harness, key: str, age: timedelta, **kw) -> int:
    alert_id = h.db.insert_alert(make_alert(key, at=BASE - age, **kw))
    assert alert_id is not None
    return alert_id


def test_run_start_retries_a_two_hour_old_undelivered_alert_and_marks_it_delivered(h: Harness):
    alert_id = old_alert(h, "young", timedelta(hours=2))
    h.poll(wf_of(PAUSED), notifier=RecordingNotifier("ntfy"))
    [row] = h.alerts()
    assert row["id"] == alert_id
    assert row["channel"] == "ntfy" and row["delivered_at"] is not None


def test_run_start_leaves_an_alert_older_than_24h_alone_and_logs_the_count(
    h: Harness, caplog: pytest.LogCaptureFixture
):
    old_alert(h, "old", timedelta(hours=30))
    old_alert(h, "older", timedelta(hours=50))
    n = RecordingNotifier("ntfy")
    with caplog.at_level(logging.INFO, logger="src.runner.poll"):
        h.poll(wf_of(PAUSED), notifier=n)
    assert n.alerts == []
    assert all(r["delivered_at"] is None for r in h.alerts())
    assert [r.getMessage() for r in caplog.records if "left alone" in r.getMessage()] == [
        "2 undelivered alert(s) older than 24h left alone"
    ]


def test_run_start_does_not_log_when_nothing_is_stale(h: Harness, caplog: pytest.LogCaptureFixture):
    old_alert(h, "young", timedelta(hours=2))
    with caplog.at_level(logging.INFO, logger="src.runner.poll"):
        h.poll(wf_of(PAUSED), notifier=RecordingNotifier("ntfy"))
    assert not [r for r in caplog.records if "left alone" in r.getMessage()]


def test_run_start_retry_that_fails_again_stays_undelivered_and_is_not_retried_in_the_run(
    h: Harness,
):
    old_alert(h, "young", timedelta(hours=2))
    n = RecordingNotifier(None)
    h.poll(wf_of(PAUSED), notifier=n)
    assert len(n.alerts) == 1
    assert h.alerts()[0]["delivered_at"] is None


def test_run_start_retries_oldest_first_before_this_runs_own_alerts(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf)
    old_alert(h, "a", timedelta(hours=5), message="first\nx")
    old_alert(h, "b", timedelta(hours=3), message="second\nx")
    h.poll(wf)
    sent = [a.message.split("\n")[0] for a in h.notifier.alerts]
    assert sent[:2] == ["first", "second"]
    assert sent[2].startswith("BUY")
    assert len(sent) == 3
    assert h.db.undelivered_alerts(max_age_hours=48) == []


def test_run_start_retry_runs_even_when_the_poll_itself_has_nothing_to_do(h: Harness):
    old_alert(h, "young", timedelta(hours=1))
    n = RecordingNotifier("ntfy")
    run = h.poll(wf_of(PAUSED), notifier=n)
    assert run.status == "ok" and run.calls_made == 0 and len(n.alerts) == 1


def test_a_retried_alert_carries_its_observations_booking_url(h: Harness):
    h.db.start_run(make_run("prior", started_at=BASE - timedelta(hours=3)))
    [oid] = h.db.insert_observations(
        [
            make_obs(
                "prior", observed_at=BASE - timedelta(hours=3), booking_url="https://b.example/1"
            )
        ]
    )
    old_alert(h, "with-url", timedelta(hours=2), observation_id=oid)
    old_alert(h, "no-obs", timedelta(hours=1))
    n = RecordingNotifier("ntfy")
    h.poll(wf_of(PAUSED), notifier=n)
    assert n.clicks == ["https://b.example/1", None]


def test_the_winning_observations_booking_url_is_passed_as_click(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf, urls=True)
    h.poll(wf)
    [buy] = h.alerts(AlertKind.BUY)
    assert h.db.observation(buy["observation_id"]).price_cents == MID  # type: ignore[union-attr]
    assert h.notifier.clicks == ["https://book.example/1553"]


def test_click_is_none_when_the_winning_observation_has_no_booking_url(h: Harness):
    wf = wf_of(RT)
    h.fixtures(wf)
    h.poll(wf)
    assert len(h.notifier.alerts) == 1 and h.notifier.clicks == [None]


def test_click_is_none_for_alerts_without_an_observation(h: Harness):
    wf = wf_of(GRID)
    h.fixtures(wf, urls=True)
    h.poll(wf, limits=BudgetLimits(max_per_run=1, max_per_day=100))
    [tripped] = h.alerts(AlertKind.BUDGET_TRIPPED)
    assert tripped["observation_id"] is None
    assert h.notifier.clicks == ["https://book.example/1553", None]  # BUY, then BUDGET_TRIPPED


def test_a_raising_notifier_during_retry_does_not_stop_the_run(tmp_path, caplog):
    """A broken notifier must never prevent polling (ARCHITECTURE §7a retry rule)."""
    import logging
    from datetime import UTC, datetime, timedelta

    from src.db import Database
    from src.models import Alert, AlertKind, Regime
    from src.runner.poll import _retry_undelivered

    class Boom:
        def send(self, alert, *, click=None):
            raise RuntimeError("ntfy exploded")

    db_path = tmp_path / "r.db"
    with Database(db_path) as db:
        db.migrate()
        now = datetime.now(UTC)
        aid = db.insert_alert(
            Alert(
                created_at=now - timedelta(hours=1),
                watchlist_id="w-x",
                kind=AlertKind.BUY,
                regime=Regime.PRIME,
                message="BUY\nbody",
                alert_key="w-x:buy:-:0",
            )
        )
        assert aid is not None
        with caplog.at_level(logging.ERROR):
            _retry_undelivered(db, Boom())
        assert "leaving it undelivered" in caplog.text
        assert db.undelivered_alerts(max_age_hours=24)[0].id == aid
