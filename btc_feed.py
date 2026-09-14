"""
BTC price feed aggregator.

Subscribes to Binance and Coinbase WebSocket streams in parallel,
maintains a rolling price history, and exposes a consensus mid price
plus a "staleness" flag if feeds diverge too much.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

import websockets

from .config import BtcFeedsCfg
from .logger import get_logger

log = get_logger("btc_feed")


@dataclass
class Tick:
    source: str
    price: float
    ts: float  # epoch seconds


@dataclass
class Consensus:
    mid: float
    binance: Optional[float]
    coinbase: Optional[float]
    divergence_pct: float
    is_stale: bool
    ts: float


class _RingBuffer:
    """Bounded deque-like buffer of (ts, price) tuples."""
    __slots__ = ("capacity_seconds", "data")

    def __init__(self, capacity_seconds: float = 7200.0):
        self.capacity_seconds = capacity_seconds
        self.data: List[tuple[float, float]] = []

    def append(self, ts: float, price: float) -> None:
        self.data.append((ts, price))
        cutoff = ts - self.capacity_seconds
        # Trim old entries; list is appended in time order so we can pop from head
        while self.data and self.data[0][0] < cutoff:
            self.data.pop(0)

    def latest(self) -> Optional[tuple[float, float]]:
        return self.data[-1] if self.data else None

    def price_at_or_before(self, ts: float) -> Optional[float]:
        # Linear scan from the tail (most recent first)
        for t, p in reversed(self.data):
            if t <= ts:
                return p
        return None

    def price_n_seconds_ago(self, now: float, n: float) -> Optional[float]:
        target = now - n
        return self.price_at_or_before(target)

    def rolling_returns(self, window_sec: float = 60.0, count: int = 60) -> List[float]:
        if len(self.data) < 2:
            return []
        now = self.data[-1][0]
        out: List[float] = []
        step = window_sec / count
        prev = None
        for i in range(count + 1):
            t = now - window_sec + i * step
            p = self.price_at_or_before(t)
            if p is None:
                continue
            if prev is not None and prev != 0:
                out.append((p - prev) / prev)
            prev = p
        return out


class BtcPriceAggregator:
    """Aggregates Binance + Coinbase feeds into a consensus mid."""

    def __init__(self, cfg: BtcFeedsCfg):
        self.cfg = cfg
        self._binance_price: Optional[float] = None
        self._binance_ts: float = 0.0
        self._coinbase_price: Optional[float] = None
        self._coinbase_ts: float = 0.0
        self._history = _RingBuffer(capacity_seconds=7200.0)
        self._stop = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._listeners: List[Callable[[Tick], Awaitable[None]]] = []
        self._stale_after_sec = 15.0  # mark feed dead if older than this

    # ----- public API ------------------------------------------------
    def add_listener(self, fn: Callable[[Tick], Awaitable[None]]) -> None:
        self._listeners.append(fn)

    def consensus(self) -> Consensus:
        now = time.time()
        binance = self._binance_price if (now - self._binance_ts) < self._stale_after_sec else None
        coinbase = self._coinbase_price if (now - self._coinbase_ts) < self._stale_after_sec else None
        prices = [p for p in (binance, coinbase) if p is not None]
        if not prices:
            return Consensus(mid=0.0, binance=binance, coinbase=coinbase,
                             divergence_pct=100.0, is_stale=True, ts=now)
        mid = sum(prices) / len(prices)
        if len(prices) == 2:
            div_pct = abs(binance - coinbase) / mid * 100.0  # type: ignore[arg-type]
        else:
            div_pct = 0.0
        is_stale = div_pct > self.cfg.cross_feed_max_divergence_pct
        return Consensus(mid=mid, binance=binance, coinbase=coinbase,
                         divergence_pct=div_pct, is_stale=is_stale, ts=now)

    def history(self) -> _RingBuffer:
        return self._history

    def volatility_60s(self) -> float:
        """Stdev of 1-minute returns over the last 60 seconds."""
        rets = self._history.rolling_returns(window_sec=60.0, count=60)
        if len(rets) < 5:
            return 0.0
        m = sum(rets) / len(rets)
        var = sum((r - m) ** 2 for r in rets) / len(rets)
        return var ** 0.5

    def price_n_seconds_ago(self, n: float) -> Optional[float]:
        return self._history.price_n_seconds_ago(time.time(), n)

    # ----- lifecycle -------------------------------------------------
    async def start(self) -> None:
        if self.cfg.binance.enabled:
            self._tasks.append(asyncio.create_task(self._run_binance()))
        if self.cfg.coinbase.enabled:
            self._tasks.append(asyncio.create_task(self._run_coinbase()))
        log.info("BTC price aggregator started "
                 f"(binance={self.cfg.binance.enabled}, coinbase={self.cfg.coinbase.enabled})")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    # ----- feed handlers ---------------------------------------------
    async def _run_binance(self) -> None:
        await self._ws_loop(
            name="binance",
            url=self.cfg.binance.ws_url,
            on_connect=lambda: None,
            on_message=self._handle_binance,
            min_backoff=self.cfg.binance.reconnect_min_sec,
            max_backoff=self.cfg.binance.reconnect_max_sec,
        )

    async def _run_coinbase(self) -> None:
        async def on_connect(ws):
            sub = {
                "type": "subscribe",
                "product_ids": [self.cfg.coinbase.product_id],
                "channels": ["ticker"],
            }
            await ws.send(json.dumps(sub))

        await self._ws_loop(
            name="coinbase",
            url=self.cfg.coinbase.ws_url,
            on_connect=on_connect,
            on_message=self._handle_coinbase,
            min_backoff=self.cfg.coinbase.reconnect_min_sec,
            max_backoff=self.cfg.coinbase.reconnect_max_sec,
        )

    async def _ws_loop(self, name, url, on_connect, on_message, min_backoff, max_backoff):
        backoff = min_backoff
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              close_timeout=5) as ws:
                    log.info(f"{name} WS connected: {url}")
                    backoff = min_backoff
                    await on_connect(ws)
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        try:
                            await on_message(msg)
                        except Exception as e:
                            log.warning(f"{name} message handler error: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"{name} WS disconnected: {e}; reconnect in {backoff}s")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)

    async def _handle_binance(self, msg: dict) -> None:
        # bookTicker stream: {"u":..,"b":"best_bid","a":"best_ask",...}
        try:
            bid = float(msg.get("b", 0))
            ask = float(msg.get("a", 0))
            if bid <= 0 or ask <= 0:
                return
            mid = (bid + ask) / 2.0
            self._binance_price = mid
            self._binance_ts = time.time()
            self._history.append(self._binance_ts, mid)
            await self._broadcast(Tick("binance", mid, self._binance_ts))
        except (TypeError, ValueError) as e:
            log.debug(f"binance bad msg: {e}")

    async def _handle_coinbase(self, msg: dict) -> None:
        # ticker channel events: {"type":"ticker","price":"...","product_id":"BTC-USD",...}
        if msg.get("type") != "ticker":
            return
        try:
            price = float(msg.get("price", 0))
            if price <= 0:
                return
            self._coinbase_price = price
            self._coinbase_ts = time.time()
            self._history.append(self._coinbase_ts, price)
            await self._broadcast(Tick("coinbase", price, self._coinbase_ts))
        except (TypeError, ValueError) as e:
            log.debug(f"coinbase bad msg: {e}")

    async def _broadcast(self, tick: Tick) -> None:
        for fn in list(self._listeners):
            try:
                await fn(tick)
            except Exception as e:
                log.warning(f"listener error: {e}")
