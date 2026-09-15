"""Risk manager — gate every entry/exit through configurable limits."""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, Iterable, List, Optional

from .config import RiskCfg
from .logger import get_logger
from .polymarket_client import OrderBook

log = get_logger("risk")


class RejectReason(str, Enum):
    OK = "OK"
    TOO_MANY_POSITIONS = "TOO_MANY_POSITIONS"
    DAILY_LOSS_HIT = "DAILY_LOSS_HIT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
    LOSS_COOLDOWN = "LOSS_COOLDOWN"
    POST_TRADE_COOLDOWN = "POST_TRADE_COOLDOWN"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"


@dataclass
class Position:
    market_condition_id: str
    market_slug: str
    side: str               # "UP" or "DOWN"
    token_id: str
    entry_price: float      # 0..1
    size_shares: float
    size_usdc: float
    entry_ts: float
    # current mark
    mark_price: float = 0.0
    # filled on exit
    exit_price: float = 0.0
    exit_ts: float = 0.0
    pnl_usdc: float = 0.0
    status: str = "OPEN"    # OPEN | CLOSED | LOST


@dataclass
class RiskState:
    open_positions: Dict[str, Position] = field(default_factory=dict)  # key = condition_id
    daily_pnl: float = 0.0
    daily_pnl_date: str = ""           # YYYY-MM-DD
    last_exit_ts: float = 0.0
    last_loss_ts: float = 0.0


