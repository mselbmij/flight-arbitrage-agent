"""ARCHITECTURE.md section 4: reject fares that are traps, never judge price.

Pure functions, no I/O. All checks run; tags come out in table order. Price is
never an input: cheap is not a risk, wrong product is.
"""

from __future__ import annotations

from src.models import FareBrand, FareObservation, ResolvedWatchlist, RiskVerdict


def risk_verdict(obs: FareObservation, wl: ResolvedWatchlist, *, config_hash: str) -> RiskVerdict:
    c = wl.constraints
    slices = obs.itinerary.slices
    rejections: list[str] = []

    if obs.fare_brand is FareBrand.BASIC_ECONOMY and not c.allow_basic_economy:
        rejections.append("basic_economy")
    if any(s.stops > c.max_stops for s in slices):
        rejections.append(f"stops>{c.max_stops}")
    if any(s.max_layover_minutes > c.max_layover_minutes for s in slices):
        rejections.append(f"layover>{c.max_layover_minutes}")
    if not c.allow_coterminal_swap and any(s.has_coterminal_swap for s in slices):
        rejections.append("coterminal_swap")
    if c.reject_overnight_layover and any(s.has_overnight_layover for s in slices):
        rejections.append("overnight_layover")
    if any(s.departure_local.time() < c.earliest_departure_local for s in slices):
        rejections.append(f"departs_before_{c.earliest_departure_local:%H:%M}")
    if any(s.arrival_local.time() > c.latest_arrival_local for s in slices):
        rejections.append(f"arrives_after_{c.latest_arrival_local:%H:%M}")

    carriers = obs.itinerary.marketing_carriers
    offending: set[str] = set()
    if c.allowed_carriers:
        offending |= carriers - set(c.allowed_carriers)
    offending |= carriers & set(c.excluded_carriers)
    rejections.extend(f"carrier_{code}" for code in sorted(offending))

    return RiskVerdict(
        passed=not rejections,
        rejections=rejections,
        brand_unverified=obs.fare_brand is FareBrand.UNKNOWN,
        config_hash=config_hash,
    )


def apply(
    observations: list[FareObservation], wl: ResolvedWatchlist, *, config_hash: str
) -> list[FareObservation]:
    """Return copies with `.risk` set; the input list and its items are untouched."""
    return [
        o.model_copy(update={"risk": risk_verdict(o, wl, config_hash=config_hash)})
        for o in observations
    ]
