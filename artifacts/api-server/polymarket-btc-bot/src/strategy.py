"""
Strategy: divergence between BTC price drift and Polymarket implied probability.

Idea:
- BTC markets on Polymarket resolve to "Up" if BTC price at resolution > price at start.
- The "Up" token price is essentially a market-implied probability of BTC going up over
  the market window.
- We compute our OWN implied probability from observed BTC drift over a reference window,
  using a logistic mapping.
- When |our_prob - market_prob| >= entry_edge, we open a position betting the gap closes.
- Exit when the gap collapses below exit_edge, or hits adverse_edge_stop against us.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .btc_feed import BtcPriceAggregator
from .config import StrategyCfg
from .logger import get_logger
from .polymarket_client import OrderBook

log = get_logger("strategy")


class SignalAction(str, Enum):
    OPEN_UP = "OPEN_UP"          # buy Up token
    OPEN_DOWN = "OPEN_DOWN"      # buy Down token
    EXIT = "EXIT"                # close current position
    HOLD = "HOLD"
    SKIP_VOLATILE = "SKIP_VOLATILE"
    SKIP_STALE_FEED = "SKIP_STALE_FEED"
    SKIP_NO_HISTORY = "SKIP_NO_HISTORY"


@dataclass
class Signal:
    action: SignalAction
    btc_price: float
    btc_drift_pct: float         # % drift over reference window
    our_prob_up: float           # 0..1
    market_prob_up: float        # 0..1 (from Up token mid)
    edge: float                  # our_prob_up - market_prob_up
    abs_edge: float
    reason: str
    ts: float


class DivergenceStrategy:
    def __init__(self, cfg: StrategyCfg, feed: BtcPriceAggregator):
        self.cfg = cfg
        self.feed = feed

    def _logistic_prob_up(self, drift_pct: float) -> float:
        """Map BTC drift % (e.g. +0.10 = +0.1%) to an implied P(Up) via logistic.

        With default k=8, ±0.1% drift gives ~70/30 probability.
        Formula: z = k * drift_pct  (drift_pct is in PERCENT, not fraction)
                 P(Up) = 1 / (1 + exp(-z))
        """
        z = self.cfg.drift_to_prob_k * drift_pct
        return 1.0 / (1.0 + math.exp(-z))

    def evaluate(
        self,
        up_book: OrderBook,
        down_book: OrderBook,
        current_position: Optional[str],  # "UP" | "DOWN" | None
    ) -> Signal:
        now = time.time()

        # --- consensus / staleness check -------------------------------
        consensus = self.feed.consensus()
        if consensus.is_stale or consensus.mid <= 0:
            return Signal(
                action=SignalAction.SKIP_STALE_FEED,
                btc_price=consensus.mid,
                btc_drift_pct=0.0,
                our_prob_up=0.0,
                market_prob_up=0.0,
                edge=0.0,
                abs_edge=0.0,
                reason=f"feed stale or missing (div={consensus.divergence_pct:.3f}%)",
                ts=now,
            )

        # --- reference price -------------------------------------------
        ref_price = self.feed.price_n_seconds_ago(self.cfg.reference_window_sec)
        if ref_price is None or ref_price <= 0:
            return Signal(
                action=SignalAction.SKIP_NO_HISTORY,
                btc_price=consensus.mid,
                btc_drift_pct=0.0,
                our_prob_up=0.0,
                market_prob_up=0.0,
                edge=0.0,
                abs_edge=0.0,
                reason=f"need more history (window={self.cfg.reference_window_sec}s)",
                ts=now,
            )

        drift_pct = (consensus.mid - ref_price) / ref_price * 100.0
        our_prob_up = self._logistic_prob_up(drift_pct)

        # --- market implied probability --------------------------------
        up_mid = up_book.mid()
        down_mid = down_book.mid()
        up_ask = up_book.best_ask()
        down_ask = down_book.best_ask()
        if up_mid is None or down_mid is None or up_ask is None or down_ask is None:
            return Signal(
                action=SignalAction.HOLD,
                btc_price=consensus.mid,
                btc_drift_pct=drift_pct,
                our_prob_up=our_prob_up,
                market_prob_up=up_mid if up_mid is not None else 0.0,
                edge=0.0,
                abs_edge=0.0,
                reason="orderbook one-sided",
                ts=now,
            )
        # The "Up" token mid IS the market's probability of Up.
        market_prob_up = up_mid
        edge = our_prob_up - market_prob_up
        abs_edge = abs(edge)
        # A new position pays the ask, not the midpoint. Use actionable edge
        # for entries so the bid/ask spread cannot look like alpha.
        buy_edge_up = our_prob_up - up_ask.price
        buy_edge_down = (1.0 - our_prob_up) - down_ask.price

        # --- volatility filter ----------------------------------------
        vol = self.feed.volatility_60s()
        if vol > self.cfg.max_volatility_60s:
            return Signal(
                action=SignalAction.SKIP_VOLATILE,
                btc_price=consensus.mid,
                btc_drift_pct=drift_pct,
                our_prob_up=our_prob_up,
                market_prob_up=market_prob_up,
                edge=edge,
                abs_edge=abs_edge,
                reason=f"volatility too high ({vol:.5f} > {self.cfg.max_volatility_60s})",
                ts=now,
            )

        # --- exit logic (if we have a position) -----------------------
        if current_position == "UP":
            # We hold Up. Edge against us means market now thinks Up less likely than we do.
            if edge <= -self.cfg.adverse_edge_stop_pct:
                return Signal(action=SignalAction.EXIT, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                              market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                              reason=f"adverse edge stop ({edge:+.4f})", ts=now)
            if abs_edge <= self.cfg.exit_edge_pct:
                return Signal(action=SignalAction.EXIT, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                              market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                              reason=f"edge collapsed ({abs_edge:.4f})", ts=now)
            return Signal(action=SignalAction.HOLD, btc_price=consensus.mid,
                          btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                          market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                          reason="holding UP", ts=now)

        if current_position == "DOWN":
            # We hold Down. Edge FOR Up means against Down.
            if edge >= self.cfg.adverse_edge_stop_pct:
                return Signal(action=SignalAction.EXIT, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                              market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                              reason=f"adverse edge stop ({edge:+.4f})", ts=now)
            if abs_edge <= self.cfg.exit_edge_pct:
                return Signal(action=SignalAction.EXIT, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                              market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                              reason=f"edge collapsed ({abs_edge:.4f})", ts=now)
            return Signal(action=SignalAction.HOLD, btc_price=consensus.mid,
                          btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                          market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                          reason="holding DOWN", ts=now)

        # --- entry logic (flat) ---------------------------------------
        if max(buy_edge_up, buy_edge_down) >= self.cfg.entry_edge_pct:
            if buy_edge_up >= buy_edge_down:
                # Our prob of Up is higher than market → buy Up
                return Signal(action=SignalAction.OPEN_UP, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                              market_prob_up=market_prob_up, edge=buy_edge_up,
                              abs_edge=buy_edge_up,
                              reason=f"buy edge +{buy_edge_up:.4f} after ask → buy UP",
                              ts=now)
            else:
                return Signal(action=SignalAction.OPEN_DOWN, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                              market_prob_up=market_prob_up, edge=-buy_edge_down,
                              abs_edge=buy_edge_down,
                              reason=f"buy edge +{buy_edge_down:.4f} after ask → buy DOWN",
                              ts=now)

        return Signal(action=SignalAction.HOLD, btc_price=consensus.mid,
                      btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                      market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                      reason=(
                          f"no actionable edge "
                          f"(UP={buy_edge_up:.4f}, DOWN={buy_edge_down:.4f}, "
                          f"threshold={self.cfg.entry_edge_pct})"
                      ), ts=now)
