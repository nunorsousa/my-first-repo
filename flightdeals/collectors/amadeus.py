"""Amadeus Self-Service API collector.

Two endpoints, both counted against the free monthly quota:
- Flight Inspiration Search (v1/shopping/flight-destinations): broad
  "cheapest anywhere from origin" discovery — one call per origin per run.
- Flight Offers Search (v2/shopping/flight-offers): concrete quotes for
  watchlist routes, rotating least-recently-checked first; optionally a
  second business-class call per route to feed the premium-cabin
  error-fare heuristic.

Identical requests within api.amadeus.cache_ttl_minutes are served from
the local api_cache table instead of hitting the network.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from datetime import date, timedelta

import requests

from .. import db
from ..config import Config
from ..models import CollectorResult, FareObservation
from .base import Collector

log = logging.getLogger(__name__)

HOSTS = {
    "test": "https://test.api.amadeus.com",
    "production": "https://api.amadeus.com",
}
REQUEST_TIMEOUT = 30
OFFERS_SOURCE = "amadeus_offers"
INSPIRATION_SOURCE = "amadeus_inspiration"


class AmadeusError(Exception):
    pass


def google_flights_link(origin: str, destination: str, depart: str | None, ret: str | None) -> str:
    """Human-checkable search link (Amadeus deep links are API URLs, not pages)."""
    query = f"Flights from {origin} to {destination}"
    if depart:
        query += f" on {depart}"
    if ret:
        query += f" through {ret}"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(query)


class AmadeusCollector(Collector):
    name = "amadeus"

    def __init__(self, cfg: Config, conn):
        super().__init__(cfg, conn)
        from ..config import env  # late import keeps module import side-effect free

        self.client_id = env("AMADEUS_CLIENT_ID")
        self.client_secret = env("AMADEUS_CLIENT_SECRET")
        self.host = HOSTS[cfg.amadeus.environment]
        self.session = requests.Session()
        self._token: str | None = None

    def enabled(self) -> bool:
        return self.cfg.amadeus.enabled and bool(self.client_id and self.client_secret)

    def disabled_reason(self) -> str:
        if not self.cfg.amadeus.enabled:
            return "disabled in config (api.amadeus.enabled: false)"
        return "AMADEUS_CLIENT_ID / AMADEUS_CLIENT_SECRET not set"

    # --- auth -----------------------------------------------------------------

    def _get_token(self) -> str:
        if self._token:
            return self._token
        cache_key = f"amadeus:token:{self.cfg.amadeus.environment}"
        cached = db.cache_get(self.conn, cache_key, ttl_minutes=25)
        if cached:
            self._token = cached
            return cached
        response = self.session.post(
            f"{self.host}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            raise AmadeusError(f"token request failed: HTTP {response.status_code} {response.text[:200]}")
        self._token = response.json()["access_token"]
        db.cache_put(self.conn, cache_key, self._token)
        return self._token

    def _clear_token(self) -> None:
        self._token = None
        db.cache_put(self.conn, f"amadeus:token:{self.cfg.amadeus.environment}", "")

    # --- HTTP with cache --------------------------------------------------------

    def _get_json(self, path: str, params: dict, result: CollectorResult) -> dict:
        """GET with local response cache, one 401 token refresh, one 429 retry.
        Raises AmadeusError for anything unrecoverable; a 404 means 'no data'
        and returns an empty payload."""
        cache_key = "amadeus:" + path + ":" + json.dumps(params, sort_keys=True)
        cached = db.cache_get(self.conn, cache_key, self.cfg.amadeus.cache_ttl_minutes)
        if cached:
            log.info("amadeus: cache hit for %s", path)
            return json.loads(cached)

        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            response = self.session.get(
                f"{self.host}{path}", params=params, headers=headers, timeout=REQUEST_TIMEOUT
            )
            result.api_calls += 1
            if response.status_code == 200:
                db.cache_put(self.conn, cache_key, response.text)
                return response.json()
            if response.status_code == 404:
                return {"data": []}
            if response.status_code == 401 and attempt == 1:
                self._clear_token()
                continue
            if response.status_code == 429 and attempt == 1:
                log.warning("amadeus: rate limited, backing off 2s")
                time.sleep(2)
                continue
            raise AmadeusError(f"{path}: HTTP {response.status_code} {response.text[:300]}")
        raise AmadeusError(f"{path}: retries exhausted")

    # --- response parsing (pure, unit-tested) ------------------------------------

    @staticmethod
    def parse_inspiration(payload: dict, origin: str, fallback_currency: str) -> list[FareObservation]:
        currency = (payload.get("meta") or {}).get("currency") or fallback_currency
        observations = []
        for row in payload.get("data") or []:
            try:
                price = float(row["price"]["total"])
            except (KeyError, TypeError, ValueError):
                continue
            destination = row.get("destination")
            if not destination:
                continue
            depart = row.get("departureDate")
            ret = row.get("returnDate")
            observations.append(
                FareObservation(
                    origin=row.get("origin") or origin,
                    destination=destination,
                    price=price,
                    currency=currency,
                    source=INSPIRATION_SOURCE,
                    departure_date=depart,
                    return_date=ret,
                    cabin="ECONOMY",
                    deep_link=google_flights_link(origin, destination, depart, ret),
                )
            )
        return observations

    @staticmethod
    def parse_offers(payload: dict, origin: str, destination: str, cabin: str) -> FareObservation | None:
        """Cheapest offer in the payload, or None when the route returned nothing."""
        best_price: float | None = None
        best_currency = "EUR"
        depart = ret = None
        for offer in payload.get("data") or []:
            try:
                price = float(offer["price"]["grandTotal"])
            except (KeyError, TypeError, ValueError):
                continue
            if best_price is not None and price >= best_price:
                continue
            best_price = price
            best_currency = offer.get("price", {}).get("currency", best_currency)
            itineraries = offer.get("itineraries") or []
            try:
                depart = itineraries[0]["segments"][0]["departure"]["at"][:10]
            except (IndexError, KeyError, TypeError):
                depart = None
            try:
                ret = itineraries[1]["segments"][0]["departure"]["at"][:10]
            except (IndexError, KeyError, TypeError):
                ret = None
        if best_price is None:
            return None
        return FareObservation(
            origin=origin,
            destination=destination,
            price=best_price,
            currency=best_currency,
            source=OFFERS_SOURCE,
            departure_date=depart,
            return_date=ret,
            cabin=cabin,
            deep_link=google_flights_link(origin, destination, depart, ret),
        )

    # --- collection ----------------------------------------------------------------

    def _rotated_watchlist(self, origin: str) -> list[str]:
        """Watchlist ordered never-checked first, then least recently checked."""
        last_checked = db.last_offer_check(self.conn, origin, OFFERS_SOURCE)
        return sorted(self.cfg.watchlist, key=lambda dest: last_checked.get(dest, ""))

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        today = date.today()
        acfg = self.cfg.amadeus

        # Broad discovery: one Flight Inspiration Search per origin.
        for origin in self.cfg.origins[: acfg.max_inspiration_calls_per_run]:
            window_start = today + timedelta(days=7)
            window_end = today + timedelta(days=acfg.departure_window_days)
            min_stay, max_stay = acfg.trip_length_days
            params = {
                "origin": origin,
                "departureDate": f"{window_start},{window_end}",
                "duration": f"{min_stay},{max_stay}",
                "viewBy": "DESTINATION",
            }
            try:
                payload = self._get_json("/v1/shopping/flight-destinations", params, result)
                found = self.parse_inspiration(payload, origin, self.cfg.currency)
                result.observations.extend(found)
                log.info("amadeus: inspiration %s → %d destinations", origin, len(found))
            except (AmadeusError, requests.RequestException) as exc:
                result.errors.append(f"amadeus:inspiration:{origin}: {exc}")

        # Watchlist quotes: rotate through the list, cheapest offer per route.
        depart = (today + timedelta(days=acfg.offers_days_ahead)).isoformat()
        ret = (today + timedelta(days=acfg.offers_days_ahead + acfg.offers_stay_days)).isoformat()
        cabins = ["ECONOMY"] + (["BUSINESS"] if acfg.premium_cabin_check else [])
        for origin in self.cfg.origins:
            for destination in self._rotated_watchlist(origin)[: acfg.max_offer_calls_per_run]:
                for cabin in cabins:
                    params = {
                        "originLocationCode": origin,
                        "destinationLocationCode": destination,
                        "departureDate": depart,
                        "returnDate": ret,
                        "adults": 1,
                        "travelClass": cabin,
                        "currencyCode": self.cfg.currency,
                        "max": 5,
                    }
                    try:
                        payload = self._get_json("/v2/shopping/flight-offers", params, result)
                        observation = self.parse_offers(payload, origin, destination, cabin)
                        if observation:
                            result.observations.append(observation)
                        else:
                            log.info("amadeus: no %s offers for %s-%s", cabin, origin, destination)
                    except (AmadeusError, requests.RequestException) as exc:
                        result.errors.append(f"amadeus:offers:{origin}-{destination}:{cabin}: {exc}")
                    time.sleep(0.3)  # stay far under the sandbox 10 tx/s cap
        return result
