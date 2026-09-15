import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.btc_feed import BtcPriceAggregator
from src.config import empty_paper_config
from src.polymarket_client import OrderBook, OrderBookLevel
from src.strategy import DivergenceStrategy, SignalAction


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