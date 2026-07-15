from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from flightdeals import db
from flightdeals.config import Config
from flightdeals.models import FareObservation


@pytest.fixture
def cfg() -> Config:
    # Built-in defaults: 40% threshold, 90-day window, min 5 observations,
    # z-score 3.0, decimal ratio 0.15, premium ratio 1.4, floor 10.
    return Config()


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    yield connection
    connection.close()


@pytest.fixture
def seed_route(conn):
    """Insert a price history for a route, spread evenly over the last `days` days."""

    def _seed(
        prices: list[float],
        origin: str = "OPO",
        destination: str = "JFK",
        cabin: str = "ECONOMY",
        days: int = 30,
        end_days_ago: float = 0,
        source: str = "amadeus_offers",
    ) -> None:
        """Observations span [now - end_days_ago - days, now - end_days_ago]."""
        end = datetime.now(timezone.utc) - timedelta(days=end_days_ago)
        step = days / max(len(prices), 1)
        observations = [
            FareObservation(
                origin=origin,
                destination=destination,
                price=price,
                currency="EUR",
                source=source,
                cabin=cabin,
                observed_at=(end - timedelta(days=days) + timedelta(days=i * step)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            )
            for i, price in enumerate(prices)
        ]
        db.insert_observations(conn, observations)

    return _seed


def make_obs(price: float, cabin: str = "ECONOMY", origin: str = "OPO", destination: str = "JFK") -> FareObservation:
    return FareObservation(
        origin=origin,
        destination=destination,
        price=price,
        currency="EUR",
        source="amadeus_offers",
        cabin=cabin,
        departure_date="2026-08-29",
        return_date="2026-09-05",
    )
