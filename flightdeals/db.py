"""SQLite persistence: schema, inserts, baseline queries, cache, and run log.

All timestamps are stored as UTC ISO8601 strings ("2026-07-15T12:00:00Z"),
which sort correctly as text.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import BlogPost, Deal, FareObservation, utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS fare_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    departure_date TEXT,
    return_date TEXT,
    cabin TEXT NOT NULL DEFAULT 'ECONOMY',
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    deep_link TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_route_time
    ON fare_observations(origin, destination, cabin, observed_at);

CREATE TABLE IF NOT EXISTS blog_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed TEXT NOT NULL,
    guid TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    matched_keywords TEXT
);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    found_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT,
    destination TEXT,
    cabin TEXT,
    price REAL,
    currency TEXT,
    baseline_price REAL,
    discount_pct REAL,
    departure_date TEXT,
    return_date TEXT,
    source TEXT NOT NULL,
    url TEXT,
    reason TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    alerted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deals_dedupe ON deals(dedupe_key, found_at);
CREATE INDEX IF NOT EXISTS idx_deals_found ON deals(found_at);

CREATE TABLE IF NOT EXISTS api_cache (
    cache_key TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL,
    observations INTEGER NOT NULL DEFAULT 0,
    new_posts INTEGER NOT NULL DEFAULT 0,
    new_deals INTEGER NOT NULL DEFAULT 0,
    alerts_sent INTEGER NOT NULL DEFAULT 0,
    errors TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    return conn


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def since_iso(days: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return _iso(now - timedelta(days=days))


# --- fare observations -------------------------------------------------------

def insert_observations(conn: sqlite3.Connection, observations: list[FareObservation]) -> int:
    conn.executemany(
        """INSERT INTO fare_observations
           (observed_at, origin, destination, departure_date, return_date,
            cabin, price, currency, source, deep_link)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (o.observed_at, o.origin, o.destination, o.departure_date, o.return_date,
             o.cabin, o.price, o.currency, o.source, o.deep_link)
            for o in observations
        ],
    )
    return len(observations)


def route_prices(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    cabin: str,
    since: str,
) -> list[float]:
    rows = conn.execute(
        """SELECT price FROM fare_observations
           WHERE origin = ? AND destination = ? AND cabin = ? AND observed_at >= ?""",
        (origin, destination, cabin, since),
    ).fetchall()
    return [row["price"] for row in rows]


def last_offer_check(conn: sqlite3.Connection, origin: str, source: str) -> dict[str, str]:
    """destination -> most recent observed_at for a source; drives watchlist rotation."""
    rows = conn.execute(
        """SELECT destination, MAX(observed_at) AS last_seen
           FROM fare_observations WHERE origin = ? AND source = ?
           GROUP BY destination""",
        (origin, source),
    ).fetchall()
    return {row["destination"]: row["last_seen"] for row in rows}


def tracked_routes(conn: sqlite3.Connection, days: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT origin, destination,
                  COUNT(*) AS n_obs,
                  ROUND(AVG(price), 2) AS avg_price,
                  MIN(price) AS min_price,
                  (SELECT f2.price FROM fare_observations f2
                    WHERE f2.origin = f1.origin AND f2.destination = f1.destination
                      AND f2.cabin = 'ECONOMY'
                    ORDER BY f2.observed_at DESC LIMIT 1) AS last_price,
                  MAX(observed_at) AS last_seen,
                  MAX(currency) AS currency
           FROM fare_observations f1
           WHERE cabin = 'ECONOMY' AND observed_at >= ?
           GROUP BY origin, destination
           ORDER BY origin, destination""",
        (since_iso(days),),
    ).fetchall()


def daily_price_series(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    days: int = 30,
    cabin: str = "ECONOMY",
) -> list[tuple[str, float]]:
    rows = conn.execute(
        """SELECT substr(observed_at, 1, 10) AS day, MIN(price) AS price
           FROM fare_observations
           WHERE origin = ? AND destination = ? AND cabin = ? AND observed_at >= ?
           GROUP BY day ORDER BY day""",
        (origin, destination, cabin, since_iso(days)),
    ).fetchall()
    return [(row["day"], row["price"]) for row in rows]


# --- blog posts ---------------------------------------------------------------

def insert_blog_post(conn: sqlite3.Connection, post: BlogPost) -> bool:
    """Insert if unseen. Returns True when the post is new (drives alerting)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO blog_posts
           (feed, guid, title, url, summary, published_at, fetched_at, matched_keywords)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (post.feed, post.guid, post.title, post.url, post.summary,
         post.published_at, utcnow_iso(), json.dumps(post.matched_keywords)),
    )
    return cur.rowcount == 1


