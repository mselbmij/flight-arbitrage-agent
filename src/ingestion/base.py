"""Provider protocol (ARCHITECTURE.md section 5).

Clients raise `ProviderError` rather than returning partial data, and never retry on their own:
the runner owns retries, the ApiCall ledger and the circuit breaker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from src.models import (
    BenchmarkBand,
    FareObservation,
    Provider,
    ResolvedWatchlist,
    SearchTarget,
)


class ProviderError(Exception):
    def __init__(
        self,
        provider: Provider,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


class ProviderResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    observations: list[FareObservation]
    band: BenchmarkBand | None
    raw: dict[str, Any]  # payload as received; the runner writes it to data/raw/


class FlightProvider(Protocol):
    name: Provider

    def search(
        self,
        target: SearchTarget,
        watchlist: ResolvedWatchlist,
        run_id: str,
        observed_at: datetime,
    ) -> ProviderResult: ...
