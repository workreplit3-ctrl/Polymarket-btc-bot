import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

from py_clob_client_v2 import ClobClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import empty_paper_config
from src.polymarket_client import PolymarketClient
from src.risk import RiskManager
from src.storage import Storage
from src.telegram_bot import TelegramBot


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs) -> None:
        self.replies.append(text)


class FakeOrchestrator:
    def __init__(self) -> None:
        self.poly = SimpleNamespace(
            validate_real_mode=PolymarketClient.validate_real_mode,
        )
        self.mode_changes = 0

    async def on_mode_changed(self) -> None:
        self.mode_changes += 1


def test_mode_real_incompatible_clob_keeps_paper_mode(
    monkeypatch, tmp_path
) -> None:
    """A failed Telegram activation must not change the configured mode."""
    def legacy_create_order(self, token_id, price, size, side):
        return None

    monkeypatch.setattr(ClobClient, "create_and_post_order", legacy_create_order)

    cfg = empty_paper_config()
    risk = RiskManager(cfg.risk)
    storage = Storage(str(tmp_path / "telegram.db"))
    orchestrator = FakeOrchestrator()
    bot = TelegramBot(cfg, risk, storage, engine_ref=None,
                      orchestrator_ref=orchestrator)
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=cfg.telegram.allowed_user_ids[0]),
        effective_message=message,
    )
    context = SimpleNamespace(args=["real"])

    asyncio.run(bot._cmd_mode(update, context))

    assert cfg.mode == "paper"
    assert orchestrator.mode_changes == 0
    assert len(message.replies) == 1
    assert "order API is incompatible" in message.replies[0]
    assert "Upgrade or reinstall py-clob-client-v2" in message.replies[0]