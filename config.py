"""Configuration loader with validation."""
from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class ConfigError(Exception):
    pass


@dataclass
class TelegramCfg:
    bot_token: str
    allowed_user_ids: List[int]
    heartbeat_interval_sec: int

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TelegramCfg":
        if not d.get("bot_token") or d["bot_token"] == "PUT_YOUR_BOT_TOKEN_HERE":
            raise ConfigError("telegram.bot_token is not set")
        ids = d.get("allowed_user_ids", [])
        if not ids:
            raise ConfigError("telegram.allowed_user_ids must contain at least one user id")
        return cls(
            bot_token=d["bot_token"],
            allowed_user_ids=[int(x) for x in ids],
            heartbeat_interval_sec=int(d.get("heartbeat_interval_sec", 1800)),
        )


@dataclass
class BtcFeedCfg:
    enabled: bool
    ws_url: str
    product_id: Optional[str] = None
    reconnect_min_sec: int = 1
    reconnect_max_sec: int = 30


@dataclass
class BtcFeedsCfg:
    binance: BtcFeedCfg
    coinbase: BtcFeedCfg
    cross_feed_max_divergence_pct: float

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BtcFeedsCfg":
        b = d.get("binance", {})
        c = d.get("coinbase", {})
        return cls(
            binance=BtcFeedCfg(
                enabled=bool(b.get("enabled", True)),
                ws_url=b.get("ws_url", "wss://stream.binance.com:9443/ws/btcusdt@bookTicker"),
                reconnect_min_sec=int(b.get("reconnect_min_sec", 1)),
                reconnect_max_sec=int(b.get("reconnect_max_sec", 30)),
            ),
            coinbase=BtcFeedCfg(
                enabled=bool(c.get("enabled", True)),
                ws_url=c.get("ws_url", "wss://advanced-trade-ws.coinbase.com/ticker"),
                product_id=c.get("product_id", "BTC-USD"),
                reconnect_min_sec=int(c.get("reconnect_min_sec", 1)),
                reconnect_max_sec=int(c.get("reconnect_max_sec", 30)),
            ),
            cross_feed_max_divergence_pct=float(
                d.get("cross_feed_max_divergence_pct", 0.15)
            ),
        )


@dataclass
class WalletCfg:
    private_key: Optional[str]
    funder: Optional[str]

    def is_configured(self) -> bool:
        return bool(self.private_key and self.funder)


@dataclass
class MarketFilterCfg:
    slug_prefix: str
    min_minutes_to_resolution: int
    min_volume_usd: float


@dataclass
class PolymarketCfg:
    chain_id: int
    clob_host: str
    gamma_host: str
    http_proxy: Optional[str]
    wallet: WalletCfg
    market_filter: MarketFilterCfg


@dataclass
class StrategyCfg:
    tick_interval_sec: int
    reference_window_sec: int
    drift_to_prob_k: float
    entry_edge_pct: float
    exit_edge_pct: float
    adverse_edge_stop_pct: float
    prefer_side: str
    max_volatility_60s: float


@dataclass
class RiskCfg:
    max_open_positions: int
    per_trade_size_usdc: float
    daily_loss_limit_usdc: float
    max_total_exposure_usdc: float
    loss_cooldown_sec: int
    post_trade_cooldown_sec: int
    max_slippage_cents: float


@dataclass
class StorageCfg:
    sqlite_path: str
    log_retention_days: int


@dataclass
class LoggingCfg:
    level: str
    file: str
    json: bool


@dataclass
class Config:
    telegram: TelegramCfg
    mode: str  # "paper" | "real"
    btc_feeds: BtcFeedsCfg
    polymarket: PolymarketCfg
    strategy: StrategyCfg
    risk: RiskCfg
    storage: StorageCfg
    logging: LoggingCfg
    raw: Dict[str, Any] = field(default_factory=dict)

    def validate_for_mode(self, mode: str) -> None:
        if mode == "real":
            w = self.polymarket.wallet
            if not w.is_configured():
                raise ConfigError(
                    "Real mode requires polymarket.wallet.private_key and .funder"
                )

    def switch_mode(self, mode: str) -> None:
        if mode not in ("paper", "real"):
            raise ConfigError(f"mode must be 'paper' or 'real', got {mode!r}")
        self.validate_for_mode(mode)
        self.mode = mode


