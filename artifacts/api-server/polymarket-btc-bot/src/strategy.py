"""Value-based BTC Up/Down strategy.

The model estimates the probability that BTC finishes above the *specific
market's* start price.  It uses the observed distance to that start price and
the time remaining, rather than comparing BTC to an unrelated fixed lookback.
The volatility-to-probability mapping is deliberately conservative and real
entries remain disabled until it has been calibrated against resolved markets.
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
from .x_feed import XSignal

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
    btc_drift_pct: float         # % distance from this market's start price
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

    def _normal_prob_up(
        self,
        current_price: float,
        market_start_price: float,
        seconds_remaining: float,
        volatility_per_sec: float,
    ) -> float:
        """Estimate P(BTC close > market start) with a conservative normal model."""
        if seconds_remaining <= 0 or current_price <= 0:
            return 0.5
        sigma = (
            current_price
            * max(volatility_per_sec, self.cfg.min_volatility_per_sec)
            * math.sqrt(seconds_remaining)
        )
        if sigma <= 0:
            return 0.5
        z = (current_price - market_start_price) / sigma
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    def evaluate(
        self,
        up_book: OrderBook,
        down_book: OrderBook,
        current_position: Optional[str],  # "UP" | "DOWN" | None
        market_start_ts: Optional[float] = None,
        market_end_ts: Optional[float] = None,
        x_signal: Optional[XSignal] = None,
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

        # A market-specific start price is required. Falling back to a generic
        # 300-second lookback would recreate the original source of false edge.
        market_start_price = (
            self.feed.price_at(market_start_ts)
            if market_start_ts is not None
            else None
        )
        if market_start_price is None or market_start_price <= 0:
            return Signal(
                action=SignalAction.SKIP_NO_HISTORY,
                btc_price=consensus.mid,
                btc_drift_pct=0.0,
                our_prob_up=0.0,
                market_prob_up=0.0,
                edge=0.0,
                abs_edge=0.0,
                reason="market start price is not available in BTC history",
                ts=now,
            )

        seconds_remaining = (
            market_end_ts - now
            if market_end_ts is not None
            else float("nan")
        )
        if not math.isfinite(seconds_remaining) or seconds_remaining <= 0:
            return Signal(
                action=SignalAction.SKIP_NO_HISTORY,
                btc_price=consensus.mid,
                btc_drift_pct=0.0,
                our_prob_up=0.0,
                market_prob_up=0.0,
                edge=0.0,
                abs_edge=0.0,
                reason="market end time is unavailable or already passed",
                ts=now,
            )

        drift_pct = (consensus.mid - market_start_price) / market_start_price * 100.0
        our_prob_up = self._normal_prob_up(
            consensus.mid,
            market_start_price,
            seconds_remaining,
            self.feed.volatility_60s(),
        )

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
        entry_window_ok = (
            self.cfg.min_seconds_remaining_for_entry
            <= seconds_remaining
            <= self.cfg.max_seconds_remaining_for_entry
        )
        if not entry_window_ok:
            return Signal(
                action=SignalAction.HOLD,
                btc_price=consensus.mid,
                btc_drift_pct=drift_pct,
                our_prob_up=our_prob_up,
                market_prob_up=market_prob_up,
                edge=edge,
                abs_edge=abs_edge,
                reason=(
                    f"outside entry window "
                    f"({seconds_remaining:.0f}s; allowed "
                    f"{self.cfg.min_seconds_remaining_for_entry}-"
                    f"{self.cfg.max_seconds_remaining_for_entry}s)"
                ),
                ts=now,
            )

        if (
            up_ask.price < self.cfg.min_token_price
            or up_ask.price > self.cfg.max_token_price
            or down_ask.price < self.cfg.min_token_price
            or down_ask.price > self.cfg.max_token_price
        ):
            return Signal(
                action=SignalAction.HOLD,
                btc_price=consensus.mid,
                btc_drift_pct=drift_pct,
                our_prob_up=our_prob_up,
                market_prob_up=market_prob_up,
                edge=edge,
                abs_edge=abs_edge,
                reason=(
                    f"token price outside safe range "
                    f"[{self.cfg.min_token_price:.2f},"
                    f"{self.cfg.max_token_price:.2f}]"
                ),
                ts=now,
            )

        net_buy_edge_up = buy_edge_up - self.cfg.round_trip_cost_buffer_pct
        net_buy_edge_down = buy_edge_down - self.cfg.round_trip_cost_buffer_pct
        x_signal = x_signal or XSignal(reason="X not supplied")
        x_can_confirm = (
            x_signal.valid
            and x_signal.confidence >= self.cfg.x_min_confidence
            and max(net_buy_edge_up, net_buy_edge_down)
            >= self.cfg.x_min_base_entry_edge_pct
        )
        x_boost_up = (
            self.cfg.x_confirming_edge_pct * x_signal.confidence
            if x_can_confirm and x_signal.direction == "UP"
            else 0.0
        )
        x_boost_down = (
            self.cfg.x_confirming_edge_pct * x_signal.confidence
            if x_can_confirm and x_signal.direction == "DOWN"
            else 0.0
        )
        adjusted_edge_up = net_buy_edge_up + x_boost_up
        adjusted_edge_down = net_buy_edge_down + x_boost_down
        best_base_edge = max(net_buy_edge_up, net_buy_edge_down)
        if (
            x_can_confirm
            and x_signal.direction in ("UP", "DOWN")
            and x_signal.direction != (
                "UP" if net_buy_edge_up >= net_buy_edge_down else "DOWN"
            )
            and x_signal.confidence >= self.cfg.x_min_confidence
        ):
            return Signal(
                action=SignalAction.HOLD,
                btc_price=consensus.mid,
                btc_drift_pct=drift_pct,
                our_prob_up=our_prob_up,
                market_prob_up=market_prob_up,
                edge=edge,
                abs_edge=abs_edge,
                reason=f"X confirmation conflicts with best side ({x_signal.summary})",
                ts=now,
            )
        if max(adjusted_edge_up, adjusted_edge_down) >= self.cfg.entry_edge_pct:
            if adjusted_edge_up >= adjusted_edge_down:
                # Our prob of Up is higher than market → buy Up
                return Signal(action=SignalAction.OPEN_UP, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                               market_prob_up=market_prob_up, edge=adjusted_edge_up,
                               abs_edge=adjusted_edge_up,
                              reason=(
                                   f"net buy edge +{adjusted_edge_up:.4f} "
                                  f"(gross={buy_edge_up:.4f}, "
                                   f"cost buffer={self.cfg.round_trip_cost_buffer_pct:.4f}, "
                                   f"X boost={x_boost_up:.4f}) "
                                  "→ buy UP"
                              ),
                              ts=now)
            else:
                return Signal(action=SignalAction.OPEN_DOWN, btc_price=consensus.mid,
                              btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                               market_prob_up=market_prob_up, edge=-adjusted_edge_down,
                               abs_edge=adjusted_edge_down,
                              reason=(
                                   f"net buy edge +{adjusted_edge_down:.4f} "
                                  f"(gross={buy_edge_down:.4f}, "
                                   f"cost buffer={self.cfg.round_trip_cost_buffer_pct:.4f}, "
                                   f"X boost={x_boost_down:.4f}) "
                                  "→ buy DOWN"
                              ),
                              ts=now)

        return Signal(action=SignalAction.HOLD, btc_price=consensus.mid,
                      btc_drift_pct=drift_pct, our_prob_up=our_prob_up,
                      market_prob_up=market_prob_up, edge=edge, abs_edge=abs_edge,
                      reason=(
                          f"no actionable edge "
                           f"(net UP={adjusted_edge_up:.4f}, "
                           f"net DOWN={adjusted_edge_down:.4f}, "
                           f"base max={best_base_edge:.4f}, "
                           f"threshold={self.cfg.entry_edge_pct}; "
                           f"X={x_signal.direction}/{x_signal.confidence:.2f})"
                      ), ts=now)
