import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import empty_paper_config
from src.engine import TradingEngine
from src.polymarket_client import MarketInfo, OrderBook, OrderBookLevel
from src.risk import Position, RiskManager
from src.storage import Storage
from src.strategy import Signal, SignalAction


class FakePolymarket:
    def __init__(self, receipt):
        self.receipt = receipt

    async def place_order(self, **kwargs):
        return self.receipt


def _market() -> MarketInfo:
    return MarketInfo(
        condition_id="condition-1",
        question="BTC up or down?",
        slug="btc-test",
        end_date="2099-01-01T00:00:00Z",
        outcome_up_token_id="up-token",
        outcome_down_token_id="down-token",
        outcomes=["Up", "Down"],
        volume=1000.0,
        active=True,
    )


def _book() -> OrderBook:
    return OrderBook(
        token_id="up-token",
        bids=[OrderBookLevel(0.49, 100.0)],
        asks=[OrderBookLevel(0.50, 100.0)],
        ts=1.0,
    )


def _signal(action: SignalAction) -> Signal:
    return Signal(
        action=action, btc_price=60000.0, btc_drift_pct=0.1,
        our_prob_up=0.7, market_prob_up=0.5, edge=0.2, abs_edge=0.2,
        reason="test", ts=1.0,
    )


def _real_engine(tmp_path, receipt):
    cfg = empty_paper_config()
    cfg.mode = "real"
    storage = Storage(str(tmp_path / "trades.db"))
    risk = RiskManager(cfg.risk)
    return TradingEngine(cfg, FakePolymarket(receipt), risk, storage), risk, storage


def test_unfilled_real_order_does_not_open_position_or_trade(tmp_path):
    engine, risk, storage = _real_engine(
        tmp_path,
        {"orderID": "accepted-order", "order_status": "accepted", "filled_size": 0},
    )

    event = asyncio.run(
        engine.execute(_signal(SignalAction.OPEN_UP), _market(), _book(), _book())
    )

    assert event.action == "SKIP"
    assert event.order_status == "accepted"
    assert risk.state.open_positions == {}
    assert asyncio.run(storage.recent_trades()) == []


def test_partial_real_entry_tracks_only_filled_size(tmp_path):
    engine, risk, storage = _real_engine(
        tmp_path,
        {
            "orderID": "partial-order",
            "order_status": "partially_filled",
            "filled_size": 10.0,
            "fill_price": 0.50,
        },
    )

    event = asyncio.run(
        engine.execute(_signal(SignalAction.OPEN_UP), _market(), _book(), _book())
    )

    position = risk.state.open_positions["condition-1"]
    trade = asyncio.run(storage.recent_trades())[0]
    assert event.action == "OPEN"
    assert event.order_status == "partially_filled"
    assert position.size_shares == 10.0
    assert position.size_usdc == 5.0
    assert trade["size_shares"] == 10.0
    assert trade["size_usdc"] == 5.0
    assert trade["order_status"] == "partially_filled"


def test_partial_real_exit_keeps_unfilled_position_exposure(tmp_path):
    engine, risk, storage = _real_engine(
        tmp_path,
        {
            "orderID": "partial-exit",
            "order_status": "partially_filled",
            "filled_size": 8.0,
            "fill_price": 0.55,
        },
    )
    market = _market()
    risk.add_position(Position(
        market_condition_id=market.condition_id,
        market_slug=market.slug,
        side="UP",
        token_id=market.outcome_up_token_id,
        entry_price=0.50,
        size_shares=20.0,
        size_usdc=10.0,
        entry_ts=1.0,
        mark_price=0.50,
    ))

    event = asyncio.run(
        engine.execute(_signal(SignalAction.EXIT), market, _book(), _book())
    )

    remaining = risk.state.open_positions["condition-1"]
    trade = asyncio.run(storage.recent_trades())[0]
    assert event.action == "CLOSE"
    assert event.size_usdc == 4.0
    assert remaining.size_shares == 12.0
    assert remaining.size_usdc == 6.0
    assert trade["size_shares"] == 8.0
    assert trade["order_status"] == "partially_filled"