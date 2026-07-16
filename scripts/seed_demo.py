"""Seed a demo database so the dashboard can be previewed with realistic data.

Usage:
    python scripts/seed_demo.py            # writes data/demo.db
    FLIGHTDEALS_DB=data/demo.db python -m flightdeals.dashboard

The demo DB is gitignored; delete it whenever you like.
"""

from __future__ import annotations

import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flightdeals import db  # noqa: E402
from flightdeals.models import BlogPost, Deal, FareObservation  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "demo.db"

ROUTES = {
    "JFK": 520, "GRU": 610, "BKK": 640, "LHR": 95, "MAD": 60,
    "CDG": 110, "FCO": 130, "DXB": 430,
}


def main() -> None:
    random.seed(7)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = db.connect(DB_PATH)
    now = datetime.now(timezone.utc)

    observations = []
    for destination, base in ROUTES.items():
        for day in range(30, -1, -1):
            wobble = 1 + 0.10 * math.sin(day / 4.5) + random.uniform(-0.06, 0.06)
            price = round(base * wobble, 2)
            observations.append(
                FareObservation(
                    origin="OPO", destination=destination, price=price, currency="EUR",
                    source="google_flights",
                    observed_at=(now - timedelta(days=day, hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            )
    # a visible dip on JFK in the last few days
    for day in (2, 1, 0):
        observations.append(
            FareObservation(
                origin="OPO", destination="JFK", price=289, currency="EUR",
                source="google_flights", departure_date="2026-09-10", return_date="2026-09-17",
                observed_at=(now - timedelta(days=day, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
    db.insert_observations(conn, observations)

    deals = [
        Deal(kind="price_drop", origin="OPO", destination="JFK", cabin="ECONOMY",
             price=289, currency="EUR", baseline_price=524, discount_pct=44.8,
             departure_date="2026-09-10", return_date="2026-09-17",
             source="google_flights", reason="45% below the 90-day average €524 (58 observations)",
             url="https://www.google.com/travel/flights/search?tfs=GhoSCjIwMjYtMDktMTBqBRIDT1BPcgUSA0pGSxoaEgoyMDI2LTA5LTE3agUSA0pGS3IFEgNPUE9CAQFIAZgBAQ%3D%3D&hl=en-US&curr=EUR",
             dedupe_key="price_drop:OPO:JFK:ECONOMY",
             found_at=(now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        Deal(kind="error_fare", origin="OPO", destination="BKK", cabin="BUSINESS",
             price=712, currency="EUR", baseline_price=645, discount_pct=None,
             departure_date="2026-10-02", return_date="2026-10-12",
             source="google_flights",
             reason="Business fare €712 is only 1.10× the economy reference €645 — premium-cabin mispricing",
             dedupe_key="error_fare:OPO:BKK:BUSINESS",
             found_at=(now - timedelta(days=1, hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        Deal(kind="error_fare", origin="OPO", destination="DXB", cabin="ECONOMY",
             price=58, currency="EUR", baseline_price=432, discount_pct=86.6,
             source="google_flights",
             reason="Price €58 is ≤15% of the 90-day average €432 — possible decimal/currency error; "
                    "87% below the 90-day average €432 (31 observations)",
             dedupe_key="error_fare:OPO:DXB:ECONOMY",
             found_at=(now - timedelta(days=2, hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        Deal(kind="blog", source="Secret Flying",
             reason="“Porto, Portugal to Rio de Janeiro, Brazil from only €333 roundtrip” (matched: porto, portugal)",
             url="https://www.secretflying.com/", dedupe_key="blog:demo-1",
             found_at=(now - timedelta(days=3, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        Deal(kind="blog", source="Fly4Free",
             reason="“Cheap flights from Porto to the Azores from €38” (matched: porto)",
             url="https://www.fly4free.com/", dedupe_key="blog:demo-2",
             found_at=(now - timedelta(days=6, hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ]
    for deal in deals:
        deal_id = db.insert_deal(conn, deal)
        if deal.kind != "blog":
            db.mark_deal_alerted(conn, deal_id)

    db.insert_blog_post(conn, BlogPost(
        feed="Secret Flying", guid="demo-1",
        title="Porto, Portugal to Rio de Janeiro, Brazil from only €333 roundtrip",
        url="https://www.secretflying.com/", summary="", published_at=None,
        matched_keywords=["porto", "portugal"]))
    db.insert_blog_post(conn, BlogPost(
        feed="Fly4Free", guid="demo-2",
        title="Cheap flights from Porto to the Azores from €38",
        url="https://www.fly4free.com/", summary="", published_at=None,
        matched_keywords=["porto"]))

    run_id = db.start_run(conn, "cron")
    db.finish_run(conn, run_id, observations=len(observations), new_posts=2,
                  new_deals=len(deals), alerts_sent=3, errors=[])
    conn.close()
    print(f"Demo database written to {DB_PATH}")
    print(f"Preview:  FLIGHTDEALS_DB={DB_PATH.relative_to(Path.cwd()) if DB_PATH.is_relative_to(Path.cwd()) else DB_PATH} python -m flightdeals.dashboard")


if __name__ == "__main__":
    main()
