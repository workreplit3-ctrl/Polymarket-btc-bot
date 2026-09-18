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
    max_minutes_to_resolution: int
    min_volume_usd: float


@dataclass
class PolymarketCfg:
    chain_id: int
    clob_host: str
    gamma_host: str
    http_proxy: Optional[str]
    wallet: WalletCfg
    market_filter: MarketFilterCfg
    data_api_host: str = "https://data-api.polymarket.com"


@dataclass
class StrategyCfg:
    tick_interval_sec: int
    entry_edge_pct: float
    exit_edge_pct: float
    take_profit_pct: float
    take_profit_cost_buffer_pct: float
    adverse_edge_stop_pct: float
    prefer_side: str
    max_volatility_60s: float
    min_volatility_per_sec: float
    min_seconds_remaining_for_entry: int
    max_seconds_remaining_for_entry: int
    min_token_price: float
    max_token_price: float
    round_trip_cost_buffer_pct: float
    max_market_start_age_sec: float
    calibration_market_logit_intercept: float
    calibration_market_logit_slope: float
    calibration_raw_model_weight: float
    real_entries_enabled: bool
    x_confirming_edge_pct: float
    x_min_confidence: float
    x_min_base_entry_edge_pct: float


@dataclass
class RiskCfg:
    max_open_positions: int
    per_trade_size_usdc: float
    daily_loss_limit_usdc: float
    max_total_exposure_usdc: float
    loss_cooldown_sec: int
    post_trade_cooldown_sec: int
    max_slippage_cents: float
    day_timezone: str


