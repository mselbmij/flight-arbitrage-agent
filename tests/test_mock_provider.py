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
    assert be.risk is not None and be.risk.passed is False
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
