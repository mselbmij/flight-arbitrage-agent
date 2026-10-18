"""Pydantic v2 schemas — the one irreversible artifact in this repo.

Every row in fares.db, every line of watchlists.yaml and every provider payload
is validated through these models before it is stored or acted on. Changing a
field here is a migration; see ARCHITECTURE.md § "Schema change policy".

Design rules enforced by these models (ARCHITECTURE.md explains each):
  * Money is integer minor units (cents) + ISO currency. Never float.
  * Timestamps that mean "when did we observe this" are UTC and tz-aware.
    Timestamps that mean "when does the plane leave" are LOCAL wall-clock,
    tz-aware, because the 06:00 departure rule is about the passenger's day.
  * A fare is identified by its itinerary_key (carriers + flight numbers +
    local departure times), not by price or provider. That is what lets
    post-purchase tracking follow "the exact flight".
  * Unknown fare brand is not an error, but it is never trusted for a BUY.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Primitive types
# --------------------------------------------------------------------------- #

IATA = Annotated[str, Field(pattern=r"^[A-Z]{3}$", description="IATA airport code")]
CarrierCode = Annotated[str, Field(pattern=r"^[A-Z0-9]{2}$", description="IATA airline code")]
Cents = Annotated[int, Field(ge=0, description="Money in minor units; never float")]
Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")]

#: Airports that serve the same metro. A connection that changes airport
#: within one group is a "co-terminal swap" and rejected by default.
COTERMINAL_GROUPS: dict[str, frozenset[str]] = {
    "NYC": frozenset({"JFK", "LGA", "EWR"}),
    "WAS": frozenset({"IAD", "DCA", "BWI"}),
    "CHI": frozenset({"ORD", "MDW"}),
    "HOU": frozenset({"IAH", "HOU"}),
    "DFW": frozenset({"DFW", "DAL"}),
    "QLA": frozenset({"LAX", "BUR", "LGB", "SNA", "ONT"}),
    "QSF": frozenset({"SFO", "OAK", "SJC"}),
    "MIA": frozenset({"MIA", "FLL"}),
    "LON": frozenset({"LHR", "LGW", "STN", "LTN", "LCY"}),
    "PAR": frozenset({"CDG", "ORY"}),
    "TYO": frozenset({"HND", "NRT"}),
    "MIL": frozenset({"MXP", "LIN"}),
}


def coterminal_group(airport: str) -> str | None:
    for group, members in COTERMINAL_GROUPS.items():
        if airport in members:
            return group
    return None


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Provider(StrEnum):
    SERPAPI = "serpapi"  # Google Flights via SerpAPI; source of the benchmark band
    AMADEUS = "amadeus"  # Amadeus Self-Service; branded fares + second price source
    MOCK = "mock"  # seeded synthetic data; never alerts for real


class Cabin(StrEnum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class FareBrand(StrEnum):
    """Normalized fare family. Providers map their raw brand names onto this.

    BASIC_ECONOMY is rejected by the risk filter unless the watchlist allows it.
    UNKNOWN is allowed through the filter but can never *alone* trigger a BUY —
    the alert carries an "unverified brand" flag instead.
    """

    BASIC_ECONOMY = "basic_economy"
    MAIN_CABIN = "main_cabin"
    MAIN_PLUS = "main_plus"  # Comfort+, Main Plus, Premium Select-lite etc.
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"
    UNKNOWN = "unknown"


class PriceLevel(StrEnum):
    """Google Flights' own label for where the current low sits vs history."""

    LOW = "low"
    TYPICAL = "typical"
    HIGH = "high"
    UNKNOWN = "unknown"


class Regime(StrEnum):
    """Days-to-departure windows. Thresholds live in watchlists.yaml, not here."""

    MONITOR = "monitor"  # DTD > 65
    PRIME = "prime"  # 21 < DTD <= 65
    FENCE = "fence"  # 14 < DTD <= 21  (advance-purchase buckets close at 21/14)
    LATE = "late"  # DTD <= 14
    POST_PURCHASE = "post_purchase"
    EXPIRED = "expired"  # departure passed


class WatchStatus(StrEnum):
    WATCHING = "watching"
    PURCHASED = "purchased"
    PAUSED = "paused"
    EXPIRED = "expired"


