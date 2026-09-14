#!/usr/bin/env python3
"""
Entry point for the Polymarket BTC Up/Down bot.

Usage:
    python -m src.main --config config/config.yaml
    python -m src.main --dry-run          # no real config needed; validates imports + paper loop
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

# Add project root to sys.path so `src.*` imports work when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import load_config, empty_paper_config, ConfigError  # noqa: E402
from src.logger import setup_logging, get_logger  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", "-c", default="config/config.yaml",
                   help="path to config.yaml")
    p.add_argument("--dry-run", action="store_true",
                   help="validate imports & config in paper mode without real Telegram/network")
    return p.parse_args()


async def dry_run() -> int:
    """Self-check: instantiate all modules and run a single paper tick
    with a stubbed feed (no real network)."""
    log = get_logger("dryrun")
    log.info("=== DRY-RUN VALIDATION ===")

    cfg = empty_paper_config()
    log.info(f"config built (paper mode): mode={cfg.mode}")

    from src.btc_feed import BtcPriceAggregator
    from src.engine import TradingEngine
    from src.polymarket_client import PolymarketClient
    from src.risk import RiskManager
    from src.storage import Storage
    from src.strategy import DivergenceStrategy, SignalAction
    from src.polymarket_client import OrderBook, OrderBookLevel, MarketInfo

    feed = BtcPriceAggregator(cfg.btc_feeds)
    poly = PolymarketClient(cfg.polymarket)
    risk = RiskManager(cfg.risk)
    storage = Storage("data/dryrun.db")
    strategy = DivergenceStrategy(cfg.strategy, feed)
    engine = TradingEngine(cfg, poly, risk, storage)

    log.info("modules imported & instantiated OK")

    # Inject a fake BTC history into the feed so strategy can compute drift.
    # Need to cover at least `reference_window_sec` (300s) into the past.
    import time
    now = time.time()
    base = 60000.0
    window = 600  # 10 minutes of history
    step = 2.0
    n_ticks = int(window // step)
    for i in range(n_ticks):
        ts = now - window + i * step
        # Walk the price up slightly to produce a positive drift
        price = base + i * 0.5
        feed._history.append(ts, price)
    feed._binance_price = base + n_ticks * 0.5
    feed._binance_ts = now
    feed._coinbase_price = base + n_ticks * 0.5 + 2.0
    feed._coinbase_ts = now
    log.info(f"injected fake history: {len(feed._history.data)} ticks over {window}s, "
             f"latest ${feed._binance_price:,.2f}")

    # Build a fake market + orderbooks
    market = MarketInfo(
        condition_id="0xTEST", question="BTC up or down?",
        slug="bitcoin-up-or-down-test",
        end_date="2099-01-01T00:00:00Z",
        outcome_up_token_id="11111", outcome_down_token_id="22222",
        outcomes=["Up", "Down"], volume=5000.0, active=True,
    )
    # Market price of Up = 0.45; our logistic prob will be ~0.95 → big edge → OPEN_UP
    up_book = OrderBook(
        token_id="11111",
        bids=[OrderBookLevel(0.44, 1000), OrderBookLevel(0.43, 500)],
        asks=[OrderBookLevel(0.46, 1000), OrderBookLevel(0.47, 500)],
        ts=now,
    )
    down_book = OrderBook(
        token_id="22222",
        bids=[OrderBookLevel(0.54, 1000)],
        asks=[OrderBookLevel(0.56, 1000)],
        ts=now,
    )

    # Evaluate strategy flat → expect OPEN_UP
    sig = strategy.evaluate(up_book, down_book, current_position=None)
    log.info(f"signal (flat): action={sig.action.value} edge={sig.edge:+.4f} "
             f"reason={sig.reason}")
    assert sig.action == SignalAction.OPEN_UP, f"expected OPEN_UP, got {sig.action}"
    log.info("PASS: strategy emits OPEN_UP on a strong positive drift edge")

    # Execute in paper mode
    event = await engine.execute(sig, market, up_book, down_book)
    log.info(f"event: action={event.action} side={event.side} "
             f"price={event.price:.4f} usdc={event.size_usdc:.2f}")
    assert event.action == "OPEN"
    assert len(risk.state.open_positions) == 1
    log.info("PASS: paper engine opened a position")

    # Now collapse the edge → expect EXIT
    up_book2 = OrderBook(
        token_id="11111",
        bids=[OrderBookLevel(0.90, 1000)],
        asks=[OrderBookLevel(0.92, 1000)],
        ts=now,
    )
    down_book2 = OrderBook(
        token_id="22222",
        bids=[OrderBookLevel(0.08, 1000)],
        asks=[OrderBookLevel(0.10, 1000)],
        ts=now,
    )
    sig2 = strategy.evaluate(up_book2, down_book2, current_position="UP")
    log.info(f"signal (with pos): action={sig2.action.value} edge={sig2.edge:+.4f}")
    # With market_prob=0.91 and our_prob~0.95, edge ~ +0.04 (smaller than exit threshold? no, 0.04>0.015)
    # Actually: our_prob=0.95, market=0.91, edge=+0.04 > exit_edge 0.015 → HOLD
    # Let's force an exit by pushing market up to 0.95
    up_book3 = OrderBook(
        token_id="11111",
        bids=[OrderBookLevel(0.945, 1000)],
        asks=[OrderBookLevel(0.955, 1000)],
        ts=now,
    )
    sig3 = strategy.evaluate(up_book3, down_book2, current_position="UP")
    log.info(f"signal (collapsed edge): action={sig3.action.value} edge={sig3.edge:+.4f}")
    assert sig3.action == SignalAction.EXIT, f"expected EXIT, got {sig3.action}"
    event3 = await engine.execute(sig3, market, up_book3, down_book2)
    log.info(f"exit event: action={event3.action} pnl=${event3.pnl:.2f}")
    assert event3.action == "CLOSE"
    assert len(risk.state.open_positions) == 0
    log.info("PASS: paper engine closed the position when edge collapsed")

    # Risk manager: ensure daily loss limit triggers reject
    risk.state.daily_pnl = -risk.cfg.daily_loss_limit_usdc - 0.01
    ok, reason, msg = risk.check_entry(10.0, up_book)
    log.info(f"after loss limit: ok={ok} reason={reason.value} msg={msg}")
    assert not ok
    log.info("PASS: risk manager rejects new entries after daily loss limit hit")

    await poly.close()
    log.info("=== DRY-RUN PASSED ✅ ===")
    return 0


async def run_real(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    setup_logging(level=cfg.logging.level, file_path=cfg.logging.file,
                  json_log=cfg.logging.json)
    log = get_logger("main")
    log.info(f"starting bot in {cfg.mode} mode (config={args.config})")

    from src.bot import Orchestrator
    orch = Orchestrator(cfg)

    # Handle SIGINT/SIGTERM cleanly
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, orch.request_shutdown)
        except NotImplementedError:
            pass  # Windows

    await orch.run()
    return 0


def main() -> int:
    args = parse_args()
    if args.dry_run:
        setup_logging(level="INFO")
        return asyncio.run(dry_run())
    return asyncio.run(run_real(args))


if __name__ == "__main__":
    sys.exit(main())
