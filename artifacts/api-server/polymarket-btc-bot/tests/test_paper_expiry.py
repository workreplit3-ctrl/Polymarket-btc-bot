import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot import Orchestrator
from src.config import empty_paper_config
from src.engine import TradingEngine
from src.polymarket_client import MarketInfo, OrderBook, OrderBookLevel
from src.risk import Position, RiskManager
from src.storage import Storage


def _market(end_date: str = "2099-01-01T00:00:00Z") -> MarketInfo:
    return MarketInfo(
        condition_id="condition-1",
        question="BTC up or down?",
        slug="btc-updown-5m-test",
        end_date=end_date,
        outcome_up_token_id="up-token",
        outcome_down_token_id="down-token",
        outcomes=["Up", "Down"],
        volume=1000.0,
        active=True,
    )


class NoOrderPolymarket:
    def __init__(self):
        self.order_calls = 0
        self.book_calls = 0

    async def place_order(self, **kwargs):
        self.order_calls += 1
        raise AssertionError("paper expiry must not place an order")

    async def get_orderbook(self, token_id):
        self.book_calls += 1
        raise AssertionError("expired market should be settled before book fetch")


def _paper_engine(tmp_path, poly=None):
    cfg = empty_paper_config()
    storage = Storage(str(tmp_path / "trades.db"))
    risk = RiskManager(cfg.risk)
    return (
        TradingEngine(cfg, poly or NoOrderPolymarket(), risk, storage),
        risk,
        storage,
    )


def _position(market: MarketInfo, mark_price: float = 0.61) -> Position:
    return Position(
        market_condition_id=market.condition_id,
        market_slug=market.slug,
        side="UP",
        token_id=market.outcome_up_token_id,
        entry_price=0.50,
        size_shares=10.0,
        size_usdc=5.0,
        entry_ts=1.0,
        mark_price=mark_price,
    )


def test_expiry_settles_at_last_mark_once_without_order(tmp_path):
    poly = NoOrderPolymarket()
    engine, risk, storage = _paper_engine(tmp_path, poly)
    market = _market()
    risk.add_position(_position(market, mark_price=0.61))

    event = asyncio.run(
        engine.expire_paper_position(market, 0.61, "market expired")
    )
    repeated = asyncio.run(
        engine.expire_paper_position(market, 0.61, "market expired")
    )

    trades = asyncio.run(storage.recent_trades())
    assert event.action == "CLOSE"
    assert event.reason == "market expired"
    assert event.order_status == "settled"
    assert event.price == 0.61
    assert event.pnl == pytest.approx(1.1)
    assert repeated is None
    assert risk.state.open_positions == {}
    assert len(trades) == 1
    assert trades[0]["price"] == 0.61
    assert trades[0]["order_status"] == "settled"
    assert poly.order_calls == 0


def test_orchestrator_settles_expired_market_before_fetching_books(tmp_path):
    market = _market("2000-01-01T00:00:00Z")
    poly = NoOrderPolymarket()
    engine, risk, storage = _paper_engine(tmp_path, poly)
    risk.add_position(_position(market, mark_price=0.58))

    orch = object.__new__(Orchestrator)
    orch.cfg = empty_paper_config()
    orch.engine = engine
    orch.storage = storage
    orch.risk = risk
    orch.tracked_markets = [market]
    orch._known_markets = {market.condition_id: market}
    orch._latest_market_ids = {market.condition_id}
    orch.tg = None

    events = asyncio.run(orch._close_expired_paper_positions())

    assert [event.reason for event in events] == ["market expired"]
    assert events[0].price == 0.58
    assert orch.tracked_markets == []
    assert risk.state.open_positions == {}
    assert poly.book_calls == 0


def test_missing_active_market_is_settled_as_no_longer_tradable(tmp_path):
    market = _market()
    engine, risk, storage = _paper_engine(tmp_path)
    risk.add_position(_position(market, mark_price=0.47))

    orch = object.__new__(Orchestrator)
    orch.cfg = empty_paper_config()
    orch.engine = engine
    orch.storage = storage
    orch.risk = risk
    orch.tracked_markets = []
    orch._known_markets = {market.condition_id: market}
    orch._latest_market_ids = set()

    events = asyncio.run(orch._close_expired_paper_positions())

    assert [event.reason for event in events] == ["market no longer tradable"]
    assert events[0].price == 0.47
    with sqlite3.connect(storage.path) as conn:
        row = conn.execute(
            "SELECT action, price, raw FROM trades"
        ).fetchone()
    assert row[0] == "CLOSE"
    assert row[1] == 0.47
    assert '"market no longer tradable"' in row[2]


def test_expiry_helper_is_inert_in_real_mode(tmp_path):
    cfg = empty_paper_config()
    cfg.mode = "real"
    storage = Storage(str(tmp_path / "trades.db"))
    risk = RiskManager(cfg.risk)
    engine = TradingEngine(cfg, NoOrderPolymarket(), risk, storage)
    market = _market("2000-01-01T00:00:00Z")
    risk.add_position(_position(market, mark_price=0.58))

    event = asyncio.run(
        engine.expire_paper_position(market, 0.58, "market expired")
    )

    assert event is None
    assert list(risk.state.open_positions) == ["condition-1"]
    assert asyncio.run(storage.recent_trades()) == []