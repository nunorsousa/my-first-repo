"""RSS collector: deal-blog feeds filtered for Porto/OPO/Portugal keywords.

Feeds only — no page scraping. Each feed is fetched once per run with a
polite User-Agent and a timeout; a failing feed becomes an error string,
never an exception.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from ..models import BlogPost, CollectorResult
from .base import Collector

log = logging.getLogger(__name__)

USER_AGENT = "flightdeals/0.1 (personal flight-deal tracker; RSS only)"


def match_keywords(text: str, keywords: list[str]) -> list[str]:
    """Case-insensitive whole-word matches, so 'porto' hits 'Porto!' but not
    'Portofino', and 'opo' hits 'OPO-JFK' but not 'Opoczno'."""
    found = []
    for keyword in keywords:
        if re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE):
            found.append(keyword)
    return found


def is_recent(published_at: str | None, max_age_days: int, now: datetime | None = None) -> bool:
    """Posts with no parseable date are kept (better a rare duplicate-looking
    alert than a missed deal); dated posts must be within the age window."""
    if not published_at:
        return True
    now = now or datetime.now(timezone.utc)
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    return now - published <= timedelta(days=max_age_days)


def _entry_published_iso(entry) -> str | None:
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            timestamp = calendar.timegm(parsed)  # struct_time is UTC in feedparser
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def _entry_text(entry) -> str:
    tags = " ".join(t.get("term", "") for t in entry.get("tags", []) if isinstance(t, dict))
    return f"{entry.get('title', '')} {entry.get('summary', '')} {tags}"


class RssCollector(Collector):
    name = "rss"

    def enabled(self) -> bool:
        return bool(self.cfg.rss.feeds)

    def disabled_reason(self) -> str:
        return "no RSS feeds configured"

    def collect(self) -> CollectorResult:
        result = CollectorResult()
        for feed in self.cfg.rss.feeds:
            try:
                response = requests.get(
                    feed.url,
                    timeout=self.cfg.rss.request_timeout_seconds,
                    headers={"User-Agent": USER_AGENT},
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                result.errors.append(f"rss:{feed.name}: fetch failed: {exc}")
                continue

            parsed = feedparser.parse(response.content)
            if not parsed.entries:
                detail = getattr(parsed, "bozo_exception", "no entries")
                result.errors.append(f"rss:{feed.name}: unparseable or empty feed ({detail})")
                continue

            matched = 0
            for entry in parsed.entries:
                keywords = match_keywords(_entry_text(entry), self.cfg.rss.keywords)
                if not keywords:
                    continue
                published_at = _entry_published_iso(entry)
                if not is_recent(published_at, self.cfg.rss.max_age_days):
                    continue
                guid = entry.get("id") or entry.get("link") or f"{feed.name}:{entry.get('title', '')}"
                result.posts.append(
                    BlogPost(
                        feed=feed.name,
                        guid=guid,
                        title=(entry.get("title") or "(untitled)").strip(),
                        url=entry.get("link"),
                        summary=re.sub(r"<[^>]+>", " ", entry.get("summary", ""))[:1000].strip(),
                        published_at=published_at,
                        matched_keywords=keywords,
                    )
                )
                matched += 1
            log.info("rss:%s — %d entries, %d matched", feed.name, len(parsed.entries), matched)
        return result
