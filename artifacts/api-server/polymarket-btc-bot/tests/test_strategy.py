import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.btc_feed import BtcPriceAggregator
from src.calibration import calibrated_probability
from src.config import ConfigError, _build, empty_paper_config
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
    assert signal.our_prob_up == pytest.approx(
        calibrated_probability(
            0.5,
            0.49,
            market_logit_intercept=cfg.strategy.calibration_market_logit_intercept,
            market_logit_slope=cfg.strategy.calibration_market_logit_slope,
            raw_model_weight=cfg.strategy.calibration_raw_model_weight,
        )
    )
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


def test_strategy_refuses_stale_market_start_price():
    cfg = empty_paper_config()
    feed = BtcPriceAggregator(cfg.btc_feeds)
    now = time.time()
    observed_start = now - 40.0
    market_start = now - 20.0
    feed._history.append(observed_start, 60000.0)
    feed._binance_price = 60000.0
    feed._binance_ts = now
    feed._coinbase_price = 60000.0
    feed._coinbase_ts = now
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up"),
        _book("down"),
        current_position=None,
        market_start_ts=market_start,
        market_end_ts=now + 180.0,
    )

    assert signal.action == SignalAction.SKIP_NO_HISTORY
    assert "older than" in signal.reason


def test_strategy_accepts_fresh_tick_just_after_market_start():
    cfg = empty_paper_config()
    feed = BtcPriceAggregator(cfg.btc_feeds)
    now = time.time()
    market_start = now - 2.0
    feed._history.append(now - 1.0, 60000.0)
    feed._binance_price = 60000.0
    feed._binance_ts = now
    feed._coinbase_price = 60000.0
    feed._coinbase_ts = now
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up"),
        _book("down"),
        current_position=None,
        market_start_ts=market_start,
        market_end_ts=now + 180.0,
    )

    assert signal.action == SignalAction.HOLD
    assert "market start price" not in signal.reason


def test_strategy_refuses_crossed_orderbook():
    cfg = empty_paper_config()
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up", bid=0.60, ask=0.50),
        _book("down"),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=end_ts,
    )

    assert signal.action == SignalAction.HOLD
    assert "invalid or crossed" in signal.reason


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
    cfg.strategy.entry_edge_pct = 0.08
    cfg.strategy.x_min_base_entry_edge_pct = 0.06
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
    assert with_x.action == SignalAction.HOLD
    assert "no actionable edge" in with_x.reason
    assert "X boost" not in with_x.reason


def test_conflicting_fresh_x_signal_blocks_new_entry():
    cfg = empty_paper_config()
    cfg.strategy.entry_edge_pct = 0.08
    cfg.strategy.x_min_base_entry_edge_pct = 0.06
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
    assert "no actionable edge" in signal.reason


def _configured_strategy():
    """Read deployed strategy settings without wallet/secrets or network."""
    path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["telegram"]["bot_token"] = "offline-test-token"
    return _build(raw)


def test_configured_entry_thresholds_preserve_other_limits():
    cfg = _configured_strategy()
    assert cfg.strategy.entry_edge_pct == pytest.approx(0.10)
    assert cfg.strategy.x_min_base_entry_edge_pct == pytest.approx(0.07)
    assert cfg.x.min_base_entry_edge_pct == cfg.strategy.x_min_base_entry_edge_pct
    assert cfg.risk.per_trade_size_usdc == 1
    assert cfg.risk.max_total_exposure_usdc == 4
    assert cfg.risk.daily_loss_limit_usdc == 20
    assert cfg.risk.max_open_positions == 1
    assert cfg.strategy.real_entries_enabled is True
    assert cfg.strategy.exit_edge_pct == 0.015
    assert cfg.strategy.take_profit_pct == pytest.approx(0.30)
    assert cfg.strategy.take_profit_cost_buffer_pct == pytest.approx(0.02)
    assert cfg.strategy.adverse_edge_stop_pct == 0.10
    assert cfg.strategy.round_trip_cost_buffer_pct == 0.02
    assert cfg.strategy.x_confirming_edge_pct == 0.015
    assert cfg.strategy.x_min_confidence == 0.55
    assert cfg.strategy.min_seconds_remaining_for_entry == 150
    assert cfg.strategy.max_seconds_remaining_for_entry == 270
    assert cfg.strategy.calibration_market_logit_intercept == pytest.approx(0.0)
    assert cfg.strategy.calibration_market_logit_slope == pytest.approx(1.0)
    assert cfg.strategy.calibration_raw_model_weight == pytest.approx(0.60)


