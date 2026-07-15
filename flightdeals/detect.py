"""Deal detection: score fare observations against per-route baselines.

Rules (all thresholds come from config.detection):

price_drop  — fare is >= discount_threshold_pct below the trailing
              baseline_window_days average for the route+cabin. Needs at
              least min_observations history rows, so the first days of
              data collection can't false-positive.

error_fare  — any of:
              * decimal/currency error: price <= decimal_error_ratio ×
                baseline average (e.g. 80 € vs an 800 € average);
              * statistical outlier: price >= zscore_threshold std-devs
                below the baseline average (catches anomalies on routes
                with tight price variance that never reach the % threshold);
              * premium-cabin mispricing: BUSINESS/FIRST fare <=
                premium_cabin_ratio × the economy reference price. The
                reference is the economy baseline when it exists, else an
                economy fare for the same route seen in the same run — so
                this heuristic works from the very first run.

blog        — every *new* matching RSS post becomes a deal, no price
              scoring involved.

Deals are deduped: the same dedupe_key within alerts.cooldown_hours is
suppressed unless the price fell a further realert_drop_pct.

Ordering matters for correctness: the runner calls detect_deals() BEFORE
inserting the run's observations, so an anomalous fare can't drag its own
baseline down and mask itself.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db
from .config import Config
from .models import PREMIUM_CABINS, BlogPost, Deal, FareObservation

log = logging.getLogger(__name__)

# Below this, a std-dev is treated as "prices are essentially constant" and
# the z-score rule is skipped — dividing by a near-zero sigma would flag
# trivial fluctuations on flat routes.
MIN_MEANINGFUL_STDDEV = 1.0


@dataclass
class RouteStats:
    count: int
    avg: float | None
    stddev: float
    minimum: float | None
    maximum: float | None


def compute_stats(prices: list[float]) -> RouteStats:
    if not prices:
        return RouteStats(0, None, 0.0, None, None)
    return RouteStats(
        count=len(prices),
        avg=statistics.fmean(prices),
        stddev=statistics.stdev(prices) if len(prices) >= 2 else 0.0,
        minimum=min(prices),
        maximum=max(prices),
    )


def _fmt_money(price: float, currency: str | None) -> str:
    amount = f"{price:,.0f}"
    return f"€{amount}" if (currency or "EUR") == "EUR" else f"{amount} {currency}"


def evaluate_fare(
    obs: FareObservation,
    stats: RouteStats,
    econ_reference: float | None,
    cfg: Config,
) -> Deal | None:
    """Score one observation. `stats` is the baseline for the observation's own
    route+cabin; `econ_reference` is the economy price used by the premium-cabin
    rule (None when the observation is economy or no reference exists)."""
    det = cfg.detection
    error_reasons: list[str] = []
    drop_reason: str | None = None
    window = det.baseline_window_days
    price_str = _fmt_money(obs.price, obs.currency)

    if obs.cabin in PREMIUM_CABINS and econ_reference and econ_reference > 0:
        ratio = obs.price / econ_reference
        if ratio <= det.premium_cabin_ratio:
            error_reasons.append(
                f"{obs.cabin.title()} fare {price_str} is only {ratio:.2f}× the economy "
                f"reference {_fmt_money(econ_reference, obs.currency)} — premium-cabin mispricing"
            )

    baseline_ready = stats.count >= det.min_observations and stats.avg
    if baseline_ready:
        avg_str = _fmt_money(stats.avg, obs.currency)
        if obs.price <= stats.avg * det.decimal_error_ratio:
            error_reasons.append(
                f"Price {price_str} is ≤{det.decimal_error_ratio:.0%} of the {window}-day "
                f"average {avg_str} — possible decimal/currency error"
            )
        elif stats.stddev >= MIN_MEANINGFUL_STDDEV:
            zscore = (stats.avg - obs.price) / stats.stddev
            if zscore >= det.zscore_threshold:
                error_reasons.append(
                    f"Price {price_str} is {zscore:.1f}σ below the {window}-day average {avg_str}"
                )
        if obs.price <= stats.avg * (1 - det.discount_threshold_pct / 100):
            drop_reason = (
                f"{(1 - obs.price / stats.avg):.0%} below the {window}-day average "
                f"{avg_str} ({stats.count} observations)"
            )

    if error_reasons:
        kind = "error_fare"
    elif drop_reason:
        kind = "price_drop"
    else:
        return None

    reasons = error_reasons + ([drop_reason] if drop_reason else [])
    return Deal(
        kind=kind,
        origin=obs.origin,
        destination=obs.destination,
        cabin=obs.cabin,
        price=obs.price,
        currency=obs.currency,
        baseline_price=round(stats.avg, 2) if baseline_ready else econ_reference,
        discount_pct=round((1 - obs.price / stats.avg) * 100, 1) if baseline_ready else None,
        departure_date=obs.departure_date,
        return_date=obs.return_date,
        source=obs.source,
        url=obs.deep_link,
        reason="; ".join(reasons),
        dedupe_key=f"{kind}:{obs.origin}:{obs.destination}:{obs.cabin}",
        found_at=obs.observed_at,
    )


def deal_from_post(post: BlogPost) -> Deal:
    matched = ", ".join(post.matched_keywords) or "keyword match"
    return Deal(
        kind="blog",
        source=post.feed,
        url=post.url,
        reason=f"“{post.title}” (matched: {matched})",
        dedupe_key=f"blog:{post.guid}",
    )


def _passes_cooldown(conn: sqlite3.Connection, deal: Deal, cfg: Config, now: datetime) -> bool:
    previous = db.last_deal_for_key(conn, deal.dedupe_key)
    if previous is None:
        return True
    age = now - db.parse_iso(previous["found_at"])
    if age >= timedelta(hours=cfg.alerts.cooldown_hours):
        return True
    if (
        deal.price is not None
        and previous["price"] is not None
        and deal.price <= previous["price"] * (1 - cfg.alerts.realert_drop_pct / 100)
    ):
        return True  # dropped meaningfully further since the last alert
    return False


def detect_deals(
    conn: sqlite3.Connection,
    observations: list[FareObservation],
    new_posts: list[BlogPost],
    cfg: Config,
    now: datetime | None = None,
) -> list[Deal]:
    """Run all rules over this run's observations + newly seen blog posts.
    Call BEFORE inserting the observations (see module docstring)."""
    now = now or datetime.now(timezone.utc)
    since = db.since_iso(cfg.detection.baseline_window_days, now)
    deals: list[Deal] = []

    # Same-run economy prices, the premium-rule fallback reference.
    batch_economy: dict[tuple[str, str], float] = {}
    for obs in observations:
        if obs.cabin == "ECONOMY":
            key = (obs.origin, obs.destination)
            batch_economy[key] = min(obs.price, batch_economy.get(key, float("inf")))

    for obs in observations:
        if obs.price < cfg.detection.min_price_floor:
            log.info("skipping sub-floor fare %s-%s at %s", obs.origin, obs.destination, obs.price)
            continue
        stats = compute_stats(db.route_prices(conn, obs.origin, obs.destination, obs.cabin, since))
        econ_reference = None
        if obs.cabin in PREMIUM_CABINS:
            econ_stats = compute_stats(
                db.route_prices(conn, obs.origin, obs.destination, "ECONOMY", since)
            )
            if econ_stats.count >= 3 and econ_stats.avg:
                econ_reference = econ_stats.avg
            else:
                econ_reference = batch_economy.get((obs.origin, obs.destination))
        deal = evaluate_fare(obs, stats, econ_reference, cfg)
        if deal and _passes_cooldown(conn, deal, cfg, now):
            deals.append(deal)

    # Blog posts: novelty was already established via INSERT OR IGNORE upstream,
    # so every post here becomes a deal — no cooldown needed.
    deals.extend(deal_from_post(post) for post in new_posts)
    return deals
