"""Phase 2: src/ingestion/base.py and src/ingestion/mock_client.py (ARCHITECTURE.md section 5)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.ingestion.base import ProviderError, ProviderResult
from src.ingestion.mock_client import MockProvider
from src.models import FareBrand, Provider
from tests.conftest import REPO, TID, load_watchlists

FIXTURES = REPO / "tests" / "fixtures" / "mock"
OBSERVED = datetime(2026, 10, 17, 14, 0, tzinfo=UTC)


def _search(provider: MockProvider, watchlist_id: str = "sea-sjc-thanksgiving", at=OBSERVED):
    wl = next(r for r in load_watchlists().resolved() if r.id == watchlist_id)
    return provider.search(wl.targets[0], wl, "run-9", at)


@pytest.fixture
def result() -> ProviderResult:
    return _search(MockProvider(FIXTURES))


def test_provider_name_is_mock():
    assert MockProvider(FIXTURES).name is Provider.MOCK


def test_loads_three_observations_and_band(result: ProviderResult):
    assert len(result.observations) == 3
    assert result.band is not None
    assert result.band.typical_low_cents == 28000


def test_fixture_name_replaces_colons_with_underscores():
    expected = FIXTURES / (TID.replace(":", "_") + ".json")
    assert expected.exists()


def test_forces_provider_mock_on_every_observation(result: ProviderResult):
    raw = json.loads((FIXTURES / (TID.replace(":", "_") + ".json")).read_text())
    assert {o["provider"] for o in raw["observations"]} != {"mock"}  # fixture really disagrees
    assert {o.provider for o in result.observations} == {Provider.MOCK}


def test_forces_provider_mock_on_band(result: ProviderResult):
    assert result.band is not None and result.band.provider is Provider.MOCK


def test_fills_run_id_and_observed_at_on_everything(result: ProviderResult):
    assert {o.run_id for o in result.observations} == {"run-9"}
    assert {o.observed_at for o in result.observations} == {OBSERVED}
    assert result.band is not None
    assert (result.band.run_id, result.band.observed_at) == ("run-9", OBSERVED)


def test_days_to_departure_computed_from_observed_at(result: ProviderResult):
    # 2026-10-17 -> 2026-11-25 is 39 days
    assert {o.days_to_departure for o in result.observations} == {39}


@pytest.mark.parametrize(
    ("observed", "dtd"),
    [
        (datetime(2026, 11, 25, 23, 59, tzinfo=UTC), 0),
        (datetime(2026, 11, 24, 0, 0, tzinfo=UTC), 1),
        (datetime(2026, 11, 26, 0, 0, tzinfo=UTC), -1),
    ],
)
def test_days_to_departure_edges(observed: datetime, dtd: int):
    got = _search(MockProvider(FIXTURES), at=observed)
    assert {o.days_to_departure for o in got.observations} == {dtd}


def test_observation_content_survives_loading(result: ProviderResult):
    by_brand = {o.fare_brand: o for o in result.observations}
    assert set(by_brand) == {FareBrand.MAIN_CABIN, FareBrand.BASIC_ECONOMY}
    be = by_brand[FareBrand.BASIC_ECONOMY]
    assert be.fare_brand_raw == "Saver"
    stops = sorted(o.itinerary.outbound.stops for o in result.observations)
    assert stops == [0, 0, 1]
    assert {o.target_id for o in result.observations} == {TID}


def test_observations_have_distinct_itinerary_keys(result: ProviderResult):
    keys = [o.itinerary_key for o in result.observations]
    assert len(set(keys)) == 3


def test_raw_payload_is_returned_as_received(result: ProviderResult):
    assert result.raw["source"] == "fixture"


def test_result_is_frozen(result: ProviderResult):
    with pytest.raises((ValidationError, TypeError)):
        result.band = None  # type: ignore[misc]


def test_null_band_in_fixture_yields_none(tmp_path: Path):
    src = FIXTURES / (TID.replace(":", "_") + ".json")
    data = json.loads(src.read_text())
    data["band"] = None
    (tmp_path / src.name).write_text(json.dumps(data))
    got = _search(MockProvider(tmp_path))
    assert got.band is None
    assert len(got.observations) == 3


def test_missing_fixture_raises_non_retryable_provider_error():
    wl_id = "sea-ord-jan"  # no fixture for this target
    with pytest.raises(ProviderError) as exc:
        _search(MockProvider(FIXTURES), wl_id)
    assert exc.value.provider is Provider.MOCK
    assert exc.value.retryable is False
    assert "no fixture" in str(exc.value)


def test_risk_is_always_none_even_when_the_fixture_carries_a_verdict(result: ProviderResult):
    raw = json.loads((FIXTURES / (TID.replace(":", "_") + ".json")).read_text())
    assert any(o.get("risk") for o in raw["observations"])  # fixture really carries verdicts
    assert [o.risk for o in result.observations] == [None, None, None]


def test_identity_comes_from_the_requested_target_not_the_fixture(tmp_path: Path):
    src = FIXTURES / (TID.replace(":", "_") + ".json")
    data = json.loads(src.read_text())
    for o in data["observations"]:
        o["watchlist_id"], o["target_id"] = "stale-wl", "stale-wl:SEASJC:2020-01-01:ow"
    data["band"]["watchlist_id"] = "stale-wl"
    data["band"]["target_id"] = "stale-wl:SEASJC:2020-01-01:ow"
    (tmp_path / src.name).write_text(json.dumps(data))
    got = _search(MockProvider(tmp_path))
    assert {o.target_id for o in got.observations} == {TID}
    assert {o.watchlist_id for o in got.observations} == {"sea-sjc-thanksgiving"}
    assert got.band is not None
    assert (got.band.target_id, got.band.watchlist_id) == (TID, "sea-sjc-thanksgiving")


def _bad_fixture(tmp_path: Path, mutate) -> None:
    src = FIXTURES / (TID.replace(":", "_") + ".json")
    (tmp_path / src.name).write_text(mutate(src.read_text()))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text[: len(text) // 2],  # truncated JSON
        lambda text: json.dumps({"band": None}),  # no "observations" key
        lambda text: json.dumps({"observations": [{"price_cents": "lots"}], "band": None}),
        lambda text: json.dumps([1, 2, 3]),  # not an object
        lambda text: json.dumps({**json.loads(text), "raw": "not-a-dict"}),
    ],
    ids=["truncated", "missing-key", "invalid-observation", "not-an-object", "raw-not-a-dict"],
)
def test_malformed_fixture_raises_non_retryable_provider_error(tmp_path: Path, mutate):
    _bad_fixture(tmp_path, mutate)
    with pytest.raises(ProviderError) as exc:
        _search(MockProvider(tmp_path))
    assert exc.value.provider is Provider.MOCK
    assert exc.value.retryable is False
    assert "bad fixture" in str(exc.value)


def test_unreadable_fixture_raises_provider_error(tmp_path: Path):
    # the file exists but is not UTF-8 text
    (tmp_path / (TID.replace(":", "_") + ".json")).write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ProviderError) as exc:
        _search(MockProvider(tmp_path))
    assert "bad fixture" in str(exc.value) and exc.value.retryable is False


def test_missing_fixture_dir_content_raises_provider_error(tmp_path: Path):
    with pytest.raises(ProviderError):
        _search(MockProvider(tmp_path))


def test_provider_error_defaults_and_fields():
    err = ProviderError(Provider.SERPAPI, "rate limited", status_code=429, retryable=True)
    assert (err.provider, err.status_code, err.retryable) == (Provider.SERPAPI, 429, True)
    plain = ProviderError(Provider.AMADEUS, "bad")
    assert (plain.status_code, plain.retryable) == (None, False)
    assert isinstance(plain, Exception)


def test_fixture_dir_is_not_mutated(tmp_path: Path):
    copy = tmp_path / "mock"
    shutil.copytree(FIXTURES, copy)
    before = {p.name: p.read_bytes() for p in copy.iterdir()}
    _search(MockProvider(copy))
    assert {p.name: p.read_bytes() for p in copy.iterdir()} == before
