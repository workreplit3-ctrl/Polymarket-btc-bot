#!/usr/bin/env python3
"""Environment-aware launcher for the imported Polymarket BTC bot."""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bot import Orchestrator
from src.config import ConfigError, _build
from src.logger import get_logger, setup_logging


def build_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN_10", "").strip()
    raw_user_ids = os.environ.get("TELEGRAM_USER_ID", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN_10 is not configured")
    if not raw_user_ids:
        raise ConfigError("TELEGRAM_USER_ID is not configured")

    user_ids = [int(value.strip()) for value in raw_user_ids.split(",") if value.strip()]
    if not user_ids:
        raise ConfigError("TELEGRAM_USER_ID must contain a numeric Telegram ID")

    with (ROOT / "config" / "config.yaml").open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    raw.setdefault("telegram", {})
    raw["telegram"]["bot_token"] = token
    raw["telegram"]["allowed_user_ids"] = user_ids
    raw["mode"] = os.environ.get("POLYMARKET_MODE", "paper").strip().lower()
    raw.setdefault("polymarket", {}).setdefault("wallet", {})
    raw["polymarket"]["wallet"]["private_key"] = (
        os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip() or None
    )
    raw["polymarket"]["wallet"]["funder"] = (
        os.environ.get("POLYMARKET_FUNDER_ADDRESS", "").strip() or None
    )
    config = _build(raw)
    config.storage.sqlite_path = str(ROOT / "data" / "bot.db")
    config.logging.file = str(ROOT / "logs" / "bot.log")
    config.validate_for_mode(config.mode)
    return config


async def run() -> int:
    try:
        config = build_config()
    except (ConfigError, ValueError) as exc:
        print(f"bot configuration error: {exc}", file=sys.stderr, flush=True)
        return 2

    setup_logging(
        level=config.logging.level,
        file_path=config.logging.file,
        json_log=config.logging.json,
    )
    log = get_logger("launcher")
    log.info("starting Telegram bot 10 in %s mode", config.mode)

    orchestrator = Orchestrator(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, orchestrator.request_shutdown)
        except NotImplementedError:
            pass

    await orchestrator.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))