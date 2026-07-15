"""Tests for the deal-detection engine — the part most likely to have bugs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import make_obs

from flightdeals import db
from flightdeals.detect import compute_stats, detect_deals
from flightdeals.models import BlogPost, Deal


# --- price-drop rule -----------------------------------------------------------


def test_price_drop_flagged(conn, cfg, seed_route):
    seed_route([500] * 8)
    deals = detect_deals(conn, [make_obs(290)], [], cfg)  # 42% below average
    assert len(deals) == 1
    deal = deals[0]
    assert deal.kind == "price_drop"
    assert deal.origin == "OPO" and deal.destination == "JFK"
    assert deal.discount_pct == 42.0
    assert deal.baseline_price == 500
    assert "below the 90-day average" in deal.reason


def test_below_threshold_not_flagged(conn, cfg, seed_route):
    seed_route([500] * 8)
    assert detect_deals(conn, [make_obs(320)], [], cfg) == []  # only 36% below


def test_min_observations_gate(conn, cfg, seed_route):
    seed_route([500] * 3)  # below min_observations=5
    assert detect_deals(conn, [make_obs(100)], [], cfg) == []


def test_observations_outside_window_ignored(conn, cfg, seed_route):
    # Old cheap prices must not drag the baseline: seed 8 obs fully outside
    # the 90-day window plus 8 recent at 500. Baseline must be 500.
    seed_route([250] * 8, days=10, end_days_ago=200)
    seed_route([500] * 8)
    deals = detect_deals(conn, [make_obs(290)], [], cfg)
    assert len(deals) == 1
    assert deals[0].baseline_price == 500


# --- error-fare heuristics -------------------------------------------------------


def test_decimal_error_flagged(conn, cfg, seed_route):
    seed_route([800] * 10)
    deals = detect_deals(conn, [make_obs(79)], [], cfg)
    assert len(deals) == 1
    assert deals[0].kind == "error_fare"
    assert "decimal/currency error" in deals[0].reason
    # the price-drop context is appended too
    assert "below the 90-day average" in deals[0].reason


def test_zscore_outlier_on_tight_route(conn, cfg, seed_route):
    # σ ≈ 2 around 400: a 350 fare is only 12.5% off (misses the 40% rule)
    # but ~25σ out — the z-score rule must catch it.
    seed_route([400, 402, 398, 401, 399, 400, 403, 397])
    deals = detect_deals(conn, [make_obs(350)], [], cfg)
    assert len(deals) == 1
    assert deals[0].kind == "error_fare"
    assert "σ below" in deals[0].reason


def test_zscore_skipped_on_flat_series(conn, cfg, seed_route):
    seed_route([400] * 8)  # stddev == 0 → z-score rule must not fire
    assert detect_deals(conn, [make_obs(395)], [], cfg) == []


def test_premium_cabin_with_db_baseline(conn, cfg, seed_route):
    seed_route([450] * 6, cabin="ECONOMY")
    deals = detect_deals(conn, [make_obs(500, cabin="BUSINESS")], [], cfg)
    assert len(deals) == 1
    assert deals[0].kind == "error_fare"
    assert "premium-cabin mispricing" in deals[0].reason
    assert deals[0].cabin == "BUSINESS"


def test_premium_cabin_normal_price_not_flagged(conn, cfg, seed_route):
    seed_route([450] * 6, cabin="ECONOMY")
    # 1.55× economy — normal business pricing
    assert detect_deals(conn, [make_obs(700, cabin="BUSINESS")], [], cfg) == []


def test_premium_cabin_uses_same_run_economy_fare(conn, cfg):
    # Empty database: the economy fare seen in the same run is the reference,
    # so the heuristic works from the very first run.
    batch = [make_obs(450, cabin="ECONOMY"), make_obs(480, cabin="BUSINESS")]
    deals = detect_deals(conn, batch, [], cfg)
    assert len(deals) == 1
    assert deals[0].cabin == "BUSINESS"
    assert deals[0].kind == "error_fare"


# --- noise guard -------------------------------------------------------------------


def test_sub_floor_prices_ignored(conn, cfg, seed_route):
    seed_route([500] * 8)
    assert detect_deals(conn, [make_obs(5)], [], cfg) == []  # junk row, not a 99% deal


# --- cooldown / dedupe ----------------------------------------------------------------


def _detect_and_store(conn, cfg, obs):
    deals = detect_deals(conn, [obs], [], cfg)
    for deal in deals:
        db.insert_deal(conn, deal)
    return deals


def test_cooldown_suppresses_repeat(conn, cfg, seed_route):
    seed_route([500] * 8)
    assert len(_detect_and_store(conn, cfg, make_obs(290))) == 1
    assert _detect_and_store(conn, cfg, make_obs(290)) == []  # same deal, same day


def test_realert_when_price_drops_further(conn, cfg, seed_route):
    seed_route([500] * 8)
    _detect_and_store(conn, cfg, make_obs(290))
    deals = _detect_and_store(conn, cfg, make_obs(270))  # ≥5% below the alerted 290
    assert len(deals) == 1


def test_realert_after_cooldown_expires(conn, cfg, seed_route):
    seed_route([500] * 8)
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.insert_deal(
        conn,
        Deal(
            kind="price_drop",
            source="amadeus_offers",
            reason="old alert",
            dedupe_key="price_drop:OPO:JFK:ECONOMY",
            price=290,
            found_at=stale,
        ),
    )
    assert len(detect_deals(conn, [make_obs(290)], [], cfg)) == 1


# --- blog finds -----------------------------------------------------------------------


def _post(guid: str = "g1") -> BlogPost:
    return BlogPost(
        feed="Secret Flying",
        guid=guid,
        title="Porto to New York for €180 return",
        url="https://example.com/deal",
        summary="…",
        published_at="2026-07-14T10:00:00Z",
        matched_keywords=["porto"],
    )


def test_blog_post_becomes_deal_unconditionally(conn, cfg):
    deals = detect_deals(conn, [], [_post()], cfg)
    assert len(deals) == 1
    assert deals[0].kind == "blog"
    assert deals[0].source == "Secret Flying"
    assert "Porto to New York" in deals[0].reason
    assert deals[0].dedupe_key == "blog:g1"


def test_blog_post_novelty_is_once_ever(conn):
    assert db.insert_blog_post(conn, _post()) is True
    assert db.insert_blog_post(conn, _post()) is False  # runner drops repeats


# --- stats helpers -----------------------------------------------------------------------


def test_compute_stats_empty_and_single():
    empty = compute_stats([])
    assert empty.count == 0 and empty.avg is None and empty.stddev == 0.0
    single = compute_stats([400.0])
    assert single.count == 1 and single.avg == 400.0 and single.stddev == 0.0