@dataclass
class XCfg:
    enabled: bool
    usernames: List[str]
    fresh_window_sec: int
    refresh_interval_sec: int
    request_timeout_sec: float
    min_followers: int
    confirming_edge_pct: float
    min_confidence: float
    min_base_entry_edge_pct: float


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
    x: XCfg
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
            min_minutes_to_resolution=int(mf.get("min_minutes_to_resolution", 1)),
            max_minutes_to_resolution=int(mf.get("max_minutes_to_resolution", 10)),
            min_volume_usd=float(mf.get("min_volume_usd", 1000)),
        ),
        data_api_host=pm.get("data_api_host", "https://data-api.polymarket.com"),
    )

    s = raw.get("strategy", {})
    strat = StrategyCfg(
        tick_interval_sec=int(s.get("tick_interval_sec", 5)),
        entry_edge_pct=float(s.get("entry_edge_pct", 0.15)),
        exit_edge_pct=float(s.get("exit_edge_pct", 0.015)),
        take_profit_pct=float(s.get("take_profit_pct", 0.20)),
        take_profit_cost_buffer_pct=float(
            s.get("take_profit_cost_buffer_pct", 0.02)
        ),
        adverse_edge_stop_pct=float(s.get("adverse_edge_stop_pct", 0.10)),
        prefer_side=s.get("prefer_side", "UP"),
        max_volatility_60s=float(s.get("max_volatility_60s", 0.0040)),
        min_volatility_per_sec=float(s.get("min_volatility_per_sec", 0.00001)),
        min_seconds_remaining_for_entry=int(
            s.get("min_seconds_remaining_for_entry", 150)
        ),
        max_seconds_remaining_for_entry=int(
            s.get("max_seconds_remaining_for_entry", 270)
        ),
        min_token_price=float(s.get("min_token_price", 0.10)),
        max_token_price=float(s.get("max_token_price", 0.90)),
        round_trip_cost_buffer_pct=float(
            s.get("round_trip_cost_buffer_pct", 0.02)
        ),
        max_market_start_age_sec=float(
            s.get("max_market_start_age_sec", 10.0)
        ),
        calibration_market_logit_intercept=float(
            s.get("calibration_market_logit_intercept", 0.0)
        ),
        calibration_market_logit_slope=float(
            s.get("calibration_market_logit_slope", 1.0)
        ),
        calibration_raw_model_weight=float(
            s.get("calibration_raw_model_weight", 0.0)
        ),
        real_entries_enabled=bool(s.get("real_entries_enabled", False)),
        x_confirming_edge_pct=float(s.get("x_confirming_edge_pct", 0.015)),
        x_min_confidence=float(s.get("x_min_confidence", 0.55)),
        x_min_base_entry_edge_pct=float(s.get("x_min_base_entry_edge_pct", 0.15)),
    )
    if not 0.05 <= strat.take_profit_pct <= 1.0:
        raise ConfigError(
            "strategy.take_profit_pct must be between 0.05 and 1.00 "
            "(5%-100%)"
        )
    if strat.take_profit_cost_buffer_pct < 0:
        raise ConfigError(
            "strategy.take_profit_cost_buffer_pct cannot be negative"
        )

    r = raw.get("risk", {})
    risk = RiskCfg(
        max_open_positions=int(r.get("max_open_positions", 1)),
        per_trade_size_usdc=float(r.get("per_trade_size_usdc", 1.0)),
        daily_loss_limit_usdc=float(r.get("daily_loss_limit_usdc", 2.0)),
        max_total_exposure_usdc=float(r.get("max_total_exposure_usdc", 1.0)),
        loss_cooldown_sec=int(r.get("loss_cooldown_sec", 600)),
        post_trade_cooldown_sec=int(r.get("post_trade_cooldown_sec", 60)),
        max_slippage_cents=float(r.get("max_slippage_cents", 0.02)),
        day_timezone=str(r.get("day_timezone", "Asia/Yekaterinburg")),
    )

    x_raw = raw.get("x", {})
    x = XCfg(
        enabled=bool(x_raw.get("enabled", False)),
        usernames=[
            str(username).strip().lstrip("@").lower()
            for username in x_raw.get("usernames", [])
            if str(username).strip()
        ],
        fresh_window_sec=int(x_raw.get("fresh_window_sec", 900)),
        refresh_interval_sec=int(x_raw.get("refresh_interval_sec", 60)),
        request_timeout_sec=float(x_raw.get("request_timeout_sec", 3.0)),
        min_followers=int(x_raw.get("min_followers", 100_000)),
        confirming_edge_pct=float(x_raw.get("confirming_edge_pct", 0.015)),
        min_confidence=float(x_raw.get("min_confidence", 0.55)),
        min_base_entry_edge_pct=float(
            x_raw.get("min_base_entry_edge_pct", 0.10)
        ),
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
        x=x,
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
            "data_api_host": "https://data-api.polymarket.com",
            "http_proxy": None,
            "wallet": {"private_key": None, "funder": None},
            "market_filter": {
                "slug_prefix": "bitcoin-up-or-down",
                "min_minutes_to_resolution": 1,
                "max_minutes_to_resolution": 10,
                "min_volume_usd": 1000,
            },
        },
        "strategy": {
            "tick_interval_sec": 5,
            "entry_edge_pct": 0.15,
            "exit_edge_pct": 0.015,
            "take_profit_pct": 0.20,
            "take_profit_cost_buffer_pct": 0.02,
            "adverse_edge_stop_pct": 0.10,
            "prefer_side": "UP",
            "max_volatility_60s": 0.0040,
            "min_volatility_per_sec": 0.00001,
            "min_seconds_remaining_for_entry": 150,
            "max_seconds_remaining_for_entry": 270,
            "min_token_price": 0.10,
            "max_token_price": 0.90,
            "round_trip_cost_buffer_pct": 0.02,
            "max_market_start_age_sec": 10.0,
            "calibration_market_logit_intercept": 0.0,
            "calibration_market_logit_slope": 1.0,
            "calibration_raw_model_weight": 0.0,
            "real_entries_enabled": False,
            "x_confirming_edge_pct": 0.015,
            "x_min_confidence": 0.55,
            "x_min_base_entry_edge_pct": 0.15,
        },
        "risk": {
            "max_open_positions": 1,
            "per_trade_size_usdc": 1.0,
            "daily_loss_limit_usdc": 2.0,
            "max_total_exposure_usdc": 1.0,
            "loss_cooldown_sec": 600,
            "post_trade_cooldown_sec": 60,
            "max_slippage_cents": 0.02,
            "day_timezone": "Asia/Yekaterinburg",
        },
        "storage": {"sqlite_path": "data/bot.db", "log_retention_days": 30},
        "logging": {"level": "INFO", "file": "logs/bot.log", "json": False},
    }
    return _build(raw)
