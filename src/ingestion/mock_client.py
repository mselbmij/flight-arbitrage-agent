"""Mock provider: replays JSON fixtures (ARCHITECTURE.md section 5)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.ingestion.base import ProviderError, ProviderResult
from src.models import (
    BenchmarkBand,
    FareObservation,
    Provider,
    ResolvedWatchlist,
    SearchTarget,
)


class MockProvider:
    name = Provider.MOCK

    def __init__(self, fixture_dir: Path) -> None:
        self.fixture_dir = Path(fixture_dir)

    def search(
        self,
        target: SearchTarget,
        watchlist: ResolvedWatchlist,
        run_id: str,
        observed_at: datetime,
    ) -> ProviderResult:
        path = self.fixture_dir / (target.target_id.replace(":", "_") + ".json")
        if not path.is_file():
            raise ProviderError(Provider.MOCK, f"no fixture for {target.target_id}")
        try:
            data = json.loads(path.read_text())
            stamp = {
                "provider": Provider.MOCK.value,
                "run_id": run_id,
                "observed_at": observed_at,
            }
            # The fixture is stale data, not authority: identity comes from the requested target,
            # and the risk verdict is the engine's job, so a fixture can never smuggle one in.
            identity = {"watchlist_id": target.watchlist_id, "target_id": target.target_id}
            observations = [
                FareObservation.model_validate(
                    {
                        **o,
                        **stamp,
                        **identity,
                        "risk": None,
                        "days_to_departure": target.days_to_departure(observed_at),
                    }
                )
                for o in data["observations"]
            ]
            band_raw = data.get("band")
            band = (
                None
                if band_raw is None
                else BenchmarkBand.model_validate({**band_raw, **stamp, **identity})
            )
            # ValueError covers JSONDecodeError, UnicodeDecodeError and pydantic ValidationError
            result = ProviderResult(observations=observations, band=band, raw=data.get("raw", {}))
        except (ValueError, KeyError, TypeError, OSError) as e:
            raise ProviderError(
                Provider.MOCK, f"bad fixture: {path.name}: {type(e).__name__}", retryable=False
            ) from e
        return result
