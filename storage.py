"""SQLite-backed storage for trades, equity curve, and signal log."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    mode TEXT NOT NULL,             -- paper|real
    condition_id TEXT NOT NULL,
    slug TEXT,
    side TEXT NOT NULL,             -- UP|DOWN
    action TEXT NOT NULL,           -- OPEN|CLOSE
    price REAL NOT NULL,
    size_shares REAL NOT NULL,
    size_usdc REAL NOT NULL,
    pnl REAL DEFAULT 0,
    order_id TEXT,
    raw TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    btc_price REAL,
    btc_drift_pct REAL,
    our_prob_up REAL,
    market_prob_up REAL,
    edge REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts REAL PRIMARY KEY,
    equity_usdc REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
"""


class Storage:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        # sqlite3 in async-safe pattern: open per call (cheap, file-based)
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10.0)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        c = self._conn()
        try:
            c.executescript(SCHEMA)
            c.commit()
        finally:
            c.close()

    async def log_trade(self, **kw) -> int:
        async with self._lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._log_trade_sync, kw)

    def _log_trade_sync(self, kw: dict) -> int:
        c = self._conn()
        try:
            c.execute(
                "INSERT INTO trades (ts,mode,condition_id,slug,side,action,price,"
                "size_shares,size_usdc,pnl,order_id,raw) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kw.get("ts", time.time()),
                    kw.get("mode", "paper"),
                    kw.get("condition_id", ""),
                    kw.get("slug", ""),
                    kw.get("side", ""),
                    kw.get("action", ""),
                    float(kw.get("price", 0.0)),
                    float(kw.get("size_shares", 0.0)),
                    float(kw.get("size_usdc", 0.0)),
                    float(kw.get("pnl", 0.0)),
                    kw.get("order_id", ""),
                    json.dumps(kw.get("raw", {})),
                ),
            )
            c.commit()
            return c.total_changes
        finally:
            c.close()

    async def log_signal(self, **kw) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._log_signal_sync, kw)

    def _log_signal_sync(self, kw: dict) -> None:
        c = self._conn()
        try:
            c.execute(
                "INSERT INTO signals (ts,action,btc_price,btc_drift_pct,our_prob_up,"
                "market_prob_up,edge,reason) VALUES (?,?,?,?,?,?,?,?)",
                (
                    kw.get("ts", time.time()),
                    kw.get("action", ""),
                    float(kw.get("btc_price", 0.0)),
                    float(kw.get("btc_drift_pct", 0.0)),
                    float(kw.get("our_prob_up", 0.0)),
                    float(kw.get("market_prob_up", 0.0)),
                    float(kw.get("edge", 0.0)),
                    kw.get("reason", ""),
                ),
            )
            c.commit()
        finally:
            c.close()

    async def log_equity(self, equity_usdc: float) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._log_equity_sync, equity_usdc)

    def _log_equity_sync(self, eq: float) -> None:
        c = self._conn()
        try:
            c.execute(
                "INSERT OR REPLACE INTO equity (ts, equity_usdc) VALUES (?,?)",
                (time.time(), eq),
            )
            c.commit()
        finally:
            c.close()

    async def recent_trades(self, limit: int = 20) -> list[dict]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._recent_trades_sync, limit)

    def _recent_trades_sync(self, limit: int) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    async def today_pnl(self) -> float:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._today_pnl_sync)

    def _today_pnl_sync(self) -> float:
        c = self._conn()
        try:
            today_start = time.time() - (time.time() % 86400)
            r = c.execute(
                "SELECT COALESCE(SUM(pnl),0) as s FROM trades "
                "WHERE action='CLOSE' AND ts >= ?", (today_start,)
            ).fetchone()
            return float(r["s"]) if r else 0.0
        finally:
            c.close()
