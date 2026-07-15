"""Base interface every data source implements.

To add a new source (another price API, a different feed type):
subclass Collector, implement enabled() / disabled_reason() / collect(),
and register an instance in collectors/__init__.py:build_collectors.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod

from ..config import Config
from ..models import CollectorResult


class Collector(ABC):
    name: str = "base"

    def __init__(self, cfg: Config, conn: sqlite3.Connection):
        self.cfg = cfg
        self.conn = conn

    @abstractmethod
    def enabled(self) -> bool:
        """Whether this collector should run (config flag + credentials present)."""

    def disabled_reason(self) -> str:
        return "disabled"

    @abstractmethod
    def collect(self) -> CollectorResult:
        """Fetch data. Must not raise for per-item failures — report them
        in CollectorResult.errors so one bad source can't sink the run."""
