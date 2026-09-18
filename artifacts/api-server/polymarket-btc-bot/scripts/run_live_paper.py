#!/usr/bin/env python3
"""Run the configured BTC strategy against live public data in paper mode.

The runner deliberately does not start Telegram, does not load wallet secrets,
and never calls TradingEngine.place_order. It uses the same BTC feed,
market-discovery, orderbook, and strategy code as the bot, then simulates the
entry at best ask and the exit at best bid or at BTC market settlement.

Usage:
    python scripts/run_live_paper.py --trades 10
    python scripts/run_live_paper.py --trades 10 --out data/live_paper_10.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from src.btc_feed import BtcPriceAggregator
from src.config import _build
from src.polymarket_client import MarketInfo, OrderBook, PolymarketClient
from src.strategy import DivergenceStrategy, SignalAction
from src.x_feed import XSignal


POLL_SEC = 5.0
MARKET_REFRESH_SEC = 20.0


@dataclass
class PaperPosition:
    market: MarketInfo
    side: str
    entry_ts: float
    entry_price: float
    size_usdc: float
    size_shares: float
    entry_signal: dict[str, Any]


def _parse_end_date(value: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _signal_snapshot(signal, market: MarketInfo) -> dict[str, Any]:
    return {
        "action": signal.action.value,
        "slug": market.slug,
        "btc_price": signal.btc_price,
        "btc_drift_pct": signal.btc_drift_pct,
        "our_prob_up": signal.our_prob_up,
        "market_prob_up": signal.market_prob_up,
        "edge": signal.edge,
        "reason": signal.reason,
        "ts": signal.ts,
    }


def _settlement_up(feed: BtcPriceAggregator, market_start_price: float) -> bool:
    latest = feed.consensus().mid
    if not math.isfinite(latest) or latest <= 0:
        observed = feed.history().latest()
        latest = observed[1] if observed else market_start_price
    return latest > market_start_price


class LivePaperRunner:
    def __init__(self, cfg, target_trades: int, output: Path, max_runtime_sec: float):
        self.cfg = cfg
        self.target_trades = target_trades
        self.output = output
        self.max_runtime_sec = max_runtime_sec
        self.started_ts = time.time()
        self.feed = BtcPriceAggregator(cfg.btc_feeds)
        self.poly = PolymarketClient(cfg.polymarket)
        self.strategy = DivergenceStrategy(cfg.strategy, self.feed)
        self.position: Optional[PaperPosition] = None
        self.markets: dict[str, MarketInfo] = {}
        self.closed: list[dict[str, Any]] = []
        self.stop_requested = False
        self.last_market_refresh = 0.0

    async def run(self) -> dict[str, Any]:
        await self.feed.start()
        try:
            print(
                "live paper runner started: "
                f"target={self.target_trades} "
                f"per_trade=${self.cfg.risk.per_trade_size_usdc:.2f} "
                f"raw_weight={self.cfg.strategy.calibration_raw_model_weight:.2f}",
                flush=True,
            )
            while (
                len(self.closed) < self.target_trades
                and not self.stop_requested
                and time.time() - self.started_ts < self.max_runtime_sec
            ):
                await self.tick()
                await asyncio.sleep(POLL_SEC)

            if self.position is not None:
                await self._settle_position(self.position.market, "runner stopped")
            report = self._report()
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_text(
                json.dumps(report, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
            print(json.dumps(report, indent=2, default=str), flush=True)
            return report
        finally:
            await self.feed.stop()
            await self.poly.close()

    async def refresh_markets(self) -> None:
        markets = await self.poly.list_active_btc_updown_markets(
            self.cfg.polymarket.market_filter.slug_prefix
        )
        self.markets = {market.condition_id: market for market in markets}
        self.last_market_refresh = time.time()

    def _candidate(self) -> Optional[MarketInfo]:
        now = time.time()
        candidates = []
        for market in self.markets.values():
            end_ts = _parse_end_date(market.end_date)
            if end_ts is None:
                continue
            remaining = end_ts - now
            if remaining > 0:
                candidates.append((remaining, market))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    async def tick(self) -> None:
        now = time.time()
        if now - self.last_market_refresh >= MARKET_REFRESH_SEC or not self.markets:
            try:
                await self.refresh_markets()
            except Exception as exc:
                print(f"market refresh failed: {exc}", flush=True)
                return

        market = self.position.market if self.position else self._candidate()
        if market is None:
            return
        end_ts = _parse_end_date(market.end_date)
        if end_ts is None:
            return

        if self.position and end_ts <= now:
            await self._settle_position(market, "BTC market window ended")
            return

        try:
            up_book, down_book = await asyncio.gather(
                self.poly.get_orderbook(market.outcome_up_token_id),
                self.poly.get_orderbook(market.outcome_down_token_id),
            )
        except Exception as exc:
            print(f"orderbook fetch failed for {market.slug}: {exc}", flush=True)
            return

        current_entry = self.position.entry_price if self.position else None
        signal = self.strategy.evaluate(
            up_book,
            down_book,
            self.position.side if self.position else None,
            market_start_ts=market.start_ts,
            market_end_ts=end_ts,
            x_signal=XSignal(reason="live paper X confirmation disabled"),
            current_entry_price=current_entry,
        )

        if self.position:
            if signal.action == SignalAction.EXIT:
                await self._exit_position(market, up_book, down_book, signal.reason)
            return

        if signal.action not in (SignalAction.OPEN_UP, SignalAction.OPEN_DOWN):
            return

        side = "UP" if signal.action == SignalAction.OPEN_UP else "DOWN"
        book = up_book if side == "UP" else down_book
        ask = book.best_ask()
        if ask is None or ask.price <= 0:
            return
        size_usdc = self.cfg.risk.per_trade_size_usdc
        self.position = PaperPosition(
            market=market,
            side=side,
            entry_ts=time.time(),
            entry_price=ask.price,
            size_usdc=size_usdc,
            size_shares=size_usdc / ask.price,
            entry_signal=_signal_snapshot(signal, market),
        )
        print(
            f"paper OPEN {market.slug} {side} "
            f"ask={ask.price:.4f} shares={size_usdc / ask.price:.4f} "
            f"edge={signal.edge:+.4f}",
            flush=True,
        )

    async def _exit_position(
        self,
        market: MarketInfo,
        up_book: OrderBook,
        down_book: OrderBook,
        reason: str,
    ) -> None:
        if self.position is None:
            return
        book = up_book if self.position.side == "UP" else down_book
        bid = book.best_bid()
        if bid is None:
            return
        await self._close_at(
            market=market,
            exit_price=bid.price,
            reason=reason,
            outcome=None,
        )

    async def _settle_position(self, market: MarketInfo, reason: str) -> None:
        if self.position is None:
            return
        start_price = self.feed.price_at(
            market.start_ts, max_age_sec=self.cfg.strategy.max_market_start_age_sec
        )
        if start_price is None or start_price <= 0:
            start_price = self.position.entry_signal["btc_price"]
        up_won = _settlement_up(self.feed, start_price)
        outcome = "UP" if up_won else "DOWN"
        settlement = 1.0 if outcome == self.position.side else 0.0
        await self._close_at(
            market=market,
            exit_price=settlement,
            reason=reason,
            outcome=outcome,
        )

    async def _close_at(
        self,
        market: MarketInfo,
        exit_price: float,
        reason: str,
        outcome: Optional[str],
    ) -> None:
        position = self.position
        if position is None:
            return
        pnl = position.size_shares * (exit_price - position.entry_price)
        record = {
            "index": len(self.closed) + 1,
            "slug": market.slug,
            "condition_id": market.condition_id,
            "side": position.side,
            "entry_ts": position.entry_ts,
            "exit_ts": time.time(),
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "size_usdc": position.size_usdc,
            "size_shares": position.size_shares,
            "pnl_usdc": pnl,
            "exit_reason": reason,
            "settled_outcome": outcome,
            "entry_signal": position.entry_signal,
        }
        self.closed.append(record)
        self.position = None
        print(
            f"paper CLOSE {market.slug} {record['side']} "
            f"exit={exit_price:.4f} pnl=${pnl:+.4f} "
            f"reason={reason}",
            flush=True,
        )

    def _report(self) -> dict[str, Any]:
        pnl = sum(float(row["pnl_usdc"]) for row in self.closed)
        wins = sum(float(row["pnl_usdc"]) > 0 for row in self.closed)
        losses = sum(float(row["pnl_usdc"]) < 0 for row in self.closed)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": {
                "btc": "Binance + Coinbase live WebSocket consensus",
                "polymarket": "Gamma market metadata + public CLOB orderbooks",
                "orders_submitted": False,
                "telegram_started": False,
            },
            "strategy": {
                "raw_model_weight": self.cfg.strategy.calibration_raw_model_weight,
                "entry_edge_pct": self.cfg.strategy.entry_edge_pct,
                "per_trade_size_usdc": self.cfg.risk.per_trade_size_usdc,
                "max_open_positions": self.cfg.risk.max_open_positions,
            },
            "closed_trades": len(self.closed),
            "target_trades": self.target_trades,
            "complete": len(self.closed) >= self.target_trades,
            "elapsed_sec": time.time() - self.started_ts,
            "pnl_usdc": pnl,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(self.closed) if self.closed else 0.0,
            "avg_pnl_usdc": pnl / len(self.closed) if self.closed else 0.0,
            "trades": self.closed,
        }


async def async_main(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw["mode"] = "paper"
    raw.setdefault("x", {})["enabled"] = False
    raw.setdefault("telegram", {})["bot_token"] = "live-paper-no-telegram"
    raw["storage"] = {
        **raw.get("storage", {}),
        "sqlite_path": str(Path(args.out).with_suffix(".db")),
    }
    cfg = _build(raw)
    runner = LivePaperRunner(
        cfg=cfg,
        target_trades=args.trades,
        output=Path(args.out),
        max_runtime_sec=args.max_runtime_hours * 3600.0,
    )
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: setattr(runner, "stop_requested", True))
    loop.add_signal_handler(signal.SIGINT, lambda: setattr(runner, "stop_requested", True))
    report = await runner.run()
    return 0 if report["complete"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--trades", type=int, default=10)
    parser.add_argument("--out", default="data/live_paper_10.json")
    parser.add_argument("--max-runtime-hours", type=float, default=2.0)
    args = parser.parse_args()
    if args.trades <= 0:
        parser.error("--trades must be positive")
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())