class RiskManager:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg
        self.state = RiskState()

    # ----- daily reset ----------------------------------------------
    def _maybe_reset_daily(self) -> None:
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.state.daily_pnl_date != today:
            self.state.daily_pnl_date = today
            self.state.daily_pnl = 0.0

    # ----- position tracking ---------------------------------------
    def add_position(self, p: Position) -> None:
        self.state.open_positions[p.market_condition_id] = p
        log.info(f"position OPEN: {p.market_slug} {p.side} "
                 f"size={p.size_shares:.4f}@{p.entry_price:.4f} (${p.size_usdc:.2f})")

    def replace_open_positions(self, positions: Iterable[Position]) -> None:
        """Replace memory state with positions confirmed by the wallet."""
        self.state.open_positions.clear()
        for position in positions:
            self.add_position(position)
        log.info(
            f"risk reconciliation complete: {len(self.state.open_positions)} "
            "confirmed open position(s)"
        )

    def close_position(
        self, condition_id: str, exit_price: float,
        size_shares: Optional[float] = None,
    ) -> Optional[Position]:
        p = self.state.open_positions.get(condition_id)
        if p is None:
            return None

        close_shares = p.size_shares if size_shares is None else min(
            max(0.0, size_shares), p.size_shares
        )
        if close_shares <= 0:
            return None

        # Return a separate closed-trade record for a partial fill while
        # keeping the remaining actual shares exposed to risk.
        closed = replace(
            p,
            size_shares=close_shares,
            size_usdc=p.entry_price * close_shares,
            exit_price=exit_price,
            exit_ts=time.time(),
        )
        # PnL for a long position: shares * (exit - entry)
        closed.pnl_usdc = closed.size_shares * (exit_price - closed.entry_price)
        closed.status = "LOST" if closed.pnl_usdc < 0 else "CLOSED"
        remaining_shares = p.size_shares - close_shares
        if remaining_shares <= max(1e-9, p.size_shares * 1e-9):
            self.state.open_positions.pop(condition_id, None)
        else:
            p.size_shares = remaining_shares
            p.size_usdc = p.entry_price * remaining_shares

        self.state.last_exit_ts = closed.exit_ts
        if closed.pnl_usdc < 0:
            self.state.last_loss_ts = closed.exit_ts
        self._maybe_reset_daily()
        self.state.daily_pnl += closed.pnl_usdc
        log.info(f"position CLOSE: {p.market_slug} {p.side} "
                 f"shares={close_shares:.4f} exit={exit_price:.4f} "
                 f"pnl=${closed.pnl_usdc:.2f} "
                 f"(daily=${self.state.daily_pnl:.2f})")
        return closed

    def mark_positions(self, marks: Dict[str, float]) -> None:
        """Update mark prices for open positions. Key = condition_id."""
        for cid, p in self.state.open_positions.items():
            if cid in marks:
                p.mark_price = marks[cid]

    # ----- checks ---------------------------------------------------
    def check_entry(self, size_usdc: float, book: OrderBook) -> tuple[bool, RejectReason, str]:
        self._maybe_reset_daily()

        if len(self.state.open_positions) >= self.cfg.max_open_positions:
            return False, RejectReason.TOO_MANY_POSITIONS, (
                f"open={len(self.state.open_positions)} >= "
                f"{self.cfg.max_open_positions}"
            )

        if self.state.daily_pnl <= -abs(self.cfg.daily_loss_limit_usdc):
            return False, RejectReason.DAILY_LOSS_HIT, (
                f"daily PnL ${self.state.daily_pnl:.2f} hit "
                f"-${self.cfg.daily_loss_limit_usdc:.2f}"
            )

        total_exposure = sum(p.size_usdc for p in self.state.open_positions.values())
        if total_exposure + size_usdc > self.cfg.max_total_exposure_usdc:
            return False, RejectReason.EXPOSURE_LIMIT, (
                f"exposure ${total_exposure:.2f}+${size_usdc:.2f} > "
                f"${self.cfg.max_total_exposure_usdc:.2f}"
            )

        now = time.time()
        if now - self.state.last_loss_ts < self.cfg.loss_cooldown_sec:
            wait = self.cfg.loss_cooldown_sec - (now - self.state.last_loss_ts)
            return False, RejectReason.LOSS_COOLDOWN, f"loss cooldown {wait:.0f}s left"

        if now - self.state.last_exit_ts < self.cfg.post_trade_cooldown_sec:
            wait = self.cfg.post_trade_cooldown_sec - (now - self.state.last_exit_ts)
            return False, RejectReason.POST_TRADE_COOLDOWN, f"post-trade cooldown {wait:.0f}s left"

        # Liquidity / slippage check: ensure best ask has enough size,
        # and that ask price isn't too far from mid.
        ba = book.best_ask()
        mid = book.mid()
        if ba is None or mid is None:
            return False, RejectReason.INSUFFICIENT_LIQUIDITY, "no ask"
        if ba.size * mid < size_usdc * 0.5:
            return False, RejectReason.INSUFFICIENT_LIQUIDITY, (
                f"best ask size {ba.size:.2f} too thin"
            )
        slippage = ba.price - mid
        if slippage > self.cfg.max_slippage_cents:
            return False, RejectReason.SLIPPAGE_TOO_HIGH, (
                f"slip {slippage:.4f} > {self.cfg.max_slippage_cents:.4f}"
            )

        return True, RejectReason.OK, "ok"

    def can_exit(self) -> bool:
        return True  # always allow exits

    # ----- snapshot -------------------------------------------------
    def snapshot(self) -> dict:
        self._maybe_reset_daily()
        return {
            "open_positions": len(self.state.open_positions),
            "total_exposure_usdc": sum(p.size_usdc for p in self.state.open_positions.values()),
            "daily_pnl_usdc": self.state.daily_pnl,
            "daily_loss_limit_usdc": self.cfg.daily_loss_limit_usdc,
            "last_loss_ts": self.state.last_loss_ts,
            "last_exit_ts": self.state.last_exit_ts,
            "positions": [
                {
                    "slug": p.market_slug,
                    "side": p.side,
                    "entry": p.entry_price,
                    "mark": p.mark_price,
                    "shares": p.size_shares,
                    "usdc": p.size_usdc,
                    "unrealized_pnl": (p.size_shares * (p.mark_price - p.entry_price))
                                      if p.mark_price else 0.0,
                }
                for p in self.state.open_positions.values()
            ],
        }
