"""Fresh, allowlisted X posts used only as a conservative confirmation factor."""
from __future__ import annotations

import asyncio
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

import httpx

from .config import XCfg
from .logger import get_logger

log = get_logger("x-feed")

BTC_TERMS = re.compile(r"\b(?:btc|bitcoin)\b|#bitcoin", re.IGNORECASE)
POSITIVE_TERMS = (
    "bullish", "breakout", "breaks above", "rally", "surge", "inflow",
    "inflows", "accumulation", "support", "upside", "buy", "higher high",
    "all-time high", "ath",
)
NEGATIVE_TERMS = (
    "bearish", "breakdown", "breaks below", "dump", "selloff", "outflow",
    "outflows", "distribution", "resistance", "downside", "sell", "lower low",
    "capitulation",
)
NEGATION_TERMS = ("not ", "no ", "never ", "unlikely ")


@dataclass(frozen=True)
class XPost:
    username: str
    text: str
    created_at: str
    age_seconds: float
    direction_score: float
    followers: int


@dataclass(frozen=True)
class XSignal:
    direction: str = "NEUTRAL"  # UP | DOWN | NEUTRAL
    confidence: float = 0.0
    post_count: int = 0
    valid: bool = False
    reason: str = "X unavailable"
    posts: tuple[XPost, ...] = ()

    @property
    def summary(self) -> str:
        return (
            f"direction={self.direction} confidence={self.confidence:.2f} "
            f"posts={self.post_count} valid={self.valid} reason={self.reason}"
        )


def _parse_created_at(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _term_score(text: str) -> float:
    lowered = text.lower()
    if not BTC_TERMS.search(lowered):
        return 0.0

    positive = sum(1 for term in POSITIVE_TERMS if term in lowered)
    negative = sum(1 for term in NEGATIVE_TERMS if term in lowered)
    score = float(positive - negative)
    if score == 0:
        return 0.0

    # Do not treat "not bullish" or similar wording as a bullish confirmation.
    for negation in NEGATION_TERMS:
        if f"{negation}bullish" in lowered or f"{negation}bearish" in lowered:
            score *= -1
            break
    return max(-2.0, min(2.0, score))


def analyze_x_snapshot(
    snapshot: Dict[str, Any],
    cfg: XCfg,
    *,
    now: float | None = None,
) -> XSignal:
    now = time.time() if now is None else now
    profiles = {
        str(profile.get("id")): profile
        for profile in snapshot.get("profiles", [])
        if isinstance(profile, dict) and profile.get("id")
    }
    allowed = {username.lower().lstrip("@") for username in cfg.usernames}
    posts: List[XPost] = []
    positive_count = 0
    negative_count = 0
    weighted_score = 0.0

    for raw_post in snapshot.get("posts", []):
        if not isinstance(raw_post, dict):
            continue
        author = profiles.get(str(raw_post.get("author_id")))
        username = str(author.get("username", "")).lower() if author else ""
        if username not in allowed:
            continue
        verified = bool(author.get("verified")) or (
            bool(author.get("verified_type"))
            and str(author.get("verified_type")).lower() != "none"
        )
        followers = int((author.get("public_metrics") or {}).get("followers_count", 0))
        if not verified or followers < cfg.min_followers:
            continue

        created_at = raw_post.get("created_at")
        created_ts = _parse_created_at(created_at)
        if created_ts is None:
            continue
        age = now - created_ts
        if age < -60 or age > cfg.fresh_window_sec:
            continue

        text = str(raw_post.get("text", "")).strip()
        score = _term_score(text)
        if not text or score == 0:
            continue
        influence_weight = min(
            1.5,
            1.0 + max(0.0, math.log10(max(1, followers) / max(1, cfg.min_followers))) * 0.15,
        )
        weighted_score += score * influence_weight
        positive_count += score > 0
        negative_count += score < 0
        posts.append(
            XPost(
                username=username,
                text=text,
                created_at=str(created_at),
                age_seconds=age,
                direction_score=score,
                followers=followers,
            )
        )

    if not posts:
        return XSignal(reason="no fresh directional BTC posts")

    conflict_ratio = min(positive_count, negative_count) / max(
        positive_count, negative_count
    )
    if (
        positive_count
        and negative_count
        and conflict_ratio >= 0.35
    ):
        return XSignal(
            post_count=len(posts),
            valid=True,
            reason="fresh BTC posts conflict",
            posts=tuple(posts),
        )

    direction = "UP" if weighted_score > 0 else "DOWN"
    confidence = min(
        1.0,
        abs(weighted_score) / max(2.0, len(posts) * 1.5),
    )
    return XSignal(
        direction=direction if confidence >= cfg.min_confidence else "NEUTRAL",
        confidence=confidence,
        post_count=len(posts),
        valid=True,
        reason=f"{direction.lower()} confirmation from allowlisted verified authors",
        posts=tuple(posts),
    )


class XSignalProvider:
    def __init__(self, cfg: XCfg):
        self.cfg = cfg
        self._cached = XSignal(reason="X disabled")
        self._last_fetch = 0.0
        self._lock = asyncio.Lock()

    async def get_signal(self) -> XSignal:
        if not self.cfg.enabled:
            return XSignal(reason="X disabled")
        if not self.cfg.usernames:
            return XSignal(reason="X allowlist is empty")
        if time.time() - self._last_fetch < self.cfg.refresh_interval_sec:
            return self._cached

        async with self._lock:
            if time.time() - self._last_fetch < self.cfg.refresh_interval_sec:
                return self._cached
            try:
                payload = await self._fetch_snapshot()
                self._cached = analyze_x_snapshot(payload, self.cfg)
            except Exception as exc:
                log.warning(f"X confirmation unavailable: {exc}")
                self._cached = XSignal(reason="X API unavailable")
            self._last_fetch = time.time()
            return self._cached

    async def _fetch_snapshot(self) -> Dict[str, Any]:
        secret = os.getenv("SESSION_SECRET", "").strip()
        if not secret:
            raise RuntimeError("SESSION_SECRET is not configured")
        port = os.getenv("PORT", "5000")
        base_url = os.getenv(
            "POLYMARKET_API_BASE_URL",
            f"http://127.0.0.1:{port}/api",
        ).rstrip("/")
        params = {
            "usernames": ",".join(self.cfg.usernames),
            "lookback_seconds": str(self.cfg.fresh_window_sec),
            "max_posts": "50",
        }
        timeout = httpx.Timeout(self.cfg.request_timeout_sec)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(
                f"{base_url}/internal/x/snapshot",
                params=params,
                headers={"x-bot-internal-secret": secret},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("X snapshot response is not an object")
            return payload