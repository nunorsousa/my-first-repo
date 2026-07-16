"""Dashboard API: a small FastAPI app serving the single-page UI and JSON.

Run locally:  python -m flightdeals.dashboard  (then open http://127.0.0.1:8000)
"""

from __future__ import annotations

import asyncio

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from .. import db
from ..alerts import telegram_configured
from ..config import env, load_config
from ..run import run_once

app = FastAPI(title="Flight Deals Dashboard")
STATIC_DIR = Path(__file__).parent / "static"

_check_lock = asyncio.Lock()


def _open():
    cfg = load_config()
    return cfg, db.connect(cfg.db_path)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/summary")
def summary() -> dict:
    cfg, conn = _open()
    try:
        last = db.last_run(conn)
        return {
            "origins": cfg.origins,
            "watchlist": cfg.watchlist,
            "currency": cfg.currency,
            "threshold_pct": cfg.detection.discount_threshold_pct,
            "counts": db.counts(conn),
            "last_run": dict(last) if last else None,
            "sources": {
                "google_flights": cfg.google_flights.enabled,
                "amadeus": cfg.amadeus.enabled and bool(env("AMADEUS_CLIENT_ID")),
                "kiwi": cfg.kiwi.enabled and bool(env("KIWI_API_KEY")),
                "rss_feeds": len(cfg.rss.feeds),
                "telegram": telegram_configured(),
            },
        }
    finally:
        conn.close()


@app.get("/api/routes")
def routes(days: int = 30) -> list[dict]:
    _, conn = _open()
    try:
        payload = []
        for row in db.tracked_routes(conn, days):
            series = db.daily_price_series(conn, row["origin"], row["destination"], days)
            route = dict(row)
            route["series"] = [{"day": day, "price": price} for day, price in series]
            payload.append(route)
        return payload
    finally:
        conn.close()


@app.get("/api/deals")
def deals(days: int = 60, kind: str | None = None) -> list[dict]:
    _, conn = _open()
    try:
        return [dict(row) for row in db.recent_deals(conn, days, kind or None)]
    finally:
        conn.close()


@app.post("/api/check")
async def check():
    """Manual 'run a fresh check now' trigger from the UI."""
    if _check_lock.locked():
        return JSONResponse({"error": "a check is already running"}, status_code=409)
    async with _check_lock:
        summary = await asyncio.to_thread(run_once, None, "dashboard", False)
    return summary
