import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot import Orchestrator
from src.config import empty_paper_config
from src.polymarket_client import MarketInfo, OrderBook, OrderBookLevel
from src.risk import RiskManager
from src.storage import Storage
from src.strategy import Signal, SignalAction


def _market() -> MarketInfo:
    return MarketInfo(
        condition_id="condition-1",
        question="BTC up or down?",
        slug="btc-updown-5m-test",
        end_date="2099-01-01T00:00:00Z",
        outcome_up_token_id="up-token",
        outcome_down_token_id="down-token",
        outcomes=["Up", "Down"],
        volume=1000.0,
        active=True,
    )


def _book(token_id: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[OrderBookLevel(bid, 100.0)],
        asks=[OrderBookLevel(ask, 100.0)],
        ts=1.0,
    )


def test_paper_tick_persists_actionable_edges_for_both_sides(tmp_path):
    cfg = empty_paper_config()
    storage = Storage(str(tmp_path / "reporting.db"))
    market = _market()
    up_book = _book("up-token", bid=0.49, ask=0.55)
    down_book = _book("down-token", bid=0.28, ask=0.31)

    class FakePolymarket:
        async def get_orderbook(self, token_id):
            return up_book if token_id == "up-token" else down_book

    class FakeStrategy:
        def evaluate(self, up, down, current_side):
            return Signal(
                action=SignalAction.HOLD,
                btc_price=60000.0,
                btc_drift_pct=0.1,
                our_prob_up=0.73,
                market_prob_up=0.50,
                edge=0.23,
                abs_edge=0.23,
                reason="test",
                ts=1.0,
            )

    orch = object.__new__(Orchestrator)
    orch.cfg = cfg
    orch.storage = storage
    orch.poly = FakePolymarket()
    orch.strategy = FakeStrategy()
    orch.risk = RiskManager(cfg.risk)
    orch.tracked_markets = [market]
    orch._market_refresh_ts = time.time()
    orch._market_refresh_interval = 300
    orch.paused = False
    orch._pause_file = tmp_path / "control.json"
    orch._pause_flag = tmp_path / "paused.flag"
    orch._real_entries_paused = False

    asyncio.run(orch._tick())

    with sqlite3.connect(storage.path) as conn:
        row = conn.execute(
            "SELECT actionable_edge_up, actionable_edge_down FROM signals"
        ).fetchone()

    assert row[0] == pytest.approx(0.18)
    assert row[1] == pytest.approx(-0.04)


def test_paper_summary_counts_only_closed_paper_trades_since_start(tmp_path):
    storage = Storage(str(tmp_path / "reporting.db"))
    since_ts = 100.0

    for trade in (
        # Before the requested run.
        {"ts": 99.0, "mode": "paper", "action": "CLOSE", "pnl": 9.0},
        # Included paper close.
        {"ts": 100.0, "mode": "paper", "action": "CLOSE", "pnl": 0.75},
        # Open trades and real closes must not be counted.
        {"ts": 101.0, "mode": "paper", "action": "OPEN", "pnl": 100.0},
        {"ts": 102.0, "mode": "real", "action": "CLOSE", "pnl": 100.0},
        {"ts": 103.0, "mode": "paper", "action": "CLOSE", "pnl": -0.25},
    ):
        asyncio.run(storage.log_trade(**trade))

    summary = asyncio.run(storage.paper_summary(since_ts))

    assert summary == {
        "closed_trades": 2,
        "pnl_usdc": 0.5,
        "exit_reasons": {"unknown": 2},
    }


def test_paper_summary_aggregates_persisted_pnl_and_exit_reasons_without_positions(
    tmp_path,
):
    storage = Storage(str(tmp_path / "reporting.db"))

    for trade in (
        {
            "ts": 200.0,
            "mode": "paper",
            "action": "CLOSE",
            "pnl": 1.25,
            "raw": {"reason": "take profit"},
        },
        {
            "ts": 201.0,
            "mode": "paper",
            "action": "CLOSE",
            "pnl": -0.5,
            "raw": {"reason": "adverse stop"},
        },
        {
            "ts": 202.0,
            "mode": "paper",
            "action": "CLOSE",
            "pnl": 0.25,
            "raw": {"reason": "take profit"},
        },
    ):
        asyncio.run(storage.log_trade(**trade))

    # Reopen the database through a new storage instance. No in-memory
    # positions are created; the report must come entirely from persisted rows.
    reopened = Storage(storage.path)
    summary = asyncio.run(reopened.paper_summary(200.0))

    assert summary["closed_trades"] == 3
    assert summary["pnl_usdc"] == 1.0
    assert summary["exit_reasons"] == {
        "take profit": 2,
        "adverse stop": 1,
    }