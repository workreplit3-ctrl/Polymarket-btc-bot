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
import json
import os
import time
from pathlib import Path
from typing import List, Optional

from .btc_feed import BtcPriceAggregator
from .config import Config
from .engine import TradeEvent, TradingEngine
from .logger import get_logger
from .polymarket_client import MarketInfo, PolymarketClient
from .risk import Position, RiskManager
from .storage import Storage
from .strategy import DivergenceStrategy, Signal, SignalAction
from .telegram_bot import TelegramBot

log = get_logger("orchestrator")


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if cfg.mode == "real":
            # Fail before starting feeds, Telegram, or the trading loop.
            PolymarketClient.validate_real_mode()
        self.feed = BtcPriceAggregator(cfg.btc_feeds)
        self.poly = PolymarketClient(cfg.polymarket)
        self.risk = RiskManager(cfg.risk)
        self.storage = Storage(cfg.storage.sqlite_path)
        self.strategy = DivergenceStrategy(cfg.strategy, self.feed)
        self.engine = TradingEngine(cfg, self.poly, self.risk, self.storage)
        self.tg = TelegramBot(cfg, self.risk, self.storage, self.engine, self)

        self.tracked_markets: List[MarketInfo] = []
        self._pause_file = Path(cfg.storage.sqlite_path).with_name("control.json")
        self._pause_flag = Path(cfg.storage.sqlite_path).with_name("paused.flag")
        self.paused: bool = self._read_pause_state()
        self._real_entries_paused: bool = False
        self.reconciliation_error: Optional[str] = None
        self._shutdown = asyncio.Event()
        self._market_refresh_ts: float = 0.0
        # Refresh markets every 5 minutes
        self._market_refresh_interval = 300
        self._paper_run_started_at = time.time()
        # Keep the last successful market metadata snapshot so a position can
        # still be settled after Gamma removes its market from the active list.
        self._known_markets: dict[str, MarketInfo] = {}
        self._latest_market_ids: Optional[set[str]] = None

    # ----- lifecycle ------------------------------------------------
    async def run(self) -> None:
        log.info(f"orchestrator starting in {self.cfg.mode} mode")
        self._sync_pause_state()
        if self.cfg.mode == "real":
            try:
                await self._reconcile_real_positions()
            except Exception as exc:
                self._real_entries_paused = True
                self.reconciliation_error = str(exc)
                log.exception(
                    "real position reconciliation failed; new real entries "
                    "will remain paused"
                )
        await self.feed.start()
        await self.tg.start()
        await self.tg.send(f"🤖 bot started in *{self.cfg.mode}* mode")
        if self.reconciliation_error:
            await self.tg.send(
                "⚠️ *real-entry reconciliation error*\n"
                f"{self.reconciliation_error}\n"
                "New real entries are paused until the bot is restarted "
                "after wallet access is restored."
            )
        try:
            await self._loop()
        finally:
            await self._shutdown_graceful()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def on_mode_changed(self) -> None:
        log.info(f"mode switched to {self.cfg.mode}")
        # No special action needed; engine reads self.cfg.mode at each call.
        # Real-mode API compatibility is checked before Config.switch_mode().

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        self._pause_flag.parent.mkdir(parents=True, exist_ok=True)
        self._pause_file.parent.mkdir(parents=True, exist_ok=True)
        if paused:
            self._pause_flag.touch()
            temporary_path = self._pause_file.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps({"paused": True}) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_path, self._pause_file)
        else:
            self._pause_flag.unlink(missing_ok=True)
            self._pause_file.unlink(missing_ok=True)
        log.warning("strategy loop paused" if paused else "strategy loop resumed")

    def _sync_pause_state(self) -> None:
        file_paused = self._read_pause_state()
        if file_paused != self.paused:
            self.paused = file_paused
            log.warning("strategy loop paused" if self.paused else "strategy loop resumed")

    def _read_pause_state(self) -> bool:
        if self._pause_flag.exists():
            return True
        try:
            raw = json.loads(self._pause_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        return raw.get("paused") is True

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

        # Preserve the pause contract for expiry closes too: a pause blocks
        # execution, including paper settlement, until the operator resumes.
        self._sync_pause_state()
        expiry_events = await self._close_expired_paper_positions()
        for event in expiry_events:
            await self._notify_trade_event(event)

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
        end_ts = _parse_iso_ts(market.end_date)
        minutes_to_close = (
            max(0.0, (end_ts - time.time()) / 60.0)
            if end_ts is not None else float("nan")
        )
        up_ask = up_book.best_ask()
        down_ask = down_book.best_ask()
        actionable_edge_up = (
            signal.our_prob_up - up_ask.price if up_ask is not None else None
        )
        actionable_edge_down = (
            (1.0 - signal.our_prob_up) - down_ask.price
            if down_ask is not None else None
        )
        log.info(
            f"selected market slug={market.slug} "
            f"minutes_to_close={minutes_to_close:.2f} "
            f"signal={signal.action.value} "
            f"actionable_edge_up={_format_edge(actionable_edge_up)} "
            f"actionable_edge_down={_format_edge(actionable_edge_down)} "
            f"reason={signal.reason}"
        )
        await self.storage.log_signal(
            action=signal.action.value, btc_price=signal.btc_price,
            btc_drift_pct=signal.btc_drift_pct, our_prob_up=signal.our_prob_up,
            market_prob_up=signal.market_prob_up, edge=signal.edge,
            actionable_edge_up=actionable_edge_up,
            actionable_edge_down=actionable_edge_down,
            reason=signal.reason,
        )

        # 6. Update marks on open positions for risk snapshot freshness
        marks = {}
        for cid, p in self.risk.state.open_positions.items():
            if cid == market.condition_id:
                marks[cid] = up_book.mid() if p.side == "UP" else down_book.mid() or 0.0
        self.risk.mark_positions(marks)

        # 7. Execute (only meaningful actions)
        # A pause can arrive while feeds/orderbooks are being refreshed. Check
        # again immediately before execution so the emergency action blocks
        # new orders without closing an already open position.
        self._sync_pause_state()
        if self.paused:
            return
        if (
            self.cfg.mode == "real"
            and self._real_entries_paused
            and signal.action in (SignalAction.OPEN_UP, SignalAction.OPEN_DOWN)
        ):
            log.warning(
                "real entry blocked because wallet reconciliation has not "
                "completed successfully"
            )
            return
        if signal.action in (SignalAction.OPEN_UP, SignalAction.OPEN_DOWN, SignalAction.EXIT):
            event = await self.engine.execute(signal, market, up_book, down_book)
            if event and event.action in ("OPEN", "CLOSE"):
                await self._notify_trade_event(event)
                if event.action == "CLOSE" and self.cfg.mode == "paper":
                    summary = await self.storage.paper_summary(
                        self._paper_run_started_at
                    )
                    log.info(
                        f"paper summary closed_trades={summary['closed_trades']} "
                        f"pnl_usdc={summary['pnl_usdc']:+.4f} "
                        f"exit_reasons={summary['exit_reasons']}"
                    )

    async def _notify_trade_event(self, event: TradeEvent) -> None:
        """Broadcast a completed trade event when Telegram is available."""
        tg = getattr(self, "tg", None)
        if tg is None:
            return
        await tg.send(
            f"{'📈' if event.action == 'OPEN' else '📉'} "
            f"{event.action} {event.side} {event.slug}\n"
            f"status {event.order_status or 'filled'}\n"
            f"price {event.price:.4f}  ${event.size_usdc:.2f}\n"
            f"pnl ${event.pnl:.2f}\n"
            f"reason: {event.reason}"
        )

    async def _close_expired_paper_positions(self) -> list[TradeEvent]:
        """Settle paper positions whose market can no longer be traded."""
        if self.cfg.mode != "paper" or getattr(self, "paused", False):
            return []

        known_markets = getattr(self, "_known_markets", {})
        latest_market_ids = getattr(self, "_latest_market_ids", None)
        tracked_by_condition = {
            market.condition_id: market for market in self.tracked_markets
        }
        events: list[TradeEvent] = []
        now = time.time()

        for condition_id, position in list(
            self.risk.state.open_positions.items()
        ):
            market = (
                tracked_by_condition.get(condition_id)
                or known_markets.get(condition_id)
            )
            if market is None:
                continue

            end_ts = _parse_iso_ts(market.end_date)
            reached_resolution = end_ts is not None and end_ts <= now
            no_longer_tradable = (
                latest_market_ids is not None
                and condition_id not in latest_market_ids
            )
            if not reached_resolution and market.active and not no_longer_tradable:
                continue

            reason = (
                "market expired"
                if reached_resolution or not market.active
                else "market no longer tradable"
            )
            # A mark is the only price available after the market leaves the
            # order book. Preserve it for P/L; entry is a deterministic
            # fallback for positions that have never received a mark.
            settlement_price = (
                position.mark_price
                if position.mark_price > 0.0
                else position.entry_price
            )
            event = await self.engine.expire_paper_position(
                market, settlement_price, reason
            )
            if event is not None:
                events.append(event)
                self.tracked_markets = [
                    tracked
                    for tracked in self.tracked_markets
                    if tracked.condition_id != condition_id
                ]
        return events

    async def _reconcile_real_positions(self) -> None:
        """Discover active BTC markets and rebuild risk from wallet holdings."""
        await self._refresh_markets(raise_on_error=True)
        holdings = await self.poly.get_confirmed_token_holdings(
            self.tracked_markets
        )
        markets_by_condition = {
            market.condition_id: market for market in self.tracked_markets
        }
        markets_by_token = {
            token_id: market
            for market in self.tracked_markets
            for token_id in (
                market.outcome_up_token_id,
                market.outcome_down_token_id,
            )
            if token_id
        }
        positions: List[Position] = []
        position_conditions: set[str] = set()
        for holding in holdings:
            market = (
                markets_by_condition.get(holding.condition_id)
                or markets_by_token.get(holding.token_id)
            )
            if market is None:
                continue
            if holding.token_id == market.outcome_up_token_id:
                side = "UP"
            elif holding.token_id == market.outcome_down_token_id:
                side = "DOWN"
            else:
                continue
            if market.condition_id in position_conditions:
                raise RuntimeError(
                    "cannot reconcile real positions: wallet has holdings "
                    f"on both outcome tokens for {market.slug}"
                )
            position_conditions.add(market.condition_id)
            positions.append(
                Position(
                    market_condition_id=market.condition_id,
                    market_slug=market.slug,
                    side=side,
                    token_id=holding.token_id,
                    entry_price=holding.avg_price,
                    size_shares=holding.size_shares,
                    size_usdc=holding.size_shares * holding.avg_price,
                    entry_ts=time.time(),
                    mark_price=holding.avg_price,
                )
            )
        self.risk.replace_open_positions(positions)
        self._real_entries_paused = False
        log.info(
            f"real position reconciliation succeeded: "
            f"{len(positions)} confirmed position(s)"
        )

    async def _refresh_markets(self, raise_on_error: bool = False) -> None:
        try:
            markets = await self.poly.list_active_btc_updown_markets(
                self.cfg.polymarket.market_filter.slug_prefix
            )
        except Exception as e:
            log.warning(f"market refresh failed: {e}")
            if raise_on_error:
                raise
            return
        self._latest_market_ids = {m.condition_id for m in markets}
        self._known_markets = {
            **getattr(self, "_known_markets", {}),
            **{m.condition_id: m for m in markets},
        }
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
        if self.cfg.mode == "paper":
            summary = await self.storage.paper_summary(self._paper_run_started_at)
            log.info(
                f"paper run summary closed_trades={summary['closed_trades']} "
                f"pnl_usdc={summary['pnl_usdc']:+.4f} "
                f"exit_reasons={summary['exit_reasons']}"
            )
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


def _format_edge(edge: Optional[float]) -> str:
    return "n/a" if edge is None else f"{edge:+.4f}"
