"""Phase 2: src/db.py. ARCHITECTURE.md sections 2, 3 (lowest_seen, alert_key), 6, 7."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time
from src.db import BudgetLimits, BudgetTripped, Database

from src.models import (
    ApiCallStatus,
    FareBrand,
    Itinerary,
    PriceLevel,
    Provider,
    Regime,
    RiskVerdict,
    Segment,
    Slice,
)
from tests.conftest import (
    OTHER_TID,
    PT,
    T0,
    TID,
    WL,
    Clock,
    load_watchlists,
    make_alert,
    make_band,
    make_call,
    make_itinerary,
    make_obs,
    make_run,
)

HOUR = timedelta(hours=1)


def sql(path: Path, query: str, *args):
    con = sqlite3.connect(path)
    try:
        return con.execute(query, args).fetchall()
    finally:
        con.close()


def limits(run: int = 10, day: int = 100) -> BudgetLimits:
    return BudgetLimits(max_per_run=run, max_per_day=day)


# --------------------------------------------------------------------------- #
# Migrations and connection
# --------------------------------------------------------------------------- #


def test_migrate_applies_initial_then_is_idempotent(db_path: Path):
    with Database(db_path) as d:
        first = d.migrate()
        second = d.migrate()
    assert len(first) == 1
    assert second == []
    assert len(sql(db_path, "SELECT version FROM schema_migrations")) == 1


def test_migrate_creates_all_tables(db: Database, db_path: Path):
    names = {r[0] for r in sql(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "api_calls", "observations", "benchmark_bands", "alerts"} <= names
    assert "schema_migrations" in names


@pytest.mark.parametrize(
    ("table", "cols"),
    [
        ("observations", ["target_id", "observed_at"]),
        ("observations", ["itinerary_key", "observed_at"]),
        ("api_calls", ["target_id", "called_at"]),
        ("alerts", ["alert_key", "created_at"]),
    ],
)
def test_migration_creates_architecture_indexes(db: Database, db_path: Path, table, cols):
    found = []
    for row in sql(db_path, f"PRAGMA index_list({table})"):
        info = sql(db_path, f"PRAGMA index_info({row[1]})")
        found.append([i[2] for i in info])
    assert cols in found


def test_connection_uses_wal_mode(db: Database, db_path: Path):
    assert sql(db_path, "PRAGMA journal_mode")[0][0].lower() == "wal"


def test_foreign_keys_are_enforced(db: Database):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_observations([make_obs(run_id="no-such-run")])


def test_connection_closes_on_exit(db_path: Path):
    with Database(db_path) as d:
        d.migrate()
    with pytest.raises(sqlite3.ProgrammingError):
        d.counts()


# --------------------------------------------------------------------------- #
# Round trips: every model, every optional field populated
# --------------------------------------------------------------------------- #


def test_roundtrip_run(db: Database):
    run = make_run("r-full", finished_at=T0 + HOUR, calls_made=0, status="error", error="boom")
    db.start_run(run)
    assert db.get_run("r-full") == run


def test_get_run_unknown_returns_none(db: Database):
    assert db.get_run("nope") is None


def test_finish_run_sets_status_error_and_finished_at(db: Database, clock: Clock):
    db.start_run(make_run())
    clock.t = T0 + HOUR
    db.finish_run("run-1", "budget_tripped", "gate=run")
    got = db.get_run("run-1")
    assert got is not None
    assert (got.status, got.error) == ("budget_tripped", "gate=run")
    assert got.finished_at == T0 + HOUR


def test_roundtrip_api_call_via_reserve(db: Database, db_path: Path):
    db.start_run(make_run())
    call = make_call(latency_ms=None, cost_units=1)
    call_id = db.reserve_api_call(call, limits())
    row = sql(
        db_path,
        "SELECT run_id, provider, endpoint, watchlist_id, target_id, status, cost_units "
        "FROM api_calls WHERE id=?",
        call_id,
    )[0]
    assert row == ("run-1", "mock", "search", WL, TID, "ok", 1)


def test_roundtrip_observation_with_every_optional_field(db: Database):
    target = load_watchlists().resolved()[0].targets[0]
    dep = lambda h, m, d: datetime(2026, 11, d, h, m, tzinfo=PT)  # noqa: E731
    itin = Itinerary(
        slices=[
            Slice(
                segments=[
                    Segment(
                        carrier="AS",
                        operating_carrier="QX",
                        flight_number="2103A",
                        origin="SEA",
                        destination="PDX",
                        departure_local=dep(6, 0, 25),
                        arrival_local=dep(7, 0, 25),
                        aircraft="E75",
                    ),
                    Segment(
                        carrier="AS",
                        flight_number="1",
                        origin="PDX",
                        destination="SJC",
                        departure_local=dep(8, 15, 25),
                        arrival_local=dep(10, 0, 25),
                        aircraft="738",
                    ),
                ]  # fmt: skip
            ),
            Slice(
                segments=[
                    Segment(
                        carrier="AS",
                        flight_number="328",
                        origin="SJC",
                        destination="SEA",
                        departure_local=dep(18, 0, 29),
                        arrival_local=dep(20, 30, 29),
                    ),
                ]  # fmt: skip
            ),
        ]
    )
    obs = make_obs(
        watchlist_id=target.watchlist_id,
        target_id=target.target_id,
        itinerary=itin,
        fare_brand=FareBrand.MAIN_PLUS,
        fare_brand_raw="Comfort+",
        changeable=True,
        refundable=False,
        carry_on_included=True,
        seats_remaining=4,
        booking_url="https://example.test/book?x=1",
        raw_payload_ref="data/raw/run-1/mock-sea-sjc-thanksgiving.json",
        risk=RiskVerdict(passed=False, rejections=["stops>0", "layover>60"],
                         brand_unverified=True, config_hash="abc123def456"),
    )  # fmt: skip
    db.start_run(make_run())
    (oid,) = db.insert_observations([obs])
    got = db.observation(oid)
    assert got is not None and got.id == oid
    assert got.model_dump(exclude={"id"}) == obs.model_dump(exclude={"id"})
    assert got.itinerary_key == obs.itinerary_key


def test_roundtrip_observation_enums_and_null_optionals(db: Database):
    obs = make_obs(fare_brand=FareBrand.UNKNOWN)
    db.start_run(make_run())
    (oid,) = db.insert_observations([obs])
    got = db.observation(oid)
    assert got is not None
    assert got.model_dump(exclude={"id"}) == obs.model_dump(exclude={"id"})
    assert got.changeable is None and got.seats_remaining is None


def test_roundtrip_observation_without_risk_verdict(db: Database):
    obs = make_obs(risk=None)
    db.start_run(make_run())
    (oid,) = db.insert_observations([obs])
    got = db.observation(oid)
    assert got is not None and got.risk is None


def test_observation_unknown_id_returns_none(db: Database):
    assert db.observation(999) is None


def test_roundtrip_benchmark_band_all_fields(db: Database):
    band = make_band(lowest_price_cents=24900, typical_low_cents=27000,
                     typical_high_cents=36000, price_level=PriceLevel.LOW)  # fmt: skip
    db.start_run(make_run())
    bid = db.insert_band(band)
    got = db.latest_band(TID)
    assert got is not None and got.id == bid
    assert got.model_dump(exclude={"id"}) == band.model_dump(exclude={"id"})


def test_roundtrip_band_with_null_prices(db: Database):
    band = make_band()
    db.start_run(make_run())
    db.insert_band(band)
    got = db.latest_band(TID)
    assert got is not None
    assert got.lowest_price_cents is None and got.usable is False


def test_roundtrip_alert_with_every_optional_field(db: Database):
    db.start_run(make_run())
    (oid,) = db.insert_observations([make_obs()])
    alert = make_alert(
        observation_id=oid,
        itinerary_key=make_itinerary().itinerary_key,
        price_cents=28700,
        reference_cents=29000,
        message="SEA to SJC $287",
        regime=Regime.FENCE,
    )
    aid = db.insert_alert(alert)
    assert aid is not None
    (got,) = db.undelivered_alerts(max_age_hours=24)
    assert got.id == aid
    assert got.model_dump(exclude={"id"}) == alert.model_dump(exclude={"id"})


def test_mark_delivered_writes_channel_and_timestamp(db: Database, db_path: Path, clock: Clock):
    aid = db.insert_alert(make_alert())
    clock.t = T0 + HOUR
    db.mark_delivered(aid, "ntfy")
    row = sql(db_path, "SELECT channel, delivered_at FROM alerts WHERE id=?", aid)[0]
    assert row[0] == "ntfy"
    assert datetime.fromisoformat(row[1]) == T0 + HOUR
    assert db.undelivered_alerts(max_age_hours=24) == []


def test_counts_reports_each_table(db: Database):
    assert db.counts() == {"runs": 0, "api_calls": 0, "observations": 0,
                           "benchmark_bands": 0, "alerts": 0}  # fmt: skip
    db.start_run(make_run())
    db.reserve_api_call(make_call(), limits())
    db.insert_observations([make_obs(), make_obs(price=31000)])
    db.insert_band(make_band())
    db.insert_alert(make_alert())
    assert db.counts() == {"runs": 1, "api_calls": 1, "observations": 2,
                           "benchmark_bands": 1, "alerts": 1}  # fmt: skip


# --------------------------------------------------------------------------- #
# Observations, bands, lowest_seen, last_polled
# --------------------------------------------------------------------------- #


def test_insert_observations_returns_ids_in_input_order(db: Database):
    db.start_run(make_run())
    ids = db.insert_observations([make_obs(price=30000), make_obs(price=31000)])
    assert len(ids) == 2 and ids[0] != ids[1]
    assert [db.observation(i).price_cents for i in ids] == [30000, 31000]


def test_insert_observations_is_all_or_nothing(db: Database):
    db.start_run(make_run())
    batch = [make_obs(price=30000), make_obs(run_id="no-such-run")]
    with pytest.raises(sqlite3.Error):
        db.insert_observations(batch)
    assert db.counts()["observations"] == 0


def test_observations_for_filters_by_target_and_orders_by_time(db: Database):
    db.start_run(make_run())
    db.insert_observations([
        make_obs(price=2, observed_at=T0 + 2 * HOUR),
        make_obs(price=1, observed_at=T0),
        make_obs(price=9, target_id=OTHER_TID),
    ])  # fmt: skip
    got = db.observations_for(TID)
    assert [o.price_cents for o in got] == [1, 2]


def test_observations_for_since_is_inclusive(db: Database):
    db.start_run(make_run())
    db.insert_observations([make_obs(price=1, observed_at=T0),
                            make_obs(price=2, observed_at=T0 + HOUR)])  # fmt: skip
    got = db.observations_for(TID, since=T0 + HOUR)
    assert [o.price_cents for o in got] == [2]


def test_observations_for_passed_only_drops_rejected(db: Database):
    db.start_run(make_run())
    db.insert_observations([make_obs(price=1), make_obs(price=2, passed=False)])
    assert [o.price_cents for o in db.observations_for(TID, passed_only=True)] == [1]


def test_lowest_seen_ignores_risk_rejected(db: Database):
    db.start_run(make_run())
    db.insert_observations([
        make_obs(price=30000),
        make_obs(price=20000, passed=False, fare_brand=FareBrand.BASIC_ECONOMY),
        make_obs(price=25000),
    ])  # fmt: skip
    assert db.lowest_seen(TID) == 25000


def test_lowest_seen_is_none_when_only_rejected_exist(db: Database):
    db.start_run(make_run())
    db.insert_observations([make_obs(price=20000, passed=False)])
    assert db.lowest_seen(TID) is None


def test_lowest_seen_is_none_without_observations(db: Database):
    assert db.lowest_seen(TID) is None


def test_lowest_seen_is_scoped_to_target(db: Database):
    db.start_run(make_run())
    db.insert_observations([make_obs(price=30000), make_obs(price=10000, target_id=OTHER_TID)])
    assert db.lowest_seen(TID) == 30000


def test_lowest_seen_is_provider_independent(db: Database):
    db.start_run(make_run())
    db.insert_observations([make_obs(price=30000, provider=Provider.SERPAPI),
                            make_obs(price=28000, provider=Provider.AMADEUS)])  # fmt: skip
    assert db.lowest_seen(TID) == 28000


def test_lowest_seen_excludes_observation_without_verdict(db: Database):
    db.start_run(make_run())
    db.insert_observations([make_obs(price=10000, risk=None), make_obs(price=30000)])
    assert db.lowest_seen(TID) == 30000


def test_latest_band_returns_most_recent_by_observed_at(db: Database):
    db.start_run(make_run())
    db.insert_band(make_band(observed_at=T0 + HOUR, typical_low_cents=2))
    db.insert_band(make_band(observed_at=T0, typical_low_cents=1))
    got = db.latest_band(TID)
    assert got is not None and got.typical_low_cents == 2


def test_latest_band_none_when_absent(db: Database):
    assert db.latest_band(TID) is None


def test_last_polled_returns_none_for_unpolled_targets(db: Database):
    db.start_run(make_run())
    db.reserve_api_call(make_call(), limits())
    got = db.last_polled([TID, OTHER_TID])
    assert got == {TID: T0, OTHER_TID: None}


def test_last_polled_is_max_called_at(db: Database):
    db.start_run(make_run())
    db.reserve_api_call(make_call(at=T0 + HOUR), limits())
    db.reserve_api_call(make_call(at=T0), limits())
    assert db.last_polled([TID])[TID] == T0 + HOUR


def test_last_polled_ignores_error_and_skipped_rows(db: Database):
    db.start_run(make_run())
    db.reserve_api_call(make_call(at=T0), limits())
    db.reserve_api_call(make_call(at=T0 + 2 * HOUR, status=ApiCallStatus.ERROR), limits())
    with pytest.raises(BudgetTripped):
        db.reserve_api_call(make_call(at=T0 + 3 * HOUR), limits(run=2))
    assert db.last_polled([TID])[TID] == T0


# --------------------------------------------------------------------------- #
# Circuit breaker (section 6)
# --------------------------------------------------------------------------- #


def test_reserve_returns_id_and_increments_calls_made(db: Database):
    db.start_run(make_run())
    first = db.reserve_api_call(make_call(), limits())
    second = db.reserve_api_call(make_call(), limits())
    assert first != second
    run = db.get_run("run-1")
    assert run is not None and run.calls_made == 2


def test_run_gate_trips_on_third_call_and_logs_skipped_row(db: Database, db_path: Path):
    db.start_run(make_run())
    lim = limits(run=2)
    db.reserve_api_call(make_call(), lim)
    db.reserve_api_call(make_call(), lim)
    with pytest.raises(BudgetTripped) as exc:
        db.reserve_api_call(make_call(), lim)
    assert exc.value.gate == "run"
    assert exc.value.limit == 2
    statuses = [r[0] for r in sql(db_path, "SELECT status FROM api_calls ORDER BY id")]
    assert statuses == ["ok", "ok", "skipped_breaker"]
    run = db.get_run("run-1")
    assert run is not None and run.calls_made == 2


def test_run_gate_allows_exactly_max_per_run(db: Database):
    db.start_run(make_run())
    for _ in range(3):
        db.reserve_api_call(make_call(), limits(run=3))
    assert db.counts()["api_calls"] == 3


def test_run_gate_is_per_run(db: Database):
    db.start_run(make_run("a"))
    db.start_run(make_run("b"))
    db.reserve_api_call(make_call("a"), limits(run=1))
    db.reserve_api_call(make_call("b"), limits(run=1))
    with pytest.raises(BudgetTripped):
        db.reserve_api_call(make_call("a"), limits(run=1))


def test_day_gate_trips_across_runs(db: Database):
    db.start_run(make_run("a"))
    db.start_run(make_run("b"))
    lim = limits(run=10, day=3)
    for _ in range(3):
        db.reserve_api_call(make_call("a"), lim)
    with pytest.raises(BudgetTripped) as exc:
        db.reserve_api_call(make_call("b"), lim)
    assert exc.value.gate == "day"
    assert exc.value.used == 3 and exc.value.limit == 3
    run_b = db.get_run("b")
    assert run_b is not None and run_b.calls_made == 0


def test_day_gate_writes_skipped_row(db: Database, db_path: Path):
    db.start_run(make_run())
    db.reserve_api_call(make_call(), limits(day=1))
    with pytest.raises(BudgetTripped):
        db.reserve_api_call(make_call(), limits(day=1))
    assert [r[0] for r in sql(db_path, "SELECT status FROM api_calls ORDER BY id")] == [
        "ok",
        "skipped_breaker",
    ]


def test_day_gate_sums_cost_units_not_rows(db: Database):
    db.start_run(make_run())
    lim = limits(day=3)
    db.reserve_api_call(make_call(cost_units=2), lim)
    with pytest.raises(BudgetTripped) as exc:
        db.reserve_api_call(make_call(cost_units=2), lim)  # 2 + 2 > 3
    assert exc.value.gate == "day"
    db.reserve_api_call(make_call(cost_units=1), lim)  # 2 + 1 == 3 is allowed
    assert db.calls_today() == 3


def test_calls_today_ignores_skipped_breaker_rows(db: Database):
    db.start_run(make_run())
    lim = limits(run=2)
    db.reserve_api_call(make_call(), lim)
    db.reserve_api_call(make_call(), lim)
    with pytest.raises(BudgetTripped):
        db.reserve_api_call(make_call(), lim)
    assert db.calls_today() == 2


def test_calls_today_counts_errored_calls(db: Database):
    db.start_run(make_run())
    cid = db.reserve_api_call(make_call(), limits())
    db.complete_api_call(cid, ApiCallStatus.ERROR, 120, "timeout")
    assert db.calls_today() == 1


def test_complete_api_call_updates_status_latency_and_error(db: Database, db_path: Path):
    db.start_run(make_run())
    cid = db.reserve_api_call(make_call(), limits())
    db.complete_api_call(cid, ApiCallStatus.ERROR, 842, "HTTP 503")
    row = sql(db_path, "SELECT status, latency_ms, error FROM api_calls WHERE id=?", cid)[0]
    assert row == ("error", 842, "HTTP 503")


def test_complete_api_call_ok_with_null_latency(db: Database, db_path: Path):
    db.start_run(make_run())
    cid = db.reserve_api_call(make_call(), limits())
    db.complete_api_call(cid, ApiCallStatus.OK, None)
    row = sql(db_path, "SELECT status, latency_ms, error FROM api_calls WHERE id=?", cid)[0]
    assert row == ("ok", None, None)


def test_calls_today_rolls_over_at_midnight_utc(db_path: Path):
    clock = Clock(datetime(2026, 10, 17, 23, 59, 30, tzinfo=UTC))
    with Database(db_path, now=clock) as d:
        d.migrate()
        d.start_run(make_run())
        d.reserve_api_call(make_call(at=clock.t), limits())
        assert d.calls_today() == 1
        clock.t = datetime(2026, 10, 18, 0, 0, 30, tzinfo=UTC)
        assert d.calls_today() == 0


def test_call_at_exactly_midnight_utc_counts_today(db_path: Path):
    midnight = datetime(2026, 10, 18, 0, 0, 0, tzinfo=UTC)
    clock = Clock(midnight + timedelta(minutes=5))
    with Database(db_path, now=clock) as d:
        d.migrate()
        d.start_run(make_run())
        d.reserve_api_call(make_call(at=midnight - timedelta(seconds=1)), limits())
        d.reserve_api_call(make_call(at=midnight), limits())
        assert d.calls_today() == 1


def test_yesterdays_calls_do_not_consume_todays_day_gate(db_path: Path):
    clock = Clock(datetime(2026, 10, 18, 0, 0, 30, tzinfo=UTC))
    with Database(db_path, now=clock) as d:
        d.migrate()
        d.start_run(make_run())
        yesterday = datetime(2026, 10, 17, 23, 59, 30, tzinfo=UTC)
        for _ in range(4):
            d.reserve_api_call(make_call(at=yesterday), limits(day=100))
        d.reserve_api_call(make_call(at=clock.t), limits(day=4))  # must not raise
        assert d.calls_today() == 1


@freeze_time("2026-10-17 23:59:30")
def test_default_clock_is_utc_now_and_rolls_over(db_path: Path):
    with Database(db_path) as d:
        d.migrate()
        d.start_run(make_run())
        d.reserve_api_call(make_call(at=datetime(2026, 10, 17, 23, 59, 30, tzinfo=UTC)), limits())
        assert d.calls_today() == 1
        with freeze_time("2026-10-18 00:00:30"):
            assert d.calls_today() == 0


def test_budget_tripped_carries_gate_used_and_limit():
    exc = BudgetTripped("day", 4, 4)
    assert (exc.gate, exc.used, exc.limit) == ("day", 4, 4)
    assert isinstance(exc, Exception)


def test_budget_limits_is_frozen():
    lim = BudgetLimits(max_per_run=3, max_per_day=4)
    with pytest.raises((TypeError, ValueError)):
        lim.max_per_run = 99


# --------------------------------------------------------------------------- #
# Alerts: dedup (section 3) and undelivered
# --------------------------------------------------------------------------- #


def test_insert_alert_dedups_same_key_inside_window(db: Database):
    assert db.insert_alert(make_alert("k", T0)) is not None
    assert db.insert_alert(make_alert("k", T0 + 23 * HOUR)) is None
    assert db.counts()["alerts"] == 1


def test_insert_alert_allows_same_key_outside_window(db: Database):
    first = db.insert_alert(make_alert("k", T0))
    second = db.insert_alert(make_alert("k", T0 + 25 * HOUR))
    assert first is not None and second is not None and first != second
    assert db.counts()["alerts"] == 2


def test_insert_alert_different_key_inside_window_inserts(db: Database):
    db.insert_alert(make_alert("k1", T0))
    assert db.insert_alert(make_alert("k2", T0 + HOUR)) is not None


def test_insert_alert_honours_custom_window(db: Database):
    db.insert_alert(make_alert("k", T0), dedup_window_hours=1)
    assert db.insert_alert(make_alert("k", T0 + 2 * HOUR), dedup_window_hours=1) is not None


def test_insert_alert_dedups_even_after_delivery(db: Database):
    aid = db.insert_alert(make_alert("k", T0))
    db.mark_delivered(aid, "ntfy")
    assert db.insert_alert(make_alert("k", T0 + HOUR)) is None


def test_undelivered_alerts_excludes_delivered(db: Database):
    sent = db.insert_alert(make_alert("sent"))
    db.insert_alert(make_alert("pending"))
    db.mark_delivered(sent, "ntfy")
    assert [a.alert_key for a in db.undelivered_alerts(max_age_hours=24)] == ["pending"]


def test_undelivered_alerts_excludes_older_than_max_age(db: Database, clock: Clock):
    clock.t = T0 + 30 * HOUR
    db.insert_alert(make_alert("old", T0))
    db.insert_alert(make_alert("fresh", T0 + 29 * HOUR))
    got = db.undelivered_alerts(max_age_hours=24)
    assert [a.alert_key for a in got] == ["fresh"]


def test_undelivered_alerts_empty_on_fresh_db(db: Database):
    assert db.undelivered_alerts(max_age_hours=24) == []
