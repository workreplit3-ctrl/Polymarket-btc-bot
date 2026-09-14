"""
Trading engine — translates strategy signals into either simulated (paper)
or real (CLOB) orders, and keeps the risk manager in sync.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .logger import get_logger
from .polymarket_client import MarketInfo, OrderBook, PolymarketClient
from .risk import Position, RiskManager
from .storage import Storage
from .strategy import Signal, SignalAction

log = get_logger("engine")


@dataclass
class TradeEvent:
    action: str       # "OPEN" | "CLOSE" | "SKIP" | "HOLD"
    side: str         # "UP" | "DOWN" | ""
    slug: str
    price: float
    size_usdc: float
    pnl: float
    reason: str


class TradingEngine:
    """Unified engine for paper and real modes. The only difference is whether
    `place_order` actually hits the CLOB or just records a fill at the best ask.
    """

    def __init__(self, cfg: Config, poly: PolymarketClient,
                 risk: RiskManager, storage: Storage):
        self.cfg = cfg
        self.poly = poly
        self.risk = risk
        self.storage = storage
        self.paper_balance = 1000.0  # notional paper starting balance

    # ----- main entrypoint -----------------------------------------
    async def execute(self, signal: Signal, market: MarketInfo,
                      up_book: OrderBook, down_book: OrderBook) -> Optional[TradeEvent]:
        action = signal.action
        side_token = lambda side: (market.outcome_up_token_id if side == "UP"
                                   else market.outcome_down_token_id)
        side_book = lambda side: (up_book if side == "UP" else down_book)

        if action == SignalAction.OPEN_UP or action == SignalAction.OPEN_DOWN:
            side = "UP" if action == SignalAction.OPEN_UP else "DOWN"
            return await self._open(signal, market, side, side_book(side))

        if action == SignalAction.EXIT:
            return await self._exit_current(signal, market, up_book, down_book)

        # Non-trade actions
        return TradeEvent(
            action="SKIP" if action != SignalAction.HOLD else "HOLD",
            side="", slug=market.slug, price=signal.market_prob_up,
            size_usdc=0.0, pnl=0.0, reason=signal.reason,
        )

    # ----- open ----------------------------------------------------
    async def _open(self, signal: Signal, market: MarketInfo, side: str,
                    book: OrderBook) -> Optional[TradeEvent]:
        size_usdc = self.cfg.risk.per_trade_size_usdc
        ok, reason_code, msg = self.risk.check_entry(size_usdc, book)
        if not ok:
            log.info(f"entry rejected [{reason_code.value}] {msg}")
            return TradeEvent(action="SKIP", side=side, slug=market.slug,
                              price=book.mid() or 0.0, size_usdc=size_usdc,
                              pnl=0.0, reason=f"{reason_code.value}: {msg}")

        ba = book.best_ask()
        if ba is None:
            return TradeEvent(action="SKIP", side=side, slug=market.slug,
                              price=0.0, size_usdc=size_usdc, pnl=0.0,
                              reason="no ask")
        fill_price = ba.price
        size_shares = size_usdc / fill_price if fill_price > 0 else 0.0

        order_id = ""
        if self.cfg.mode == "real":
            try:
                receipt = await self.poly.place_order(
                    token_id=(market.outcome_up_token_id if side == "UP"
                              else market.outcome_down_token_id),
                    side="BUY", price=fill_price, size=size_shares,
                )
                order_id = str(receipt.get("orderID") or receipt.get("id") or "")
            except Exception as e:
                log.error(f"real order placement failed: {e}")
                return TradeEvent(action="SKIP", side=side, slug=market.slug,
                                  price=fill_price, size_usdc=size_usdc, pnl=0.0,
                                  reason=f"order failed: {e}")

        pos = Position(
            market_condition_id=market.condition_id,
            market_slug=market.slug,
            side=side,
            token_id=(market.outcome_up_token_id if side == "UP"
                      else market.outcome_down_token_id),
            entry_price=fill_price,
            size_shares=size_shares,
            size_usdc=size_usdc,
            entry_ts=time.time(),
            mark_price=fill_price,
        )
        self.risk.add_position(pos)
        await self.storage.log_trade(
            mode=self.cfg.mode, condition_id=market.condition_id,
            slug=market.slug, side=side, action="OPEN",
            price=fill_price, size_shares=size_shares, size_usdc=size_usdc,
            order_id=order_id, raw={"signal_reason": signal.reason},
        )
        if self.cfg.mode == "paper":
            self.paper_balance -= size_usdc
        return TradeEvent(action="OPEN", side=side, slug=market.slug,
                          price=fill_price, size_usdc=size_usdc, pnl=0.0,
                          reason=signal.reason)

    # ----- exit ----------------------------------------------------
    async def _exit_current(self, signal: Signal, market: MarketInfo,
                            up_book: OrderBook, down_book: OrderBook) -> Optional[TradeEvent]:
        pos = self.risk.state.open_positions.get(market.condition_id)
        if pos is None:
            return TradeEvent(action="SKIP", side="", slug=market.slug,
                              price=signal.market_prob_up, size_usdc=0.0, pnl=0.0,
                              reason="no open position to exit")
        book = up_book if pos.side == "UP" else down_book
        bb = book.best_bid()
        if bb is None:
            return TradeEvent(action="SKIP", side=pos.side, slug=market.slug,
                              price=0.0, size_usdc=pos.size_usdc, pnl=0.0,
                              reason="no bid to exit into")
        fill_price = bb.price

        order_id = ""
        if self.cfg.mode == "real":
            try:
                receipt = await self.poly.place_order(
                    token_id=pos.token_id, side="SELL",
                    price=fill_price, size=pos.size_shares,
                )
                order_id = str(receipt.get("orderID") or receipt.get("id") or "")
            except Exception as e:
                log.error(f"real sell failed: {e}")
                return TradeEvent(action="SKIP", side=pos.side, slug=market.slug,
                                  price=fill_price, size_usdc=pos.size_usdc, pnl=0.0,
                                  reason=f"sell failed: {e}")

        closed = self.risk.close_position(market.condition_id, fill_price)
        if closed is None:
            return None
        await self.storage.log_trade(
            mode=self.cfg.mode, condition_id=market.condition_id,
            slug=market.slug, side=closed.side, action="CLOSE",
            price=fill_price, size_shares=closed.size_shares,
            size_usdc=closed.size_usdc, pnl=closed.pnl_usdc,
            order_id=order_id, raw={"reason": signal.reason},
        )
        if self.cfg.mode == "paper":
            self.paper_balance += closed.size_usdc + closed.pnl_usdc
        return TradeEvent(
            action="CLOSE", side=closed.side, slug=market.slug,
            price=fill_price, size_usdc=closed.size_usdc, pnl=closed.pnl_usdc,
            reason=signal.reason,
        )
