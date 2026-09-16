import sys
import time
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.btc_feed import BtcPriceAggregator
from src.config import _build, empty_paper_config
from src.polymarket_client import OrderBook, OrderBookLevel
from src.strategy import DivergenceStrategy, SignalAction
from src.x_feed import XSignal


def _book(token_id: str, bid: float = 0.48, ask: float = 0.50) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(bid, 100.0)],
        asks=[OrderBookLevel(ask, 100.0)],
        ts=time.time(),
    )


def _feed() -> tuple[BtcPriceAggregator, float, float]:
    cfg = empty_paper_config()
    feed = BtcPriceAggregator(cfg.btc_feeds)
    now = time.time()
    start_ts = now - 100.0
    for i in range(121):
        ts = now - 120.0 + i
        feed._history.append(ts, 60000.0)
    feed._binance_price = 60000.0
    feed._binance_ts = now
    feed._coinbase_price = 60000.0
    feed._coinbase_ts = now
    return feed, start_ts, now + 180.0


def test_value_model_uses_market_start_and_remaining_time():
    cfg = empty_paper_config()
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up"),
        _book("down"),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=end_ts,
    )

    assert signal.action == SignalAction.HOLD
    assert signal.our_prob_up == 0.5
    assert "no actionable edge" in signal.reason


def test_strategy_refuses_generic_lookback_without_market_start():
    cfg = empty_paper_config()
    feed, _start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up"),
        _book("down"),
        current_position=None,
        market_end_ts=end_ts,
    )

    assert signal.action == SignalAction.SKIP_NO_HISTORY
    assert "market start price" in signal.reason


def test_strategy_refuses_entries_too_close_to_resolution():
    cfg = empty_paper_config()
    feed, start_ts, _end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up", bid=0.30, ask=0.31),
        _book("down", bid=0.30, ask=0.31),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=time.time() + 60.0,
    )

    assert signal.action == SignalAction.HOLD
    assert "outside entry window" in signal.reason


def test_x_can_confirm_only_a_near_threshold_model_edge():
    cfg = empty_paper_config()
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    without_x = strategy.evaluate(
        _book("up", bid=0.36, ask=0.375),
        _book("down", bid=0.58, ask=0.60),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=end_ts,
    )
    with_x = strategy.evaluate(
        _book("up", bid=0.36, ask=0.375),
        _book("down", bid=0.58, ask=0.60),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=end_ts,
        x_signal=XSignal(
            direction="UP",
            confidence=1.0,
            post_count=2,
            valid=True,
            reason="test confirmation",
        ),
    )

    assert without_x.action == SignalAction.HOLD
    assert with_x.action == SignalAction.OPEN_UP
    assert "X boost" in with_x.reason


def test_conflicting_fresh_x_signal_blocks_new_entry():
    cfg = empty_paper_config()
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up", bid=0.36, ask=0.375),
        _book("down", bid=0.58, ask=0.60),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=end_ts,
        x_signal=XSignal(
            direction="DOWN",
            confidence=1.0,
            post_count=2,
            valid=True,
            reason="test conflict",
        ),
    )

    assert signal.action == SignalAction.HOLD
    assert "conflicts with best side" in signal.reason


def _configured_strategy():
    """Read deployed strategy settings without wallet/secrets or network."""
    path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["telegram"]["bot_token"] = "offline-test-token"
    return _build(raw)


def test_configured_entry_thresholds_preserve_other_limits():
    cfg = _configured_strategy()
    assert cfg.strategy.entry_edge_pct == pytest.approx(0.12 * 0.7)
    assert cfg.strategy.x_min_base_entry_edge_pct == pytest.approx(0.10 * 0.7)
    assert cfg.x.min_base_entry_edge_pct == cfg.strategy.x_min_base_entry_edge_pct
    assert cfg.risk.per_trade_size_usdc == 4
    assert cfg.risk.max_total_exposure_usdc == 4
    assert cfg.risk.daily_loss_limit_usdc == 4
    assert cfg.risk.max_open_positions == 1
    assert cfg.strategy.exit_edge_pct == 0.015
    assert cfg.strategy.adverse_edge_stop_pct == 0.10
    assert cfg.strategy.round_trip_cost_buffer_pct == 0.02
    assert cfg.strategy.x_confirming_edge_pct == 0.015
    assert cfg.strategy.x_min_confidence == 0.55
    assert cfg.strategy.min_seconds_remaining_for_entry == 150
    assert cfg.strategy.max_seconds_remaining_for_entry == 270


@pytest.mark.parametrize("side", ["UP", "DOWN"])
@pytest.mark.parametrize(
    "ask,x_direction,remaining,opens",
    [
        (0.39, None, 180, True),       # 9% net: passes 8.4%, not the old 12%.
        (0.397, None, 180, False),     # 8.3% net: below the new threshold.
        (0.409, "confirm", 180, True), # 7.1% base + 1.5% X confirmation.
        (0.411, "confirm", 180, False),# Base below 7%: X cannot qualify it.
        (0.39, "conflict", 180, False),
        (0.39, None, 60, False),      # Entry window is unchanged.
        (0.39, None, 280, False),
    ],
)
def test_configured_entry_behavior(side, ask, x_direction, remaining, opens):
    cfg = _configured_strategy()
    feed, start_ts, _ = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)
    target = _book(side, bid=ask - 0.01, ask=ask)
    other = _book("other", bid=0.58, ask=0.60)
    x_signal = None
    if x_direction:
        direction = side if x_direction == "confirm" else (
            "DOWN" if side == "UP" else "UP"
        )
        x_signal = XSignal(
            direction=direction, confidence=1.0, post_count=2,
            valid=True, reason="offline test",
        )
    signal = strategy.evaluate(
        target if side == "UP" else other,
        other if side == "UP" else target,
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=time.time() + remaining,
        x_signal=x_signal,
    )
    expected = (
        SignalAction.OPEN_UP if side == "UP" else SignalAction.OPEN_DOWN
    ) if opens else SignalAction.HOLD
    assert signal.action == expected