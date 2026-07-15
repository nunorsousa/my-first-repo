"""Configuration loading: config.yaml for tunables, .env for secrets.

Every field has a sensible default so a partial config.yaml still works.
Environment overrides: FLIGHTDEALS_CONFIG (config path), FLIGHTDEALS_DB (db path).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)

DEFAULT_KEYWORDS = ["porto", "opo", "portugal"]


@dataclass
class DetectionCfg:
    discount_threshold_pct: float = 40.0
    baseline_window_days: int = 90
    min_observations: int = 5
    zscore_threshold: float = 3.0
    decimal_error_ratio: float = 0.15
    premium_cabin_ratio: float = 1.4
    min_price_floor: float = 10.0


@dataclass
class AlertsCfg:
    cooldown_hours: float = 24.0
    realert_drop_pct: float = 5.0
    max_alerts_per_run: int = 15


@dataclass
class RssFeed:
    name: str
    url: str


@dataclass
class RssCfg:
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    feeds: list[RssFeed] = field(default_factory=list)
    max_age_days: int = 7
    request_timeout_seconds: int = 20


@dataclass
class AmadeusCfg:
    enabled: bool = True
    environment: str = "test"  # test | production
    max_inspiration_calls_per_run: int = 2
    max_offer_calls_per_run: int = 3
    premium_cabin_check: bool = True
    departure_window_days: int = 60
    trip_length_days: tuple[int, int] = (3, 14)
    offers_days_ahead: int = 45
    offers_stay_days: int = 7
    cache_ttl_minutes: int = 120


@dataclass
class KiwiCfg:
    enabled: bool = False
    max_calls_per_run: int = 2


@dataclass
class Config:
    origins: list[str] = field(default_factory=lambda: ["OPO"])
    currency: str = "EUR"
    watchlist: list[str] = field(default_factory=list)
    detection: DetectionCfg = field(default_factory=DetectionCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    rss: RssCfg = field(default_factory=RssCfg)
    amadeus: AmadeusCfg = field(default_factory=AmadeusCfg)
    kiwi: KiwiCfg = field(default_factory=KiwiCfg)
    check_every_hours: float = 3.0
    db_path: Path = Path("data/flightdeals.db")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _section(data: dict, *keys: str) -> dict:
    node = data
    for key in keys:
        node = node.get(key) or {}
        if not isinstance(node, dict):
            return {}
    return node


def _apply(target, data: dict) -> None:
    """Copy matching keys from a yaml dict onto a dataclass, keeping defaults."""
    for key, value in data.items():
        if value is None or not hasattr(target, key):
            continue
        setattr(target, key, value)


def _parse_watchlist(raw) -> list[str]:
    routes: list[str] = []
    for item in raw or []:
        if isinstance(item, str):
            routes.append(item.strip().upper())
        elif isinstance(item, dict) and item.get("destination"):
            routes.append(str(item["destination"]).strip().upper())
    return [r for r in routes if r]


def _parse_feeds(raw) -> list[RssFeed]:
    feeds: list[RssFeed] = []
    for item in raw or []:
        if isinstance(item, dict) and item.get("url"):
            feeds.append(RssFeed(name=str(item.get("name") or item["url"]), url=str(item["url"])))
        elif isinstance(item, str):
            feeds.append(RssFeed(name=item, url=item))
        else:
            log.warning("Skipping malformed RSS feed entry in config: %r", item)
    return feeds


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv()  # no-op if there is no .env; real env vars always win

    config_path = Path(path or env("FLIGHTDEALS_CONFIG") or "config.yaml")
    data: dict = {}
    if config_path.exists():
        with open(config_path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    else:
        log.warning("Config file %s not found — using built-in defaults", config_path)

    cfg = Config()
    if isinstance(data.get("origins"), list):
        cfg.origins = [str(o).strip().upper() for o in data["origins"] if str(o).strip()]
    if data.get("currency"):
        cfg.currency = str(data["currency"]).strip().upper()
    cfg.watchlist = _parse_watchlist(data.get("watchlist"))

    _apply(cfg.detection, _section(data, "detection"))
    _apply(cfg.alerts, _section(data, "alerts"))
    _apply(cfg.amadeus, _section(data, "api", "amadeus"))
    _apply(cfg.kiwi, _section(data, "api", "kiwi"))

    rss_data = _section(data, "rss")
    _apply(cfg.rss, {k: v for k, v in rss_data.items() if k not in ("feeds", "keywords")})
    if isinstance(rss_data.get("keywords"), list):
        cfg.rss.keywords = [str(k).strip().lower() for k in rss_data["keywords"] if str(k).strip()]
    cfg.rss.feeds = _parse_feeds(rss_data.get("feeds"))

    schedule = _section(data, "schedule")
    if schedule.get("check_every_hours"):
        cfg.check_every_hours = float(schedule["check_every_hours"])

    db_path = env("FLIGHTDEALS_DB") or _section(data, "database").get("path") or "data/flightdeals.db"
    cfg.db_path = Path(db_path)

    if isinstance(cfg.amadeus.trip_length_days, list):
        cfg.amadeus.trip_length_days = tuple(cfg.amadeus.trip_length_days[:2])
    if cfg.amadeus.environment not in ("test", "production"):
        log.warning("Unknown amadeus environment %r — falling back to 'test'", cfg.amadeus.environment)
        cfg.amadeus.environment = "test"

    return cfg