def _build(raw: Dict[str, Any]) -> Config:
    tg = TelegramCfg.from_dict(raw.get("telegram", {}))
    mode = raw.get("mode", "paper")
    if mode not in ("paper", "real"):
        raise ConfigError(f"invalid mode {mode!r}")
    bf = BtcFeedsCfg.from_dict(raw.get("btc_feeds", {}))

    pm = raw.get("polymarket", {})
    wallet = WalletCfg(
        private_key=pm.get("wallet", {}).get("private_key"),
        funder=pm.get("wallet", {}).get("funder"),
    )
    mf = pm.get("market_filter", {})
    poly = PolymarketCfg(
        chain_id=int(pm.get("chain_id", 137)),
        clob_host=pm.get("clob_host", "https://clob.polymarket.com"),
        gamma_host=pm.get("gamma_host", "https://gamma-api.polymarket.com"),
        http_proxy=pm.get("http_proxy"),
        wallet=wallet,
        market_filter=MarketFilterCfg(
            slug_prefix=mf.get("slug_prefix", "bitcoin-up-or-down"),
            min_minutes_to_resolution=int(mf.get("min_minutes_to_resolution", 5)),
            min_volume_usd=float(mf.get("min_volume_usd", 1000)),
        ),
    )

    s = raw.get("strategy", {})
    strat = StrategyCfg(
        tick_interval_sec=int(s.get("tick_interval_sec", 5)),
        reference_window_sec=int(s.get("reference_window_sec", 300)),
        drift_to_prob_k=float(s.get("drift_to_prob_k", 8.0)),
        entry_edge_pct=float(s.get("entry_edge_pct", 0.06)),
        exit_edge_pct=float(s.get("exit_edge_pct", 0.015)),
        adverse_edge_stop_pct=float(s.get("adverse_edge_stop_pct", 0.10)),
        prefer_side=s.get("prefer_side", "UP"),
        max_volatility_60s=float(s.get("max_volatility_60s", 0.0040)),
    )

    r = raw.get("risk", {})
    risk = RiskCfg(
        max_open_positions=int(r.get("max_open_positions", 3)),
        per_trade_size_usdc=float(r.get("per_trade_size_usdc", 10.0)),
        daily_loss_limit_usdc=float(r.get("daily_loss_limit_usdc", 30.0)),
        max_total_exposure_usdc=float(r.get("max_total_exposure_usdc", 50.0)),
        loss_cooldown_sec=int(r.get("loss_cooldown_sec", 600)),
        post_trade_cooldown_sec=int(r.get("post_trade_cooldown_sec", 60)),
        max_slippage_cents=float(r.get("max_slippage_cents", 0.02)),
    )

    st = raw.get("storage", {})
    storage = StorageCfg(
        sqlite_path=st.get("sqlite_path", "data/bot.db"),
        log_retention_days=int(st.get("log_retention_days", 30)),
    )

    lg = raw.get("logging", {})
    logging_ = LoggingCfg(
        level=lg.get("level", "INFO"),
        file=lg.get("file", "logs/bot.log"),
        json=bool(lg.get("json", False)),
    )

    cfg = Config(
        telegram=tg,
        mode=mode,
        btc_feeds=bf,
        polymarket=poly,
        strategy=strat,
        risk=risk,
        storage=storage,
        logging=logging_,
        raw=raw,
    )
    cfg.validate_for_mode(mode)
    return cfg


def load_config(path: str | Path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _build(raw)


def empty_paper_config() -> Config:
    """Build a minimal paper-mode config for dry-run validation when no real
    config.yaml is available. Telegram and wallet are placeholders."""
    raw = {
        "telegram": {
            "bot_token": "DRYRUN_TOKEN",
            "allowed_user_ids": [0],
            "heartbeat_interval_sec": 3600,
        },
        "mode": "paper",
        "btc_feeds": {
            "binance": {"enabled": True},
            "coinbase": {"enabled": True},
            "cross_feed_max_divergence_pct": 0.15,
        },
        "polymarket": {
            "chain_id": 137,
            "clob_host": "https://clob.polymarket.com",
            "gamma_host": "https://gamma-api.polymarket.com",
            "http_proxy": None,
            "wallet": {"private_key": None, "funder": None},
            "market_filter": {
                "slug_prefix": "bitcoin-up-or-down",
                "min_minutes_to_resolution": 5,
                "min_volume_usd": 1000,
            },
        },
        "strategy": {
            "tick_interval_sec": 5,
            "reference_window_sec": 300,
            "drift_to_prob_k": 8.0,
            "entry_edge_pct": 0.06,
            "exit_edge_pct": 0.015,
            "adverse_edge_stop_pct": 0.10,
            "prefer_side": "UP",
            "max_volatility_60s": 0.0040,
        },
        "risk": {
            "max_open_positions": 3,
            "per_trade_size_usdc": 10.0,
            "daily_loss_limit_usdc": 30.0,
            "max_total_exposure_usdc": 50.0,
            "loss_cooldown_sec": 600,
            "post_trade_cooldown_sec": 60,
            "max_slippage_cents": 0.02,
        },
        "storage": {"sqlite_path": "data/bot.db", "log_retention_days": 30},
        "logging": {"level": "INFO", "file": "logs/bot.log", "json": False},
    }
    return _build(raw)
