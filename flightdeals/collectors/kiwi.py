"""Kiwi.com Tequila collector — legacy/optional.

Tequila closed to new sign-ups in 2023, so this collector ships disabled
(api.kiwi.enabled: false) and is untested against the live API. It is kept
as a working template: if you hold a legacy key, set KIWI_API_KEY in .env
and enable it in config.yaml. It follows the documented /v2/search shape
(cheapest-anywhere from each origin) and degrades to error strings if the
API answers differently.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import requests

from ..config import Config
from ..models import CollectorResult, FareObservation
from .base import Collector

log = logging.getLogger(__name__)

API_URL = "https://api.tequila.kiwi.com/v2/search"
REQUEST_TIMEOUT = 30


class KiwiCollector(Collector):
    name = "kiwi"

    def __init__(self, cfg: Config, conn):
        super().__init__(cfg, conn)
        from ..config import env

        self.api_key = env("KIWI_API_KEY")

    def enabled(self) -> bool:
        return self.cfg.kiwi.enabled and bool(self.api_key)

    def disabled_reason(self) -> str:
        if not self.cfg.kiwi.enabled:
            return "disabled in config (Tequila closed to new sign-ups; enable only with a legacy key)"
        return "KIWI_API_KEY not set"

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        today = date.today()
        date_from = (today + timedelta(days=7)).strftime("%d/%m/%Y")
        date_to = (today + timedelta(days=self.cfg.kiwi.departure_window_days)).strftime("%d/%m/%Y")

        for origin in self.cfg.origins[: self.cfg.kiwi.max_calls_per_run]:
            params = {
                "fly_from": origin,
                "date_from": date_from,
                "date_to": date_to,
                "nights_in_dst_from": 3,
                "nights_in_dst_to": 14,
                "curr": self.cfg.currency,
                "one_for_city": 1,
                "sort": "price",
                "limit": 100,
            }
            try:
                response = requests.get(
                    API_URL, params=params, headers={"apikey": self.api_key}, timeout=REQUEST_TIMEOUT
                )
                result.api_calls += 1
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                result.errors.append(f"kiwi:{origin}: {exc}")
                continue

            for row in payload.get("data") or []:
                try:
                    result.observations.append(
                        FareObservation(
                            origin=origin,
                            destination=row["flyTo"],
                            price=float(row["price"]),
                            currency=self.cfg.currency,
                            source="kiwi",
                            departure_date=(row.get("local_departure") or "")[:10] or None,
                            cabin="ECONOMY",
                            deep_link=row.get("deep_link"),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            log.info("kiwi: %s → %d fares", origin, len(result.observations))
        return result
