"""Google Flights collector — the default price source. No account, no API key.

Wraps the third-party `fast-flights` library (github.com/AWeirdDev/flights),
which builds the same base64-encoded protobuf search request Google Flights'
own web UI sends, fetches it with a browser-impersonating HTTP client, and
parses the returned HTML. Read this before relying on it:

- This is NOT an official API and is not sanctioned by Google's Terms of
  Service for automated access. It is a personal-project convenience, used
  here at low volume with caching and delays to keep the footprint small.
- Google can and does block scraping from shared/datacenter IP ranges. This
  is a real risk specifically for the GitHub Actions cron: it may work fine
  from your own home network and get silently blocked from a hosted runner.
  Check the run log after your first few scheduled runs — if every call is
  erroring, price-based detection just goes quiet; RSS "blog find" deals and
  the dashboard keep working regardless.
- Upstream HTML/markup changes can break parsing without warning.
- There is no "search anywhere" mode like Amadeus's Flight Inspiration
  Search, so broad discovery here means rotating through a curated
  `discovery_destinations` list (config.yaml) rather than a true open search.

If you'd rather have an official, quota-backed source, the Amadeus collector
(flightdeals/collectors/amadeus.py) remains available — just create a free
Amadeus for Developers account and set api.amadeus.enabled: true. Both write
the same FareObservation shape, so detection and the dashboard don't care
which one is active.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

from .. import db
from ..config import Config
from ..models import CollectorResult, FareObservation
from .base import Collector

log = logging.getLogger(__name__)

SOURCE = "google_flights"
SEAT_TO_CABIN = {"economy": "ECONOMY", "business": "BUSINESS"}


class GoogleFlightsCollector(Collector):
    name = "google_flights"

    def enabled(self) -> bool:
        return self.cfg.google_flights.enabled

    def disabled_reason(self) -> str:
        return "disabled in config (api.google_flights.enabled: false)"

    def _rotated(self, destinations: list[str], origin: str) -> list[str]:
        """Never-checked destinations first, then least-recently-checked."""
        last_checked = db.last_offer_check(self.conn, origin, SOURCE)
        return sorted(destinations, key=lambda dest: last_checked.get(dest, ""))

    def _quote(
        self, origin: str, destination: str, seat: str, depart: str, ret: str, result: CollectorResult
    ) -> FareObservation | None:
        """Cheapest round-trip fare for one route+cabin+dates, via cache or a
        real (rate-limited) request. Any failure is recorded on `result` and
        returns None — callers never need to handle exceptions themselves."""
        cache_key = f"{SOURCE}:{origin}:{destination}:{seat}:{depart}:{ret}"
        cached = db.cache_get(self.conn, cache_key, self.cfg.google_flights.cache_ttl_minutes)
        if cached:
            payload = json.loads(cached)
        else:
            query = create_query(
                flights=[
                    FlightQuery(date=depart, from_airport=origin, to_airport=destination),
                    FlightQuery(date=ret, from_airport=destination, to_airport=origin),
                ],
                seat=seat,
                trip="round-trip",
                passengers=Passengers(adults=1),
                currency=self.cfg.currency,
                language="en-US",
            )
            result.api_calls += 1
            try:
                found = get_flights(query)
                prices = [f.price for f in found if isinstance(f.price, (int, float)) and f.price > 0]
                payload = {"price": min(prices) if prices else None, "url": query.url()}
            except FlightsNotFound:
                payload = {"price": None, "url": query.url()}
            except Exception as exc:
                result.errors.append(f"{SOURCE}:{origin}-{destination}:{seat}: {exc}")
                return None
            finally:
                time.sleep(self.cfg.google_flights.request_delay_seconds)
            db.cache_put(self.conn, cache_key, json.dumps(payload))

        if payload["price"] is None:
            return None
        return FareObservation(
            origin=origin,
            destination=destination,
            price=float(payload["price"]),
            currency=self.cfg.currency,
            source=SOURCE,
            departure_date=depart,
            return_date=ret,
            cabin=SEAT_TO_CABIN.get(seat, seat.upper()),
            deep_link=payload["url"],
        )

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        gcfg = self.cfg.google_flights
        today = date.today()
        depart = (today + timedelta(days=gcfg.departure_days_ahead)).isoformat()
        ret = (today + timedelta(days=gcfg.departure_days_ahead + gcfg.stay_days)).isoformat()
        seats = ("economy", "business") if gcfg.premium_cabin_check else ("economy",)

        for origin in self.cfg.origins:
            discovery = self._rotated(self.cfg.discovery_destinations, origin)
            for destination in discovery[: gcfg.max_discovery_calls_per_run]:
                obs = self._quote(origin, destination, "economy", depart, ret, result)
                if obs:
                    result.observations.append(obs)

            watchlist = self._rotated(self.cfg.watchlist, origin)
            for destination in watchlist[: gcfg.max_offer_calls_per_run]:
                for seat in seats:
                    obs = self._quote(origin, destination, seat, depart, ret, result)
                    if obs:
                        result.observations.append(obs)

            log.info(
                "google_flights: %s → %d fares (%d discovery + %d watchlist routes attempted)",
                origin, len(result.observations),
                min(len(discovery), gcfg.max_discovery_calls_per_run),
                min(len(watchlist), gcfg.max_offer_calls_per_run),
            )
        return result
