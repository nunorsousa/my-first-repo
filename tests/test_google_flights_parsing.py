"""Tests for the Google Flights collector's caching/parsing/error handling.

`get_flights()` and `time.sleep()` are mocked throughout — these tests never
touch the network (the library itself has no test-friendly seams, so we
patch it at the module level where the collector calls it).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fast_flights.exceptions import FlightsNotFound
from fast_flights.model import CarbonEmission, Flights

import flightdeals.collectors.google_flights as gfmod
from flightdeals.models import CollectorResult


@pytest.fixture(autouse=True)
def no_sleep():
    with patch.object(gfmod.time, "sleep"):
        yield


@pytest.fixture
def collector(cfg, conn):
    return gfmod.GoogleFlightsCollector(cfg, conn)


def fake_result(*prices: float) -> list[Flights]:
    return [Flights(type="t", price=p, airlines=["TAP"], flights=[], carbon=CarbonEmission(0, 0)) for p in prices]


def test_enabled_needs_no_credentials(collector):
    assert collector.enabled() is True  # unlike Amadeus/Kiwi, no API key gate


def test_quote_picks_cheapest_and_builds_deep_link(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", return_value=fake_result(734.50, 289.0, 612.0)):
        obs = collector._quote("OPO", "JFK", "economy", "2026-09-10", "2026-09-17", result)
    assert obs.price == 289.0
    assert obs.origin == "OPO" and obs.destination == "JFK"
    assert obs.cabin == "ECONOMY"
    assert obs.source == "google_flights"
    assert obs.deep_link.startswith("https://www.google.com/travel/flights/search?tfs=")
    assert result.api_calls == 1


def test_business_seat_maps_to_business_cabin(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", return_value=fake_result(900.0)):
        obs = collector._quote("OPO", "JFK", "business", "2026-09-10", "2026-09-17", result)
    assert obs.cabin == "BUSINESS"


def test_zero_and_negative_prices_are_ignored(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", return_value=fake_result(0, -5, 410.0)):
        obs = collector._quote("OPO", "JFK", "economy", "2026-09-10", "2026-09-17", result)
    assert obs.price == 410.0


def test_empty_result_list_yields_no_observation(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", return_value=[]):
        assert collector._quote("OPO", "JFK", "economy", "2026-09-10", "2026-09-17", result) is None
    assert result.errors == []  # genuinely no fares is not an error


def test_flights_not_found_is_not_an_error(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", side_effect=FlightsNotFound("none")):
        assert collector._quote("OPO", "XXX", "economy", "2026-09-10", "2026-09-17", result) is None
    assert result.errors == []


def test_network_or_block_failure_is_recorded_not_raised(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", side_effect=RuntimeError("blocked")):
        assert collector._quote("OPO", "YYY", "economy", "2026-09-10", "2026-09-17", result) is None
    assert len(result.errors) == 1
    assert "blocked" in result.errors[0]


def test_second_call_uses_cache_not_network(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", return_value=fake_result(300.0)) as mock_get:
        collector._quote("OPO", "JFK", "economy", "2026-09-10", "2026-09-17", result)
        collector._quote("OPO", "JFK", "economy", "2026-09-10", "2026-09-17", result)
    assert mock_get.call_count == 1
    assert result.api_calls == 1


def test_not_found_result_is_also_cached(collector):
    result = CollectorResult()
    with patch.object(gfmod, "get_flights", side_effect=FlightsNotFound("none")) as mock_get:
        collector._quote("OPO", "XXX", "economy", "2026-09-10", "2026-09-17", result)
        collector._quote("OPO", "XXX", "economy", "2026-09-10", "2026-09-17", result)
    assert mock_get.call_count == 1  # the "no flights" answer itself is cached


def test_collect_respects_discovery_and_watchlist_call_budgets(collector, cfg):
    cfg.discovery_destinations = ["MAD", "CDG", "FCO"]
    cfg.watchlist = ["JFK", "GRU"]
    cfg.google_flights.max_discovery_calls_per_run = 1
    cfg.google_flights.max_offer_calls_per_run = 1
    cfg.google_flights.premium_cabin_check = True

    with patch.object(gfmod, "get_flights", return_value=fake_result(500.0)) as mock_get:
        result = collector.collect()

    # 1 discovery destination (economy) + 1 watchlist destination (economy + business) = 3 calls
    assert mock_get.call_count == 3
    assert len(result.observations) == 3


def test_collect_rotates_least_recently_checked_first(collector, cfg, conn):
    from flightdeals import db
    from flightdeals.models import FareObservation

    cfg.discovery_destinations = []
    cfg.watchlist = ["JFK", "GRU"]
    cfg.google_flights.max_offer_calls_per_run = 1
    cfg.google_flights.premium_cabin_check = False

    # JFK already checked recently via google_flights; GRU never checked.
    db.insert_observations(conn, [
        FareObservation(origin="OPO", destination="JFK", price=500, currency="EUR", source="google_flights")
    ])

    with patch.object(gfmod, "get_flights", return_value=fake_result(400.0)) as mock_get:
        result = collector.collect()

    assert mock_get.call_count == 1
    assert result.observations[0].destination == "GRU"  # never-checked wins over recently-checked
