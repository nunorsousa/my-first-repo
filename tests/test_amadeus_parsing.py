"""Tests for Amadeus response parsing (pure functions, no HTTP)."""

from __future__ import annotations

from flightdeals.collectors.amadeus import AmadeusCollector, google_flights_link

INSPIRATION_PAYLOAD = {
    "data": [
        {
            "type": "flight-destination",
            "origin": "OPO",
            "destination": "MAD",
            "departureDate": "2026-08-01",
            "returnDate": "2026-08-08",
            "price": {"total": "58.30"},
        },
        {"destination": "BAD", "price": {"total": "not-a-number"}},  # must be skipped
        {"price": {"total": "99.00"}},  # missing destination — skipped
    ],
    "meta": {"currency": "EUR"},
}


def test_parse_inspiration_happy_path_and_bad_rows():
    observations = AmadeusCollector.parse_inspiration(INSPIRATION_PAYLOAD, "OPO", "USD")
    assert len(observations) == 1
    obs = observations[0]
    assert (obs.origin, obs.destination, obs.price) == ("OPO", "MAD", 58.30)
    assert obs.currency == "EUR"  # from meta, not the fallback
    assert obs.cabin == "ECONOMY"
    assert obs.source == "amadeus_inspiration"
    assert obs.departure_date == "2026-08-01" and obs.return_date == "2026-08-08"


def test_parse_inspiration_uses_fallback_currency():
    payload = {"data": [{"destination": "MAD", "price": {"total": "58.30"}}]}
    assert AmadeusCollector.parse_inspiration(payload, "OPO", "EUR")[0].currency == "EUR"


OFFERS_PAYLOAD = {
    "data": [
        {
            "price": {"grandTotal": "734.50", "currency": "EUR"},
            "itineraries": [
                {"segments": [{"departure": {"iataCode": "OPO", "at": "2026-08-29T10:00:00"}}]},
                {"segments": [{"departure": {"iataCode": "JFK", "at": "2026-09-05T18:00:00"}}]},
            ],
        },
        {
            "price": {"grandTotal": "612.00", "currency": "EUR"},
            "itineraries": [
                {"segments": [{"departure": {"iataCode": "OPO", "at": "2026-08-29T06:00:00"}}]},
                {"segments": [{"departure": {"iataCode": "JFK", "at": "2026-09-05T12:00:00"}}]},
            ],
        },
    ]
}


def test_parse_offers_picks_cheapest():
    obs = AmadeusCollector.parse_offers(OFFERS_PAYLOAD, "OPO", "JFK", "ECONOMY")
    assert obs.price == 612.00
    assert obs.departure_date == "2026-08-29"
    assert obs.return_date == "2026-09-05"
    assert obs.cabin == "ECONOMY"
    assert obs.source == "amadeus_offers"


def test_parse_offers_empty_and_malformed():
    assert AmadeusCollector.parse_offers({"data": []}, "OPO", "JFK", "ECONOMY") is None
    assert AmadeusCollector.parse_offers({}, "OPO", "JFK", "ECONOMY") is None
    # price parses even when itinerary structure is missing
    obs = AmadeusCollector.parse_offers(
        {"data": [{"price": {"grandTotal": "300.00"}}]}, "OPO", "JFK", "BUSINESS"
    )
    assert obs.price == 300.0 and obs.departure_date is None


def test_google_flights_link_is_url_encoded():
    link = google_flights_link("OPO", "JFK", "2026-08-29", "2026-09-05")
    assert link.startswith("https://www.google.com/travel/flights?q=")
    assert "Flights+from+OPO+to+JFK" in link