class AlertKind(StrEnum):
    BUY = "buy"  # price in strike zone for the current regime
    EXTREME_LOW = "extreme_low"  # MONITOR regime anomaly
    FENCE_WARNING = "fence_warning"  # entering FENCE regime; not price-driven
    CREDIT_DROP = "credit_drop"  # purchased fare now cheaper by >= threshold
    DOT_24H = "dot_24h"  # inside the free-cancellation window after purchase
    BUDGET_TRIPPED = "budget_tripped"  # circuit breaker fired
    PROVIDER_ERROR = "provider_error"


class ApiCallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    SKIPPED_BREAKER = "skipped_breaker"


# --------------------------------------------------------------------------- #
# Config: watchlists.yaml
# --------------------------------------------------------------------------- #


class StrikeRule(BaseModel):
    """`price <= reference * multiplier` ⇒ strike.

    reference is always an *external* number (the benchmark band or the user's
    own ceiling). Internal statistics are deliberately not a reference until
    the dataset justifies them.
    """

    model_config = ConfigDict(extra="forbid")

    reference: Literal["typical_low", "typical_high", "lowest_seen", "ceiling"]
    multiplier: float = Field(gt=0, le=2)

    @field_validator("multiplier")
    @classmethod
    def _at_most_three_decimals(cls, v: float) -> float:
        # the strike compare is exact Decimal(str(multiplier)); more precision than this
        # is a typo, not a strategy
        if Decimal(str(v)).as_tuple().exponent < -3:  # type: ignore[operator]
            raise ValueError("multiplier may have at most 3 decimal places")
        return v


class StrikeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monitor: StrikeRule
    prime: StrikeRule
    fence: StrikeRule
    late: StrikeRule


class Constraints(BaseModel):
    """Routing + schedule sanity. Anything failing these is rejected before
    pricing logic ever sees it."""

    model_config = ConfigDict(extra="forbid")

    allow_basic_economy: bool = False
    max_stops: int = Field(default=1, ge=0, le=3)
    max_layover_minutes: int = Field(default=180, ge=30, le=1440)
    allow_coterminal_swap: bool = False
    reject_overnight_layover: bool = True
    earliest_departure_local: time = time(6, 0)
    latest_arrival_local: time = time(23, 30)
    allowed_carriers: list[CarrierCode] = Field(default_factory=list)  # empty = any
    excluded_carriers: list[CarrierCode] = Field(default_factory=list)


class PollConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Legacy carriers refile fares a few times a day via ATPCO; >3 is waste.
    times_per_day: int = Field(default=2, ge=1, le=4)
    providers: list[Provider] = Field(default_factory=lambda: [Provider.SERPAPI])
    #: How many concrete SearchTargets of this watchlist one run may poll.
    #: Targets are visited round-robin by least-recently-polled, so every target
    #: is refreshed every ceil(n_targets / targets_per_run) runs.
    targets_per_run: int = Field(default=2, ge=1, le=20)
    #: Optional cheap parametric scan (Amadeus cheapest-date / inspiration
    #: endpoints: one call per origin-destination pair, returns a date grid).
    #: When set, the scan runs first and its cheapest dates are polled before
    #: the round-robin continues. None = no scan tier.
    scan_provider: Provider | None = None


class PostPurchaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Alert when current price <= purchase price - this. Default $25.
    credit_threshold_cents: Cents = 2500
    #: US DOT: free cancel within 24h if booked >= 7 days before departure.
    dot_window_hours: int = Field(default=24, ge=1, le=48)


