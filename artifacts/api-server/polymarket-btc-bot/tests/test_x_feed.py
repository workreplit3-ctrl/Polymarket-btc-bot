import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import empty_paper_config
from src.x_feed import analyze_x_snapshot


def _cfg():
    cfg = empty_paper_config().x
    cfg.usernames = ["trusted_btc", "conflict_btc"]
    cfg.min_followers = 100_000
    cfg.fresh_window_sec = 900
    cfg.min_confidence = 0.55
    return cfg


def _profile(profile_id, username, *, verified=True, followers=500_000):
    return {
        "id": profile_id,
        "username": username,
        "verified": verified,
        "verified_type": "blue" if verified else "none",
        "public_metrics": {"followers_count": followers},
    }


def _post(post_id, author_id, text, created_at):
    return {
        "id": post_id,
        "author_id": author_id,
        "text": text,
        "created_at": created_at,
    }


def test_x_signal_requires_verified_influential_fresh_author():
    now = time.time()
    snapshot = {
        "profiles": [
            _profile("1", "trusted_btc"),
            _profile("2", "unverified", verified=False),
            _profile("3", "small_btc", followers=10_000),
        ],
        "posts": [
            _post("1", "1", "Bitcoin bullish breakout and upside.", "2026-09-15T00:00:00Z"),
            _post("2", "2", "Bitcoin bullish breakout.", "2026-09-15T00:00:00Z"),
            _post("3", "3", "Bitcoin bullish breakout.", "2026-09-15T00:00:00Z"),
        ],
    }
    # Replace the fixed example timestamp with a fresh one without making the
    # test depend on the wall clock date.
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30))
    snapshot["posts"][0]["created_at"] = fresh
    snapshot["posts"][1]["created_at"] = fresh
    snapshot["posts"][2]["created_at"] = fresh

    signal = analyze_x_snapshot(snapshot, _cfg(), now=now)

    assert signal.direction == "UP"
    assert signal.post_count == 1
    assert signal.valid is True


def test_x_signal_rejects_stale_posts_and_marks_conflicts():
    now = time.time()
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))
    fresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30))
    snapshot = {
        "profiles": [
            _profile("1", "trusted_btc"),
            _profile("2", "conflict_btc"),
        ],
        "posts": [
            _post("old", "1", "Bitcoin bullish breakout.", old),
            _post("up", "1", "Bitcoin bullish breakout.", fresh),
            _post("down", "2", "Bitcoin bearish breakdown.", fresh),
        ],
    }

    signal = analyze_x_snapshot(snapshot, _cfg(), now=now)

    assert signal.direction == "NEUTRAL"
    assert signal.reason == "fresh BTC posts conflict"
    assert signal.post_count == 2