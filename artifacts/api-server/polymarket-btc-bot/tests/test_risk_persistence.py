import asyncio

from src.config import empty_paper_config
from src.risk import RiskManager
from src.storage import Storage


def test_persisted_risk_state_restores_daily_loss_and_cooldowns(tmp_path):
    cfg = empty_paper_config()
    storage = Storage(str(tmp_path / "risk.db"))
    asyncio.run(
        storage.log_trade(
            mode="real",
            condition_id="condition-1",
            slug="btc-test",
            side="UP",
            action="CLOSE",
            price=0.2,
            size_shares=10,
            size_usdc=5,
            pnl=-2.5,
        )
    )

    persisted = asyncio.run(
        storage.persisted_risk_state("real", cfg.risk.day_timezone)
    )
    risk = RiskManager(cfg.risk)
    risk.restore_persisted_state(
        daily_pnl=persisted["daily_pnl"],
        last_exit_ts=persisted["last_exit_ts"],
        last_loss_ts=persisted["last_loss_ts"],
    )

    assert risk.state.daily_pnl == -2.5
    assert risk.state.last_exit_ts > 0
    assert risk.state.last_loss_ts == risk.state.last_exit_ts