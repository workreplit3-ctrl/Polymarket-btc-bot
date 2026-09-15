import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bot import Orchestrator
from src.config import empty_paper_config
from src.polymarket_client import MarketInfo, TokenHolding
from src.risk import Position, RiskManager


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


def _orchestrator_with_market(holdings):
    cfg = empty_paper_config()
    cfg.mode = "real"
    orch = object.__new__(Orchestrator)
    orch.cfg = cfg
    orch.tracked_markets = []
    orch.risk = RiskManager(cfg.risk)
    orch._real_entries_paused = True

    async def refresh_markets(raise_on_error=False):
        orch.tracked_markets = [_market()]

    class FakePolymarket:
        async def get_confirmed_token_holdings(self, markets):
            return holdings

    orch._refresh_markets = refresh_markets
    orch.poly = FakePolymarket()
    return orch


def test_reconciliation_replaces_risk_with_confirmed_holding() -> None:
    orch = _orchestrator_with_market([
        TokenHolding(
            condition_id="condition-1",
            token_id="up-token",
            size_shares=12.5,
            avg_price=0.42,
        )
    ])
    orch.risk.add_position(Position(
        market_condition_id="stale-condition",
        market_slug="stale",
        side="DOWN",
        token_id="stale-token",
        entry_price=0.5,
        size_shares=10.0,
        size_usdc=5.0,
        entry_ts=1.0,
    ))

    asyncio.run(orch._reconcile_real_positions())

    positions = orch.risk.state.open_positions
    assert list(positions) == ["condition-1"]
    assert positions["condition-1"].side == "UP"
    assert positions["condition-1"].size_shares == 12.5
    assert positions["condition-1"].size_usdc == 5.25
    assert orch._real_entries_paused is False


def test_failed_reconciliation_does_not_replace_risk_state() -> None:
    orch = _orchestrator_with_market([])
    original = Position(
        market_condition_id="condition-1",
        market_slug="btc-updown-5m-test",
        side="UP",
        token_id="up-token",
        entry_price=0.42,
        size_shares=12.5,
        size_usdc=5.25,
        entry_ts=1.0,
    )
    orch.risk.add_position(original)

    class UnavailablePolymarket:
        async def get_confirmed_token_holdings(self, markets):
            raise RuntimeError("wallet endpoint unavailable")

    orch.poly = UnavailablePolymarket()

    try:
        asyncio.run(orch._reconcile_real_positions())
    except RuntimeError as exc:
        assert str(exc) == "wallet endpoint unavailable"
    else:
        raise AssertionError("reconciliation should fail when wallet is unavailable")

    assert orch.risk.state.open_positions["condition-1"] is original
    assert orch._real_entries_paused is True