"""Run orchestrator: collect → detect → persist → alert.

CLI:
    python -m flightdeals.run                 # one check
    python -m flightdeals.run --dry-run       # no Telegram sends, alerts printed
    python -m flightdeals.run --loop          # keep running every check_every_hours
    python -m flightdeals.run --trigger cron  # label the run in the run log
    python -m flightdeals.run --db path.db    # override database location
"""

from __future__ import annotations

import argparse
import logging
import time

from . import db
from .alerts import alert_deals
from .collectors import build_collectors
from .config import load_config
from .detect import detect_deals
from .models import BlogPost, FareObservation

log = logging.getLogger(__name__)


def run_once(
    config_path: str | None = None,
    trigger: str = "manual",
    dry_run: bool = False,
    db_path: str | None = None,
) -> dict:
    """One full check. Returns a summary dict (also used by the dashboard)."""
    cfg = load_config(config_path)
    conn = db.connect(db_path or cfg.db_path)
    run_id = db.start_run(conn, trigger)
    errors: list[str] = []
    observations: list[FareObservation] = []
    matched_posts: list[BlogPost] = []
    api_calls = 0

    try:
        collectors = build_collectors(cfg, conn)
        log.info("run #%d (%s): collectors: %s", run_id, trigger, [c.name for c in collectors] or "none")
        for collector in collectors:
            try:
                result = collector.collect()
                observations.extend(result.observations)
                matched_posts.extend(result.posts)
                errors.extend(result.errors)
                api_calls += result.api_calls
            except Exception as exc:  # a broken collector must not sink the run
                log.exception("collector %s crashed", collector.name)
                errors.append(f"{collector.name}: crashed: {exc}")

        # Keep junk out of the baseline entirely.
        floor = cfg.detection.min_price_floor
        observations = [o for o in observations if o.price >= floor]

        # Novelty filter for blog posts (INSERT OR IGNORE on guid).
        new_posts = [p for p in matched_posts if db.insert_blog_post(conn, p)]

        # Detect BEFORE inserting observations so a weird fare can't
        # water down its own baseline.
        deals = detect_deals(conn, observations, new_posts, cfg)
        db.insert_observations(conn, observations)
        stored = [(db.insert_deal(conn, deal), deal) for deal in deals]

        sent, alert_errors = alert_deals(
            conn, stored, max_alerts=cfg.alerts.max_alerts_per_run, dry_run=dry_run
        )
        errors.extend(alert_errors)
        db.cache_prune(conn)
        db.finish_run(conn, run_id, len(observations), len(new_posts), len(deals), sent, errors)

        summary = {
            "run_id": run_id,
            "trigger": trigger,
            "observations": len(observations),
            "new_posts": len(new_posts),
            "new_deals": len(deals),
            "alerts_sent": sent,
            "api_calls": api_calls,
            "errors": errors,
        }
        log.info(
            "run #%d done: %d observations, %d new posts, %d deals, %d alerts, %d API calls, %d errors",
            run_id, len(observations), len(new_posts), len(deals), sent, api_calls, len(errors),
        )
        for error in errors:
            log.warning("  error: %s", error)
        return summary
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check for flight deals from your configured origins")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--db", default=None, help="override database path")
    parser.add_argument("--trigger", default="manual", choices=["manual", "cron", "dashboard", "loop"])
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    parser.add_argument("--loop", action="store_true", help="run forever on schedule.check_every_hours")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    if not args.loop:
        run_once(args.config, args.trigger, args.dry_run, args.db)
        return

    cfg = load_config(args.config)
    interval = max(cfg.check_every_hours, 0.1) * 3600
    log.info("loop mode: checking every %.1f hours (Ctrl-C to stop)", interval / 3600)
    while True:
        try:
            run_once(args.config, "loop", args.dry_run, args.db)
        except Exception:
            log.exception("run failed; will retry next cycle")
        time.sleep(interval)


if __name__ == "__main__":
    main()
