"""Collector registry: builds the set of enabled data sources for a run."""

from __future__ import annotations

import logging
import sqlite3

from ..config import Config
from .amadeus import AmadeusCollector
from .base import Collector
from .google_flights import GoogleFlightsCollector
from .kiwi import KiwiCollector
from .rss import RssCollector

log = logging.getLogger(__name__)


def build_collectors(cfg: Config, conn: sqlite3.Connection) -> list[Collector]:
    collectors: list[Collector] = []
    for collector in (
        GoogleFlightsCollector(cfg, conn),
        AmadeusCollector(cfg, conn),
        KiwiCollector(cfg, conn),
        RssCollector(cfg, conn),
    ):
        if collector.enabled():
            collectors.append(collector)
        else:
            log.info("Collector %r skipped: %s", collector.name, collector.disabled_reason())
    return collectors
