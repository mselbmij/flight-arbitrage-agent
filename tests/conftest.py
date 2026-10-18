"""Shared factories. Every datetime is tz-aware; money is integer cents."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from src.db import Database

from src.models import (
    Alert,
    AlertKind,
    ApiCall,
    ApiCallStatus,
    BenchmarkBand,
    FareBrand,
    FareObservation,
    Itinerary,
    Provider,
    Regime,
    ResolvedWatchlist,
    RiskVerdict,
    Run,
    Segment,
    Slice,
    WatchlistFile,
)

REPO = Path(__file__).resolve().parents[1]
PT = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
WL = "sea-sjc-thanksgiving"
TID = "sea-sjc-thanksgiving:SEASJC:2026-11-25:2026-11-29"
OTHER_TID = "sea-ord-jan:SEAORD:2027-01-14:2027-01-18"


class Clock:
    """Mutable 'now' for Database(now=...)."""

    def __init__(self, t: datetime = T0) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def load_watchlists(path: Path | None = None) -> WatchlistFile:
    p = path or REPO / "config" / "watchlists.yaml"
    return WatchlistFile.model_validate(yaml.safe_load(p.read_text()))


def thanksgiving() -> ResolvedWatchlist:
    return next(r for r in load_watchlists().resolved() if r.id == WL)


def make_itinerary(day: int = 25, flight: str = "327", carrier: str = "AS", stops: int = 0):
    """Outbound SEA->SJC on 2026-11-<day>; stops=1 routes via SLC with a 75 minute layover."""
    d = lambda h, m, dd=day: datetime(2026, 11, dd, h, m, tzinfo=PT)  # noqa: E731
    if stops == 0:
        segs = [Segment(carrier=carrier, flight_number=flight, origin="SEA", destination="SJC",
                        departure_local=d(7, 0), arrival_local=d(9, 25))]  # fmt: skip
    else:
        segs = [
            Segment(carrier=carrier, flight_number=flight, origin="SEA", destination="SLC",
                    departure_local=d(6, 30), arrival_local=d(9, 15)),
            Segment(carrier=carrier, flight_number="1807", origin="SLC", destination="SJC",
                    departure_local=d(10, 30), arrival_local=d(11, 50)),
        ]  # fmt: skip
    back = Segment(carrier=carrier, flight_number="328", origin="SJC", destination="SEA",
                   departure_local=d(18, 0, 29), arrival_local=d(20, 30, 29))  # fmt: skip
    return Itinerary(slices=[Slice(segments=segs), Slice(segments=[back])])


def make_run(run_id: str = "run-1", **kw) -> Run:
    return Run(**{"run_id": run_id, "started_at": T0, "trigger": "manual", **kw})


def make_call(run_id: str = "run-1", at: datetime = T0, **kw) -> ApiCall:
    base = dict(run_id=run_id, called_at=at, provider=Provider.MOCK, endpoint="search",
                watchlist_id=WL, target_id=TID, status=ApiCallStatus.OK)  # fmt: skip
    return ApiCall(**{**base, **kw})


def make_obs(run_id: str = "run-1", price: int = 30000, passed: bool = True, **kw):
    base = dict(watchlist_id=WL, target_id=TID, observed_at=T0, provider=Provider.MOCK,
                run_id=run_id, itinerary=make_itinerary(), price_cents=price, currency="USD",
                fare_brand=FareBrand.MAIN_CABIN, days_to_departure=39,
                risk=RiskVerdict(passed=passed, config_hash="h1",
                                 rejections=[] if passed else ["basic_economy"]))  # fmt: skip
    return FareObservation(**{**base, **kw})


def make_band(run_id: str = "run-1", **kw) -> BenchmarkBand:
    base = dict(watchlist_id=WL, target_id=TID, observed_at=T0, provider=Provider.MOCK,
                run_id=run_id, currency="USD")  # fmt: skip
    return BenchmarkBand(**{**base, **kw})


def make_alert(key: str = "k1", at: datetime = T0, **kw) -> Alert:
    base = dict(created_at=at, watchlist_id=WL, target_id=TID, kind=AlertKind.BUY,
                regime=Regime.PRIME, message="buy", alert_key=key)  # fmt: skip
    return Alert(**{**base, **kw})


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "fares.db"


@pytest.fixture
def db(db_path: Path, clock: Clock):
    with Database(db_path, now=clock) as d:
        d.migrate()
        yield d
