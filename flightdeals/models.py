"""Shared data types passed between collectors, detection, storage, and alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


PREMIUM_CABINS = {"BUSINESS", "FIRST"}


@dataclass
class FareObservation:
    """One priced fare seen for a route. These rows accumulate into the baseline."""

    origin: str
    destination: str
    price: float
    currency: str
    source: str  # amadeus_inspiration | amadeus_offers | kiwi | ...
    departure_date: str | None = None  # YYYY-MM-DD
    return_date: str | None = None
    cabin: str = "ECONOMY"
    deep_link: str | None = None
    observed_at: str = field(default_factory=utcnow_iso)


@dataclass
class BlogPost:
    """An RSS post that matched the Porto/OPO/Portugal keywords."""

    feed: str
    guid: str
    title: str
    url: str | None
    summary: str
    published_at: str | None  # ISO8601 UTC, None if the feed omits it
    matched_keywords: list[str] = field(default_factory=list)


@dataclass
class Deal:
    """A flagged deal, ready to be stored and alerted."""

    kind: str  # price_drop | error_fare | blog
    source: str
    reason: str
    dedupe_key: str
    origin: str | None = None
    destination: str | None = None
    cabin: str | None = None
    price: float | None = None
    currency: str | None = None
    baseline_price: float | None = None
    discount_pct: float | None = None
    departure_date: str | None = None
    return_date: str | None = None
    url: str | None = None
    found_at: str = field(default_factory=utcnow_iso)


@dataclass
class CollectorResult:
    """What one collector produced during a run."""

    observations: list[FareObservation] = field(default_factory=list)
    posts: list[BlogPost] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    api_calls: int = 0
