"""Tests for RSS keyword matching and post-age filtering."""

from __future__ import annotations

import feedparser

from flightdeals.collectors.rss import _entry_published_iso, _entry_text, is_recent, match_keywords

KEYWORDS = ["porto", "opo", "portugal"]


def test_basic_matches():
    assert match_keywords("Error fare: Porto to New York from €180", KEYWORDS) == ["porto"]
    assert match_keywords("PORTUGAL flash sale", KEYWORDS) == ["portugal"]
    assert match_keywords("Nonstop OPO-JFK for £150", KEYWORDS) == ["opo"]
    assert match_keywords("Porto, Portugal in business class", KEYWORDS) == ["porto", "portugal"]


def test_whole_word_only():
    # Substring hits must NOT match: these are different places/words.
    assert match_keywords("Portofino luxury weekend", KEYWORDS) == []
    assert match_keywords("Opoczno city guide", KEYWORDS) == []
    assert match_keywords("New York airport transit tips", KEYWORDS) == []


def test_punctuation_boundaries_match():
    assert match_keywords("Deal alert! porto: €99", KEYWORDS) == ["porto"]
    assert match_keywords("(OPO) departures", KEYWORDS) == ["opo"]


def test_age_filter():
    now_ish = "2026-07-14T10:00:00Z"
    assert is_recent(now_ish, max_age_days=7) is True
    assert is_recent("2026-05-01T10:00:00Z", max_age_days=7) is False
    assert is_recent(None, max_age_days=7) is True  # undated posts are kept
    assert is_recent("not-a-date", max_age_days=7) is True


def test_feed_entry_text_includes_categories():
    raw = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><guid>g1</guid><title>Cheap flights to sunshine</title>
    <link>http://x/1</link><description>Great deal</description>
    <pubDate>Tue, 14 Jul 2026 10:00:00 GMT</pubDate>
    <category>Porto</category><category>Error fares</category></item></channel></rss>"""
    entry = feedparser.parse(raw).entries[0]
    text = _entry_text(entry)
    # The title alone wouldn't match — the category tag must be searched too.
    assert match_keywords(text, KEYWORDS) == ["porto"]
    assert _entry_published_iso(entry) == "2026-07-14T10:00:00Z"