@pytest.mark.parametrize("take_profit_pct", [0.05, 1.0])
def test_take_profit_range_accepts_five_to_one_hundred_percent(
    take_profit_pct,
):
    raw = deepcopy(empty_paper_config().raw)
    raw["strategy"]["take_profit_pct"] = take_profit_pct

    cfg = _build(raw)

    assert cfg.strategy.take_profit_pct == take_profit_pct


@pytest.mark.parametrize("take_profit_pct", [0.0499, 1.0001])
def test_take_profit_range_rejects_values_outside_five_to_one_hundred_percent(
    take_profit_pct,
):
    raw = deepcopy(empty_paper_config().raw)
    raw["strategy"]["take_profit_pct"] = take_profit_pct

    with pytest.raises(ConfigError, match="take_profit_pct"):
        _build(raw)


@pytest.mark.parametrize("side", ["UP", "DOWN"])
@pytest.mark.parametrize(
    "ask,x_direction,remaining,opens",
    [
        (0.39, None, 180, False),      # Shrunk raw model does not overtrade.
        (0.397, None, 180, False),
        (0.409, "confirm", 180, False),
        (0.411, "confirm", 180, False),
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


def test_calibrated_value_gap_can_open_only_on_a_large_edge():
    cfg = _configured_strategy()
    feed, start_ts, _ = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    up_signal = strategy.evaluate(
        _book("up", bid=0.09, ask=0.10),
        _book("down", bid=0.89, ask=0.90),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=time.time() + 180,
    )
    down_signal = strategy.evaluate(
        _book("up", bid=0.29, ask=0.30),
        _book("down", bid=0.37, ask=0.38),
        current_position=None,
        market_start_ts=start_ts,
        market_end_ts=time.time() + 180,
    )

    # A large value gap can still create an executable edge, but the market
    # prior now dampens the raw model instead of treating it as certainty.
    assert up_signal.action == SignalAction.OPEN_UP
    assert down_signal.action == SignalAction.OPEN_DOWN


def test_exit_does_not_trigger_on_midpoint_edge_collapse_alone():
    cfg = empty_paper_config()
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up", bid=0.56, ask=0.58),
        _book("down", bid=0.42, ask=0.44),
        current_position="UP",
        market_start_ts=start_ts,
        market_end_ts=end_ts,
    )

    assert signal.action == SignalAction.HOLD


def test_exit_uses_executable_bid_against_calibrated_hold_value():
    cfg = empty_paper_config()
    cfg.strategy.calibration_market_logit_intercept = -0.20
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    signal = strategy.evaluate(
        _book("up", bid=0.66, ask=0.67),
        _book("down", bid=0.34, ask=0.35),
        current_position="UP",
        market_start_ts=start_ts,
        market_end_ts=end_ts,
    )

    assert signal.action == SignalAction.EXIT
    assert "sell bid exceeds calibrated hold value" in signal.reason


def test_take_profit_uses_entry_price_and_cost_buffer():
    cfg = empty_paper_config()
    cfg.strategy.exit_edge_pct = 0.50
    cfg.strategy.adverse_edge_stop_pct = 0.50
    feed, start_ts, end_ts = _feed()
    strategy = DivergenceStrategy(cfg.strategy, feed)

    profitable = strategy.evaluate(
        _book("up", bid=0.67, ask=0.68),
        _book("down", bid=0.42, ask=0.44),
        current_position="UP",
        market_start_ts=start_ts,
        market_end_ts=end_ts,
        current_entry_price=0.50,
    )
    not_yet_profitable = strategy.evaluate(
        _book("up", bid=0.64, ask=0.65),
        _book("down", bid=0.42, ask=0.44),
        current_position="UP",
        market_start_ts=start_ts,
        market_end_ts=end_ts,
        current_entry_price=0.50,
    )

    assert profitable.action == SignalAction.EXIT
    assert "take-profit target reached" in profitable.reason
    assert "net=32.00%" in profitable.reason
    assert "target=30.00%" in profitable.reason
    assert not_yet_profitable.action == SignalAction.HOLD