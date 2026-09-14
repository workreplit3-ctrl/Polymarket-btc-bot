"""
Polymarket client wrapper.

Wraps two things:
1. Gamma API  — public market metadata (active markets, slugs, resolutions).
2. CLOB API   — orderbook and (in real mode) order placement.

In paper mode we only need read access (orderbook + market info).
In real mode we use the official `py-clob-client` to sign and submit orders.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from .config import PolymarketCfg
from .logger import get_logger

log = get_logger("polymarket")


@dataclass
class MarketInfo:
    """Subset of a Polymarket market that we actually need."""
    condition_id: str
    question: str
    slug: str
    end_date: str           # ISO timestamp
    outcome_up_token_id: str
    outcome_down_token_id: str
    # outcome labels in the order Polymarket returns them — typically ["Up","Down"] or ["Yes","No"]
    outcomes: List[str]
    volume: float
    active: bool


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    ts: float

    def best_bid(self) -> Optional[OrderBookLevel]:
        return max(self.bids, key=lambda l: l.price) if self.bids else None

    def best_ask(self) -> Optional[OrderBookLevel]:
        return min(self.asks, key=lambda l: l.price) if self.asks else None

    def mid(self) -> Optional[float]:
        bb = self.best_bid()
        ba = self.best_ask()
        if bb and ba:
            return (bb.price + ba.price) / 2.0
        return None


class PolymarketClient:
    """Async wrapper around Gamma + CLOB HTTP endpoints."""

    def __init__(self, cfg: PolymarketCfg):
        self.cfg = cfg
        proxy = cfg.http_proxy or None
        self._http = httpx.AsyncClient(timeout=15.0, proxy=proxy)
        # Lazily constructed real-mode signing client
        self._clob_signer = None

    # ----- lifecycle -------------------------------------------------
    async def close(self) -> None:
        await self._http.aclose()

    # ----- Gamma: market discovery ----------------------------------
    async def list_active_btc_updown_markets(self, slug_prefix: str) -> List[MarketInfo]:
        """Fetch active markets whose slug starts with slug_prefix."""
        out: List[MarketInfo] = []
        seen: set[str] = set()

        # Ordering by createdAt puts the current crypto series on the first
        # page. Keep a small offset fallback for older Gamma deployments.
        for offset in range(0, 2500, 100):
            url = f"{self.cfg.gamma_host}/markets"
            params = {
                "limit": 100,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "createdAt",
                "ascending": "false",
            }
            try:
                r = await self._http.get(url, params=params)
                r.raise_for_status()
                payload = r.json()
            except Exception as e:
                log.error(f"gamma markets fetch failed: {e}")
                break

            data = payload if isinstance(payload, list) else payload.get("markets", [])
            if not isinstance(data, list) or not data:
                break

            for m in data:
                slug = m.get("slug", "")
                if slug in seen or not slug.startswith(slug_prefix):
                    continue
                seen.add(slug)
                try:
                    info = self._parse_market(m)
                    if info is not None:
                        out.append(info)
                except Exception as e:
                    log.debug(f"skip market {slug}: {e}")

            # Once the newest page has matching markets, older pages are
            # historical series and add no useful trading opportunity.
            if out:
                break

        log.info(f"found {len(out)} active BTC up/down markets")
        return out

    def _parse_market(self, m: Dict[str, Any]) -> Optional[MarketInfo]:
        # Polymarket BTC up/down markets: 2-outcome markets
        # The "outcomes" field is a JSON string in gamma; tokens in clobTokenIds
        outcomes_raw = m.get("outcomes") or m.get("outcomeList") or '["Up","Down"]'
        if isinstance(outcomes_raw, str):
            try:
                import json as _json
                outcomes = _json.loads(outcomes_raw)
            except Exception:
                outcomes = ["Up", "Down"]
        else:
            outcomes = list(outcomes_raw)

        tokens_raw = m.get("clobTokenIds") or "[]"
        if isinstance(tokens_raw, str):
            import json as _json
            tokens = _json.loads(tokens_raw)
        else:
            tokens = list(tokens_raw)
        if len(tokens) != 2:
            return None

        # Convention: token[0] = first outcome ("Up"), token[1] = second ("Down")
        up_idx = 0
        down_idx = 1
        # Heuristic: try to match label "Up"/"Yes" -> up_idx
        for i, lbl in enumerate(outcomes):
            if str(lbl).lower() in ("up", "yes"):
                up_idx = i
            if str(lbl).lower() in ("down", "no"):
                down_idx = i

        return MarketInfo(
            condition_id=m.get("conditionId") or m.get("condition_id") or "",
            question=m.get("question", ""),
            slug=m.get("slug", ""),
            end_date=m.get("endDate") or m.get("end_date_iso") or "",
            outcome_up_token_id=tokens[up_idx],
            outcome_down_token_id=tokens[down_idx],
            outcomes=outcomes,
            volume=float(
                m.get("volume")
                or m.get("volumeNum")
                or m.get("liquidityNum")
                or m.get("liquidity")
                or 0.0
            ),
            active=bool(m.get("active", True)),
        )

    # ----- CLOB: orderbook ------------------------------------------
    async def get_orderbook(self, token_id: str) -> OrderBook:
        url = f"{self.cfg.clob_host}/book?token_id={token_id}"
        try:
            r = await self._http.get(url)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning(f"orderbook fetch failed for {token_id[:8]}…: {e}")
            return OrderBook(token_id=token_id, bids=[], asks=[], ts=time.time())

        bids = [OrderBookLevel(float(b.get("price", 0)), float(b.get("size", 0)))
                for b in data.get("bids", []) if float(b.get("price", 0)) > 0]
        asks = [OrderBookLevel(float(a.get("price", 0)), float(a.get("size", 0)))
                for a in data.get("asks", []) if float(a.get("price", 0)) > 0]
        return OrderBook(token_id=token_id, bids=bids, asks=asks, ts=time.time())

    # ----- CLOB: order placement (real mode only) -------------------
    async def place_order(
        self,
        token_id: str,
        side: str,             # "BUY" or "SELL"
        price: float,          # 0..1, in Polymarket probability units
        size: float,           # number of shares
    ) -> Dict[str, Any]:
        """Place a real signed order. Requires py-clob-client installed and
        wallet configured. Returns the order receipt."""
        if self._clob_signer is None:
            try:
                # Lazy import — allows paper mode without the dependency
                from py_clob_client.client import ClobClient  # type: ignore
                from py_clob_client.clob_types import ApiCreds  # type: ignore
            except ImportError as e:
                raise RuntimeError(
                    "Real-mode trading requires py-clob-client: "
                    "pip install py-clob-client"
                ) from e

            w = self.cfg.wallet
            if not (w.private_key and w.funder):
                raise RuntimeError("wallet not configured for real mode")

            client = ClobClient(
                host=self.cfg.clob_host,
                key=w.private_key,
                chain_id=self.cfg.chain_id,
                signature_type=1,  # POLY_PROXY (Polymarket uses proxy wallets)
                funder=w.funder,
            )
            # Derive API creds if needed
            try:
                creds = client.create_or_derive_api_creds()
                client.set_api_creds(creds)
            except Exception as e:
                log.warning(f"could not derive API creds: {e}")
            self._clob_signer = client

        from py_clob_client.clob_types import OrderArgs  # type: ignore
        from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore
        side_const = BUY if side.upper() == "BUY" else SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side_const,
        )

        # Run the blocking CLOB client call in a thread
        loop = asyncio.get_running_loop()
        order = await loop.run_in_executor(
            None,
            lambda: self._clob_signer.create_and_post_order(order_args),
        )
        log.info(f"order placed: token={token_id[:8]}… side={side} price={price} size={size}")
        return order if isinstance(order, dict) else {"raw": str(order)}

    async def cancel_order(self, order_id: str) -> bool:
        if self._clob_signer is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._clob_signer.cancel(order_id)
            )
            return True
        except Exception as e:
            log.warning(f"cancel failed for {order_id}: {e}")
            return False
