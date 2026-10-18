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
        data = json.loads(path.read_text())

        stamp = {
            "provider": Provider.MOCK.value,
            "run_id": run_id,
            "observed_at": observed_at,
        }
        observations = [
            FareObservation.model_validate(
                {
                    **o,
                    **stamp,
                    "days_to_departure": target.days_to_departure(observed_at),
                }
            )
            for o in data["observations"]
        ]
        band_raw = data.get("band")
        band = None if band_raw is None else BenchmarkBand.model_validate({**band_raw, **stamp})
        return ProviderResult(observations=observations, band=band, raw=data.get("raw", {}))
