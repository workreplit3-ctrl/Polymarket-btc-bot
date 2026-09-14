"""
Bot orchestrator — wires together feed, polymarket client, strategy,
risk manager, engine, storage, and Telegram.

The main loop runs once every `strategy.tick_interval_sec` seconds:
  1. Refresh the list of tracked BTC up/down markets.
  2. For the nearest-to-resolution active market, fetch orderbooks.
  3. Run the strategy.
  4. Execute the resulting signal via the engine.
  5. Log + broadcast to Telegram.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import List, Optional

from .btc_feed import BtcPriceAggregator
from .config import Config
from .engine import TradeEvent, TradingEngine
from .logger import get_logger
from .polymarket_client import MarketInfo, PolymarketClient
from .risk import RiskManager
from .storage import Storage
from .strategy import DivergenceStrategy, Signal, SignalAction
from .telegram_bot import TelegramBot

log = get_logger("orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.feed = BtcPriceAggregator(cfg.btc_feeds)
        self.poly = PolymarketClient(cfg.polymarket)
        self.risk = RiskManager(cfg.risk)
        self.storage = Storage(cfg.storage.sqlite_path)
        self.strategy = DivergenceStrategy(cfg.strategy, self.feed)
        self.engine = TradingEngine(cfg, self.poly, self.risk, self.storage)
        self.tg = TelegramBot(cfg, self.risk, self.storage, self.engine, self)

        self.tracked_markets: List[MarketInfo] = []
        self.paused: bool = False
        self._pause_flag = Path(cfg.storage.sqlite_path).with_name("paused.flag")
        self._shutdown = asyncio.Event()
        self._market_refresh_ts: float = 0.0
        # Refresh markets every 5 minutes
        self._market_refresh_interval = 300

    # ----- lifecycle ------------------------------------------------
    async def run(self) -> None:
        log.info(f"orchestrator starting in {self.cfg.mode} mode")
        self._sync_pause_state()
        await self.feed.start()
        await self.tg.start()
        await self.tg.send(f"🤖 bot started in *{self.cfg.mode}* mode")
        try:
            await self._loop()
        finally:
            await self._shutdown_graceful()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def on_mode_changed(self) -> None:
        log.info(f"mode switched to {self.cfg.mode}")
        # No special action needed; engine reads self.cfg.mode at each call.
        # In real mode, the first order will lazy-build the CLOB signer.

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._pause_flag.parent.mkdir(parents=True, exist_ok=True)
        if paused:
            self._pause_flag.touch()
        else:
            self._pause_flag.unlink(missing_ok=True)
        log.warning("strategy loop paused" if paused else "strategy loop resumed")

    def _sync_pause_state(self) -> None:
        file_paused = self._pause_flag.exists()
        if file_paused != self.paused:
            self.paused = file_paused
            log.warning("strategy loop paused" if self.paused else "strategy loop resumed")

    # ----- main loop ------------------------------------------------
    async def _loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                self._sync_pause_state()
                if not self.paused:
                    await self._tick()
                # Always log equity + signal summary even when paused
                await asyncio.sleep(self.cfg.strategy.tick_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception(f"tick error: {e}")
                await asyncio.sleep(self.cfg.strategy.tick_interval_sec)

    async def _tick(self) -> None:
        # 1. Refresh markets if stale
        now = time.time()
        if now - self._market_refresh_ts > self._market_refresh_interval or not self.tracked_markets:
            await self._refresh_markets()
            self._market_refresh_ts = now

        if not self.tracked_markets:
            return

        # 2. Pick the nearest-to-resolution market that still has time left
        market = self._pick_market()
        if market is None:
            return

        # 3. Fetch orderbooks for both Up and Down tokens
        up_book, down_book = await asyncio.gather(
            self.poly.get_orderbook(market.outcome_up_token_id),
            self.poly.get_orderbook(market.outcome_down_token_id),
        )

        # 4. Determine current position side for this market
        pos = self.risk.state.open_positions.get(market.condition_id)
        current_side = pos.side if pos else None

        # 5. Run strategy
        signal = self.strategy.evaluate(up_book, down_book, current_side)
        await self.storage.log_signal(
            action=signal.action.value, btc_price=signal.btc_price,
            btc_drift_pct=signal.btc_drift_pct, our_prob_up=signal.our_prob_up,
            market_prob_up=signal.market_prob_up, edge=signal.edge,
            reason=signal.reason,
        )

        # 6. Update marks on open positions for risk snapshot freshness
        marks = {}
        for cid, p in self.risk.state.open_positions.items():
            if cid == market.condition_id:
                marks[cid] = up_book.mid() if p.side == "UP" else down_book.mid() or 0.0
        self.risk.mark_positions(marks)

        # 7. Execute (only meaningful actions)
        if signal.action in (SignalAction.OPEN_UP, SignalAction.OPEN_DOWN, SignalAction.EXIT):
            self._sync_pause_state()
            if self.paused:
                return
            event = await self.engine.execute(signal, market, up_book, down_book)
            if event and event.action in ("OPEN", "CLOSE"):
                await self.tg.send(
                    f"{'📈' if event.action == 'OPEN' else '📉'} "
                    f"{event.action} {event.side} {event.slug}\n"
                    f"price {event.price:.4f}  ${event.size_usdc:.2f}\n"
                    f"pnl ${event.pnl:.2f}\n"
                    f"reason: {event.reason}"
                )

    async def _refresh_markets(self) -> None:
        try:
            markets = await self.poly.list_active_btc_updown_markets(
                self.cfg.polymarket.market_filter.slug_prefix
            )
        except Exception as e:
            log.warning(f"market refresh failed: {e}")
            return
        # Filter by the intended resolution window and minimum volume. Gamma can
        # expose future BTC markets as active, so the upper bound is mandatory:
        # this strategy is calibrated for the current short-duration window.
        keep: List[MarketInfo] = []
        skipped_too_early = 0
        skipped_too_late = 0
        skipped_volume = 0
        skipped_invalid_time = 0
        for m in markets:
            try:
                end_ts = _parse_iso_ts(m.end_date)
                if end_ts is None:
                    skipped_invalid_time += 1
                    continue
                mins_left = (end_ts - time.time()) / 60.0
                if mins_left < self.cfg.polymarket.market_filter.min_minutes_to_resolution:
                    skipped_too_early += 1
                    continue
                if mins_left > self.cfg.polymarket.market_filter.max_minutes_to_resolution:
                    skipped_too_late += 1
                    continue
                if m.volume < self.cfg.polymarket.market_filter.min_volume_usd:
                    skipped_volume += 1
                    continue
                keep.append(m)
            except Exception:
                skipped_invalid_time += 1
                continue
        keep.sort(key=lambda x: _parse_iso_ts(x.end_date) or float("inf"))
        self.tracked_markets = keep
        log.info(
            f"tracking {len(keep)} markets; nearest end={keep[0].end_date if keep else '-'} "
            f"(skipped too_early={skipped_too_early}, too_late={skipped_too_late}, "
            f"low_volume={skipped_volume}, invalid_time={skipped_invalid_time})"
        )

    def _pick_market(self) -> Optional[MarketInfo]:
        if not self.tracked_markets:
            return None
        # If we have an open position on one of the tracked markets, prefer it.
        for m in self.tracked_markets:
            if m.condition_id in self.risk.state.open_positions:
                return m
        # Otherwise, nearest-to-resolution market.
        return self.tracked_markets[0]

    async def _shutdown_graceful(self) -> None:
        log.info("shutting down…")
        await self.tg.stop()
        await self.feed.stop()
        await self.poly.close()
        log.info("shutdown complete")


def _parse_iso_ts(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        # ISO with Z suffix
        from datetime import datetime
        s2 = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        return None