class Purchase(BaseModel):
    """What you bought, so the tracker can follow that exact itinerary.

    Deliberately no PNR / confirmation code: it is not needed to track a fare,
    and this file lives in git.
    """

    model_config = ConfigDict(extra="forbid")

    purchased_at: datetime  # tz-aware
    price_cents: Cents
    currency: str = "USD"
    itinerary_key: str = Field(min_length=12, max_length=40)
    #: Which concrete target you bought (`fares targets <watchlist_id>` lists them).
    #: Post-purchase polling is restricted to this one.
    target_id: str = Field(min_length=8, max_length=80)
    carrier: CarrierCode
    changeable: bool = True  # Basic Economy would be False → credit harvesting impossible

    @field_validator("purchased_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("purchased_at must be timezone-aware")
        return v


class Defaults(BaseModel):
    """`defaults:` block. Every field has a value so a watchlist may omit everything
    but route and dates."""

    model_config = ConfigDict(extra="forbid")

    passengers: int = Field(default=1, ge=1, le=9)
    cabin: Cabin = Cabin.ECONOMY
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    poll: PollConfig = Field(default_factory=PollConfig)
    constraints: Constraints = Field(default_factory=Constraints)
    strike: StrikeConfig = Field(
        default_factory=lambda: StrikeConfig(
            monitor=StrikeRule(reference="typical_low", multiplier=0.90),
            prime=StrikeRule(reference="typical_low", multiplier=1.00),
            fence=StrikeRule(reference="typical_low", multiplier=1.15),
            late=StrikeRule(reference="ceiling", multiplier=1.00),
        )
    )
    post_purchase: PostPurchaseConfig = Field(default_factory=PostPurchaseConfig)


#: Hard cap on grid expansion per watchlist. A grid this size cannot be polled
#: daily on any sane budget; the cap exists so a typo in a date window fails at
#: load time instead of silently starving every other watchlist.
MAX_TARGETS_PER_WATCHLIST = 60


class DateWindow(BaseModel):
    """Inclusive date range, optionally restricted to weekdays (0=Mon .. 6=Sun)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("DateWindow.end before start")
        return self

    def dates(self) -> list[date]:
        out = []
        d = self.start
        while d <= self.end:
            if self.weekdays is None or d.weekday() in self.weekdays:
                out.append(d)
            d += timedelta(days=1)
        return out


class TripLength(BaseModel):
    """Return = depart + N days for N in [min_days, max_days]. Alternative to an
    explicit return date/window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_days: int = Field(ge=1, le=90)
    max_days: int = Field(ge=1, le=90)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.max_days < self.min_days:
            raise ValueError("TripLength.max_days < min_days")
        return self


class SearchTarget(BaseModel):
    """One concrete (origin, destination, depart, return) a provider can be asked
    about. Derived from a Watchlist grid; never written by hand.

    target_id is deterministic so observations survive YAML edits that don't
    change the leg itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    watchlist_id: Slug
    origin: IATA
    destination: IATA
    depart: date
    return_: date | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_id(self) -> str:
        ret = self.return_.isoformat() if self.return_ else "ow"
        return (
            f"{self.watchlist_id}:{self.origin}{self.destination}:{self.depart.isoformat()}:{ret}"
        )

    @property
    def is_round_trip(self) -> bool:
        return self.return_ is not None

    @property
    def trip_length_days(self) -> int | None:
        return (self.return_ - self.depart).days if self.return_ else None

    def days_to_departure(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        return (self.depart - now.date()).days

    def regime(self, now: datetime | None = None) -> Regime:
        dtd = self.days_to_departure(now)
        if dtd < 0:
            return Regime.EXPIRED
        if dtd > 65:
            return Regime.MONITOR
        if dtd > 21:
            return Regime.PRIME
        if dtd > 14:
            return Regime.FENCE
        return Regime.LATE


def _as_list(v: str | list[str]) -> list[str]:
    return [v] if isinstance(v, str) else list(v)


class Watchlist(BaseModel):
    """A route *grid* as written in YAML. Scalars and lists/windows are both
    accepted so the simple case stays one line per field:

        origin: SEA                      origin: [SEA, PDX]
        destination: SJC                 destination: [SJC, SFO, OAK]
        depart: 2026-11-25               depart: {start: 2026-11-20, end: 2026-11-26, weekdays: [2, 3]}
        return: 2026-11-29               trip_length: {min_days: 3, max_days: 5}

    Expansion is the cartesian product; MAX_TARGETS_PER_WATCHLIST caps it.
    Optional fields fall back to `defaults:` — see WatchlistFile.resolved()."""

    model_config = ConfigDict(extra="forbid")

    id: Slug
    origin: IATA | list[IATA] = Field(min_length=1)
    destination: IATA | list[IATA] = Field(min_length=1)
    depart: date | DateWindow
    return_: date | DateWindow | None = Field(default=None, alias="return")
    trip_length: TripLength | None = None
    #: "I would happily pay this." Absolute strike in LATE regime; always alerts if hit.
    #: One ceiling for the whole grid — if SFO is worth more to you than OAK, make two watchlists.
    ceiling_cents: Cents
    status: WatchStatus = WatchStatus.WATCHING
    purchase: Purchase | None = None

    # overrides (None = use defaults)
    passengers: int | None = Field(default=None, ge=1, le=9)
    cabin: Cabin | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    poll: PollConfig | None = None
    constraints: Constraints | None = None
    strike: StrikeConfig | None = None
    post_purchase: PostPurchaseConfig | None = None
    notes: str | None = None

    @property
    def origins(self) -> list[str]:
        return _as_list(self.origin)

    @property
    def destinations(self) -> list[str]:
        return _as_list(self.destination)

    @property
    def depart_dates(self) -> list[date]:
        return [self.depart] if isinstance(self.depart, date) else self.depart.dates()

    def expand(self) -> list[SearchTarget]:
        targets: list[SearchTarget] = []
        for o in self.origins:
            for d in self.destinations:
                for dep in self.depart_dates:
                    if self.trip_length is not None:
                        rets: list[date | None] = [
                            dep + timedelta(days=n)
                            for n in range(self.trip_length.min_days, self.trip_length.max_days + 1)
                        ]
                    elif self.return_ is None:
                        rets = [None]
                    elif isinstance(self.return_, date):
                        rets = [self.return_]
                    else:
                        rets = [r for r in self.return_.dates() if r > dep]
                    for ret in rets:
                        targets.append(
                            SearchTarget(
                                watchlist_id=self.id,
                                origin=o,
                                destination=d,
                                depart=dep,
                                return_=ret,
                            )
                        )
        return targets

    @model_validator(mode="after")
    def _sanity(self) -> Self:
        if len(set(self.origins)) != len(self.origins):
            raise ValueError(f"{self.id}: duplicate origins")
        if len(set(self.destinations)) != len(self.destinations):
            raise ValueError(f"{self.id}: duplicate destinations")
        for o in self.origins:
            for d in self.destinations:
                if o == d:
                    raise ValueError(f"{self.id}: {o} appears as both origin and destination")
                if coterminal_group(o) and coterminal_group(o) == coterminal_group(d):
                    raise ValueError(f"{self.id}: {o} and {d} are co-terminal")
        if self.trip_length is not None and self.return_ is not None:
            raise ValueError(f"{self.id}: give either return or trip_length, not both")
        if isinstance(self.return_, date):
            if any(self.return_ <= dep for dep in self.depart_dates):
                raise ValueError(f"{self.id}: return must be after every depart date")
        if self.status is WatchStatus.PURCHASED and self.purchase is None:
            raise ValueError(f"{self.id}: status=purchased requires a purchase block")
        if self.status is not WatchStatus.PURCHASED and self.purchase is not None:
            raise ValueError(f"{self.id}: purchase block present but status != purchased")
        targets = self.expand()
        if not targets:
            raise ValueError(f"{self.id}: grid expands to zero targets (check return window)")
        if len(targets) > MAX_TARGETS_PER_WATCHLIST:
            raise ValueError(
                f"{self.id}: grid expands to {len(targets)} targets; "
                f"cap is {MAX_TARGETS_PER_WATCHLIST}. Narrow the window or split the watchlist."
            )
        if self.purchase is not None and self.purchase.target_id not in {
            t.target_id for t in targets
        }:
            raise ValueError(
                f"{self.id}: purchase.target_id is not one of this watchlist's targets"
            )
        return self


class ResolvedWatchlist(BaseModel):
    """Watchlist with every default filled and the grid expanded. This is what
    the engine consumes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Slug
    targets: list[SearchTarget]
    ceiling_cents: Cents
    status: WatchStatus
    purchase: Purchase | None
    passengers: int
    cabin: Cabin
    currency: str
    poll: PollConfig
    constraints: Constraints
    strike: StrikeConfig
    post_purchase: PostPurchaseConfig
    notes: str | None

    @property
    def pollable_targets(self) -> list[SearchTarget]:
        """Targets a run may spend budget on: all unexpired ones, or only the
        purchased one after purchase."""
        if self.status is WatchStatus.PURCHASED and self.purchase is not None:
            return [t for t in self.targets if t.target_id == self.purchase.target_id]
        if self.status is not WatchStatus.WATCHING:
            return []
        return [t for t in self.targets if t.regime() is not Regime.EXPIRED]

    def regime_for(self, target: SearchTarget, now: datetime | None = None) -> Regime:
        if self.status is WatchStatus.PURCHASED:
            return Regime.POST_PURCHASE
        return target.regime(now)

    def strike_rule(self, regime: Regime) -> StrikeRule | None:
        return {
            Regime.MONITOR: self.strike.monitor,
            Regime.PRIME: self.strike.prime,
            Regime.FENCE: self.strike.fence,
            Regime.LATE: self.strike.late,
        }.get(regime)


class WatchlistFile(BaseModel):
    """Top-level shape of config/watchlists.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    defaults: Defaults = Field(default_factory=Defaults)
    watchlists: list[Watchlist]

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [w.id for w in self.watchlists]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate watchlist ids: {sorted(dupes)}")
        return self

    def calls_per_day(self) -> int:
        """Worst-case provider calls/day this config asks for. Compare to
        MAX_DAILY_API_CALLS before you commit a change."""
        total = 0
        for r in self.resolved():
            n = min(r.poll.targets_per_run, len(r.pollable_targets))
            per_run = n * len(r.poll.providers) + (1 if r.poll.scan_provider else 0)
            total += per_run * r.poll.times_per_day
        return total

    def resolved(self) -> list[ResolvedWatchlist]:
        out: list[ResolvedWatchlist] = []
        d = self.defaults
        for w in self.watchlists:
            out.append(
                ResolvedWatchlist(
                    id=w.id,
                    targets=w.expand(),
                    ceiling_cents=w.ceiling_cents,
                    status=w.status,
                    purchase=w.purchase,
                    passengers=w.passengers if w.passengers is not None else d.passengers,
                    cabin=w.cabin or d.cabin,
                    currency=w.currency or d.currency,
                    poll=w.poll or d.poll,
                    constraints=w.constraints or d.constraints,
                    strike=w.strike or d.strike,
                    post_purchase=w.post_purchase or d.post_purchase,
                    notes=w.notes,
                )
            )
        return out

    def config_hash(self) -> str:
        """Stable hash of the resolved config; stored on each observation so a
        risk verdict can be traced to the rules that produced it."""
        payload = "|".join(r.model_dump_json(exclude={"notes"}) for r in self.resolved())
        return hashlib.sha1(payload.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Provider output: itineraries and fares
# --------------------------------------------------------------------------- #


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    carrier: CarrierCode  # marketing carrier
    operating_carrier: CarrierCode | None = None
    flight_number: Annotated[str, Field(pattern=r"^[0-9]{1,4}[A-Z]?$")]
    origin: IATA
    destination: IATA
    departure_local: datetime  # tz-aware, local to origin
    arrival_local: datetime  # tz-aware, local to destination
    aircraft: str | None = None

    @field_validator("departure_local", "arrival_local")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("segment times must be timezone-aware local times")
        return v

    @property
    def duration_minutes(self) -> int:
        return int((self.arrival_local - self.departure_local).total_seconds() // 60)

    @property
    def key(self) -> str:
        return f"{self.carrier}{self.flight_number}@{self.departure_local:%Y%m%d%H%M}{self.origin}{self.destination}"


class Slice(BaseModel):
    """One direction of travel: 1..N segments with layovers between them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segments: list[Segment] = Field(min_length=1)

    @model_validator(mode="after")
    def _contiguous(self) -> Self:
        for a, b in zip(self.segments, self.segments[1:], strict=False):
            if b.departure_local <= a.arrival_local:
                raise ValueError("segments overlap or are out of order")
        return self

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def layovers_minutes(self) -> list[int]:
        return [
            int((b.departure_local - a.arrival_local).total_seconds() // 60)
            for a, b in zip(self.segments, self.segments[1:], strict=False)
        ]

    @property
    def max_layover_minutes(self) -> int:
        return max(self.layovers_minutes, default=0)

    @property
    def has_overnight_layover(self) -> bool:
        return any(
            b.departure_local.date() != a.arrival_local.date()
            for a, b in zip(self.segments, self.segments[1:], strict=False)
        )

    @property
    def has_coterminal_swap(self) -> bool:
        """Arrive JFK, depart EWR."""
        return any(
            a.destination != b.origin
            and coterminal_group(a.destination) is not None
            and coterminal_group(a.destination) == coterminal_group(b.origin)
            for a, b in zip(self.segments, self.segments[1:], strict=False)
        )

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def departure_local(self) -> datetime:
        return self.segments[0].departure_local

    @property
    def arrival_local(self) -> datetime:
        return self.segments[-1].arrival_local


class Itinerary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slices: list[Slice] = Field(min_length=1, max_length=2)  # one-way or round-trip

    @computed_field  # type: ignore[prop-decorator]
    @property
    def itinerary_key(self) -> str:
        """Identity of "this exact flight set", provider-independent."""
        raw = "|".join(seg.key for sl in self.slices for seg in sl.segments)
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    @property
    def outbound(self) -> Slice:
        return self.slices[0]

    @property
    def inbound(self) -> Slice | None:
        return self.slices[1] if len(self.slices) == 2 else None

    @property
    def marketing_carriers(self) -> set[str]:
        return {seg.carrier for sl in self.slices for seg in sl.segments}


class RiskVerdict(BaseModel):
    """Output of src/engine/risk_filters.py, stored with the observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    rejections: list[str] = Field(default_factory=list)  # e.g. "basic_economy", "layover>180"
    brand_unverified: bool = False  # FareBrand.UNKNOWN — passes but cannot BUY alone
    #: Round-trip target but the itinerary holds only the outbound slice (SerpAPI RT searches
    #: return outbound legs only). Passes, but a BUY is prefixed "[return not checked]".
    inbound_unverified: bool = False
    config_hash: str


class FareObservation(BaseModel):
    """One priced itinerary seen at one moment from one provider. The core table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None  # assigned by SQLite
    watchlist_id: Slug
    target_id: str
    observed_at: datetime  # UTC
    provider: Provider
    run_id: str
    itinerary: Itinerary
    price_cents: Cents
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    fare_brand: FareBrand = FareBrand.UNKNOWN
    fare_brand_raw: str | None = None  # provider's own label, for mapping fixes
    changeable: bool | None = None
    refundable: bool | None = None
    carry_on_included: bool | None = None
    seats_remaining: int | None = Field(default=None, ge=0)
    days_to_departure: int  # frozen at observation time
    booking_url: str | None = None
    raw_payload_ref: str | None = None  # path under data/raw/ for replay
    risk: RiskVerdict | None = None

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset().total_seconds() != 0:
            raise ValueError("observed_at must be UTC")
        return v

    @computed_field  # type: ignore[prop-decorator]
    @property
    def itinerary_key(self) -> str:
        return self.itinerary.itinerary_key


class BenchmarkBand(BaseModel):
    """External 'typical price' band for a route+date — the only statistical
    reference the engine trusts until ARCHITECTURE.md § "Stats, if ever"."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    watchlist_id: Slug
    target_id: str
    observed_at: datetime
    provider: Provider
    run_id: str
    lowest_price_cents: Cents | None = None
    typical_low_cents: Cents | None = None
    typical_high_cents: Cents | None = None
    price_level: PriceLevel = PriceLevel.UNKNOWN
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if (
            self.typical_low_cents is not None
            and self.typical_high_cents is not None
            and self.typical_low_cents > self.typical_high_cents
        ):
            raise ValueError("typical_low > typical_high")
        return self

    @property
    def usable(self) -> bool:
        return self.typical_low_cents is not None


# --------------------------------------------------------------------------- #
# Operational state
# --------------------------------------------------------------------------- #


class Run(BaseModel):
    """One invocation of the poller. The per-run circuit breaker counts against it."""

    model_config = ConfigDict(extra="forbid")

    run_id: str  # ULID/UUID
    started_at: datetime
    finished_at: datetime | None = None
    trigger: Literal["cron", "manual", "seed"]
    calls_made: int = 0
    status: Literal["running", "ok", "budget_tripped", "error"] = "running"
    error: str | None = None


class ApiCall(BaseModel):
    """Append-only ledger; the daily circuit breaker sums `cost_units` over today."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    run_id: str
    called_at: datetime
    provider: Provider
    endpoint: str
    watchlist_id: Slug | None = None
    target_id: str | None = None
    status: ApiCallStatus
    cost_units: int = Field(default=1, ge=0)
    latency_ms: int | None = None
    error: str | None = None


class Alert(BaseModel):
    """Something worth telling a human. `alert_key` makes delivery idempotent."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    created_at: datetime
    watchlist_id: Slug
    target_id: str | None = None  # None for watchlist-level kinds (BUDGET_TRIPPED)
    kind: AlertKind
    regime: Regime
    observation_id: int | None = None
    itinerary_key: str | None = None
    price_cents: Cents | None = None
    reference_cents: Cents | None = None  # the number the strike was judged against
    message: str
    #: Dedup key. Same key within `dedup_window_hours` is not re-sent.
    alert_key: str
    delivered_at: datetime | None = None
    channel: str | None = None


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #


def load_watchlist_file(data: dict[str, Any]) -> WatchlistFile:
    return WatchlistFile.model_validate(data)