# --- deals ---------------------------------------------------------------------

def insert_deal(conn: sqlite3.Connection, deal: Deal) -> int:
    cur = conn.execute(
        """INSERT INTO deals
           (found_at, kind, origin, destination, cabin, price, currency,
            baseline_price, discount_pct, departure_date, return_date,
            source, url, reason, dedupe_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (deal.found_at, deal.kind, deal.origin, deal.destination, deal.cabin,
         deal.price, deal.currency, deal.baseline_price, deal.discount_pct,
         deal.departure_date, deal.return_date, deal.source, deal.url,
         deal.reason, deal.dedupe_key),
    )
    return cur.lastrowid


def mark_deal_alerted(conn: sqlite3.Connection, deal_id: int) -> None:
    conn.execute("UPDATE deals SET alerted_at = ? WHERE id = ?", (utcnow_iso(), deal_id))


def last_deal_for_key(conn: sqlite3.Connection, dedupe_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM deals WHERE dedupe_key = ? ORDER BY found_at DESC LIMIT 1",
        (dedupe_key,),
    ).fetchone()


def recent_deals(conn: sqlite3.Connection, days: int = 30, kind: str | None = None) -> list[sqlite3.Row]:
    query = "SELECT * FROM deals WHERE found_at >= ?"
    params: list = [since_iso(days)]
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY found_at DESC"
    return conn.execute(query, params).fetchall()


# --- API response cache ---------------------------------------------------------

def cache_get(conn: sqlite3.Connection, key: str, ttl_minutes: float) -> str | None:
    row = conn.execute(
        "SELECT fetched_at, payload FROM api_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    age = datetime.now(timezone.utc) - parse_iso(row["fetched_at"])
    if age > timedelta(minutes=ttl_minutes):
        return None
    return row["payload"]


def cache_put(conn: sqlite3.Connection, key: str, payload: str) -> None:
    conn.execute(
        """INSERT INTO api_cache (cache_key, fetched_at, payload) VALUES (?, ?, ?)
           ON CONFLICT(cache_key) DO UPDATE SET fetched_at = excluded.fetched_at,
                                                payload = excluded.payload""",
        (key, utcnow_iso(), payload),
    )


def cache_prune(conn: sqlite3.Connection, older_than_days: float = 7) -> None:
    conn.execute("DELETE FROM api_cache WHERE fetched_at < ?", (since_iso(older_than_days),))


# --- run log ---------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, trigger: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (utcnow_iso(), trigger)
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    observations: int,
    new_posts: int,
    new_deals: int,
    alerts_sent: int,
    errors: list[str],
) -> None:
    conn.execute(
        """UPDATE runs SET finished_at = ?, observations = ?, new_posts = ?,
                           new_deals = ?, alerts_sent = ?, errors = ?
           WHERE id = ?""",
        (utcnow_iso(), observations, new_posts, new_deals, alerts_sent,
         json.dumps(errors) if errors else None, run_id),
    )


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def counts(conn: sqlite3.Connection) -> dict:
    return {
        "observations": conn.execute("SELECT COUNT(*) c FROM fare_observations").fetchone()["c"],
        "routes": conn.execute(
            "SELECT COUNT(DISTINCT origin || '-' || destination) c FROM fare_observations"
        ).fetchone()["c"],
        "deals": conn.execute("SELECT COUNT(*) c FROM deals").fetchone()["c"],
        "blog_posts": conn.execute("SELECT COUNT(*) c FROM blog_posts").fetchone()["c"],
    }
