"""
Telegram bot — full control panel.

Commands:
  /start            – greeting + auth check
  /status           – bot & engine status (mode, BTC price, open positions)
  /mode paper|real  – switch trading mode (real requires wallet configured)
  /pause            – stop the strategy loop
  /resume           – resume the strategy loop
  /positions        – list open positions
  /trades [N]       – last N trades (default 10)
  /pnl              – today's PnL summary
  /markets          – currently tracked BTC up/down markets
  /config           – show key config values (secrets redacted)
  /set <key> <val>  – tune a runtime parameter (entry_edge, exit_edge, per_trade_size, ...)
  /shutdown         – gracefully stop the bot
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

try:
    from telegram import Update
    from telegram.constants import ChatAction
    from telegram.error import NetworkError, TimedOut
    from telegram.ext import (
        Application, ApplicationBuilder, CommandHandler, ContextTypes,
    )
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False

from .config import Config
from .logger import get_logger
from .risk import RiskManager
from .storage import Storage

log = get_logger("telegram")


class TelegramBot:
    def __init__(self, cfg: Config, risk: RiskManager, storage: Storage,
                 engine_ref, orchestrator_ref):
        self.cfg = cfg
        self.risk = risk
        self.storage = storage
        self.engine = engine_ref         # TradingEngine
        self.orch = orchestrator_ref     # Orchestrator (defined in bot.py)
        self.app: Optional["Application"] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ----- lifecycle ------------------------------------------------
    async def start(self) -> None:
        if not _TG_AVAILABLE:
            log.warning("python-telegram-bot not installed; Telegram control disabled")
            return
        self.app = (
            ApplicationBuilder()
            .token(self.cfg.telegram.bot_token)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("mode", self._cmd_mode))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("positions", self._cmd_positions))
        self.app.add_handler(CommandHandler("trades", self._cmd_trades))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self.app.add_handler(CommandHandler("markets", self._cmd_markets))
        self.app.add_handler(CommandHandler("config", self._cmd_config))
        self.app.add_handler(CommandHandler("set", self._cmd_set))
        self.app.add_handler(CommandHandler("shutdown", self._cmd_shutdown))
        self.app.add_handler(CommandHandler("help", self._cmd_help))

        for attempt in range(4):
            try:
                await self.app.initialize()
                break
            except (TimedOut, NetworkError) as exc:
                if attempt == 3:
                    raise
                delay = 2 ** attempt
                log.warning(
                    f"Telegram API unavailable during startup ({exc}); "
                    f"retrying in {delay}s"
                )
                await asyncio.sleep(delay)
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started")

        if self.cfg.telegram.heartbeat_interval_sec > 0:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                log.warning(f"telegram shutdown error: {e}")

    async def send(self, text: str) -> None:
        if not self.app:
            return
        for uid in self.cfg.telegram.allowed_user_ids:
            try:
                await self.app.bot.send_message(chat_id=uid, text=text[:4000])
            except Exception as e:
                log.warning(f"send to {uid} failed: {e}")

    # ----- access control -------------------------------------------
    def _authorized(self, update: Update) -> bool:
        uid = update.effective_user.id if update.effective_user else 0
        return uid in self.cfg.telegram.allowed_user_ids

    async def _reject(self, update: Update) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to control this bot."
            )

    # ----- heartbeat ------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.cfg.telegram.heartbeat_interval_sec)
                snap = self.risk.snapshot()
                btc = self.orch.feed.consensus()
                btc_str = f"${btc.mid:,.2f}" if btc and btc.mid > 0 else "n/a"
                msg = (
                    f"💓 heartbeat\n"
                    f"mode: {self.cfg.mode}\n"
                    f"BTC: {btc_str}\n"
                    f"open positions: {snap['open_positions']}\n"
                    f"exposure: ${snap['total_exposure_usdc']:.2f}\n"
                    f"today PnL: ${snap['daily_pnl_usdc']:.2f}"
                )
                await self.send(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"heartbeat error: {e}")

    # ----- commands -------------------------------------------------
    async def _cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        await self._cmd_help(update, ctx)

    async def _cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        await update.effective_message.reply_text(
            "🤖 *Polymarket BTC bot*\n"
            "/status  – bot & engine status\n"
            "/mode paper|real  – switch mode\n"
            "/pause /resume  – pause/resume strategy loop\n"
            "/positions  – open positions\n"
            "/trades [N]  – last N trades\n"
            "/pnl  – today PnL\n"
            "/markets  – tracked markets\n"
            "/config  – current config\n"
            "/set <key> <val>  – tune param\n"
            "/shutdown  – stop the bot",
            parse_mode="Markdown",
        )

    async def _cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        snap = self.risk.snapshot()
        btc = self.orch.feed.consensus()
        btc_str = f"${btc.mid:,.2f}" if btc.mid > 0 else "n/a"
        div = f"{btc.divergence_pct:.3f}%"
        stale = "⚠ stale" if btc.is_stale else "ok"
        paused = "PAUSED" if self.orch.paused else "running"
        msg = (
            f"📊 *status*\n"
            f"mode: `{self.cfg.mode}`\n"
            f"loop: {paused}\n"
            f"BTC mid: {btc_str}\n"
            f"feeds: binance={btc.binance is not None} coinbase={btc.coinbase is not None} "
            f"div={div} ({stale})\n"
            f"volatility 60s: {self.orch.feed.volatility_60s():.5f}\n"
            f"open positions: {snap['open_positions']}/{self.cfg.risk.max_open_positions}\n"
            f"exposure: ${snap['total_exposure_usdc']:.2f}/"
            f"${self.cfg.risk.max_total_exposure_usdc:.2f}\n"
            f"today PnL: ${snap['daily_pnl_usdc']:.2f} "
            f"(limit -${self.cfg.risk.daily_loss_limit_usdc:.2f})"
        )
        await update.effective_message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_mode(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        args = ctx.args
        if not args:
            await update.effective_message.reply_text(f"current mode: {self.cfg.mode}")
            return
        new_mode = args[0].lower()
        try:
            self.cfg.switch_mode(new_mode)
            await self.orch.on_mode_changed()
            await update.effective_message.reply_text(f"✅ mode → {self.cfg.mode}")
        except Exception as e:
            await update.effective_message.reply_text(f"❌ {e}")

    async def _cmd_pause(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        self.orch.set_paused(True)
        await update.effective_message.reply_text("⏸ strategy loop paused")

    async def _cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        self.orch.set_paused(False)
        await update.effective_message.reply_text("▶ strategy loop resumed")

    async def _cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        snap = self.risk.snapshot()
        if not snap["positions"]:
            await update.effective_message.reply_text("no open positions")
            return
        lines = []
        for p in snap["positions"]:
            lines.append(
                f"• {p['slug']} [{p['side']}]\n"
                f"  entry {p['entry']:.4f}  mark {p['mark']:.4f}\n"
                f"  size {p['shares']:.2f} sh (${p['usdc']:.2f})\n"
                f"  unrealized ${p['unrealized_pnl']:.2f}"
            )
        await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_trades(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        n = 10
        if ctx.args:
            try: n = int(ctx.args[0])
            except ValueError: pass
        trades = await self.storage.recent_trades(limit=n)
        if not trades:
            await update.effective_message.reply_text("no trades recorded yet")
            return
        lines = []
        for t in trades:
            ts = time.strftime("%H:%M:%S", time.gmtime(t["ts"]))
            lines.append(
                f"{ts} {t['mode']} {t['action']} {t['side']} "
                f"{t['slug']} @ {t['price']:.4f} "
                f"(${t['size_usdc']:.2f}) pnl=${t['pnl']:.2f}"
                f"{' status=' + t['order_status'] if t.get('order_status') else ''}"
            )
        await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_pnl(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        pnl = await self.storage.today_pnl()
        snap = self.risk.snapshot()
        await update.effective_message.reply_text(
            f"today PnL (closed trades): ${pnl:.2f}\n"
            f"today PnL (risk tracker):  ${snap['daily_pnl_usdc']:.2f}\n"
            f"limit: -${self.cfg.risk.daily_loss_limit_usdc:.2f}"
        )

    async def _cmd_markets(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        markets = self.orch.tracked_markets
        if not markets:
            await update.effective_message.reply_text("no markets tracked")
            return
        lines = []
        for m in markets[:5]:
            lines.append(f"• {m.slug}\n  end={m.end_date}\n  vol=${m.volume:,.0f}")
        await update.effective_message.reply_text("\n".join(lines))

    async def _cmd_config(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        s = self.cfg.strategy
        r = self.cfg.risk
        msg = (
            f"*strategy*\n"
            f"  tick: {s.tick_interval_sec}s  window: {s.reference_window_sec}s\n"
            f"  entry_edge: {s.entry_edge_pct}  exit_edge: {s.exit_edge_pct}\n"
            f"  adverse_stop: {s.adverse_edge_stop_pct}\n"
            f"  max_vol_60s: {s.max_volatility_60s}\n"
            f"*risk*\n"
            f"  max_positions: {r.max_open_positions}\n"
            f"  per_trade: ${r.per_trade_size_usdc}\n"
            f"  daily_loss_limit: ${r.daily_loss_limit_usdc}\n"
            f"  max_exposure: ${r.max_total_exposure_usdc}\n"
            f"  loss_cooldown: {r.loss_cooldown_sec}s\n"
            f"  post_trade_cooldown: {r.post_trade_cooldown_sec}s\n"
            f"  max_slip: {r.max_slippage_cents}"
        )
        await update.effective_message.reply_text(msg, parse_mode="Markdown")

    async def _cmd_set(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        if len(ctx.args) < 2:
            await update.effective_message.reply_text(
                "usage: /set <key> <value>\n"
                "keys: entry_edge_pct, exit_edge_pct, adverse_edge_stop_pct, "
                "max_volatility_60s, per_trade_size_usdc, max_open_positions, "
                "daily_loss_limit_usdc, max_total_exposure_usdc, loss_cooldown_sec, "
                "post_trade_cooldown_sec, max_slippage_cents, tick_interval_sec"
            )
            return
        key = ctx.args[0]
        raw_val = ctx.args[1]
        try:
            val = float(raw_val)
            if key.endswith("_sec") or key in ("max_open_positions",):
                val = int(val)
        except ValueError:
            val = raw_val

        # Apply to strategy or risk
        if hasattr(self.cfg.strategy, key):
            setattr(self.cfg.strategy, key, val)
        elif hasattr(self.cfg.risk, key):
            setattr(self.cfg.risk, key, val)
        else:
            await update.effective_message.reply_text(f"unknown key: {key}")
            return
        await update.effective_message.reply_text(f"✅ {key} = {val}")

    async def _cmd_shutdown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._reject(update); return
        await update.effective_message.reply_text("🛑 shutting down…")
        self.orch.request_shutdown()
