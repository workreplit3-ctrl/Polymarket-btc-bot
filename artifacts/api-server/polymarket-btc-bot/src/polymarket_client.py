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
import inspect
import re
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
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
    # Unix timestamp for the beginning of the market window. Short BTC
    # slugs contain the slot timestamp; older/fake market records may omit it.
    start_ts: float = 0.0


@dataclass
class TokenHolding:
    """A confirmed conditional-token holding returned by the Data API."""
    condition_id: str
    token_id: str
    size_shares: float
    avg_price: float


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


@dataclass(frozen=True)
class RealModeReadiness:
    """Safe-to-display result of the checks required before live trading."""

    ready: bool
    reason: str


class PolymarketClient:
    """Async wrapper around Gamma + CLOB HTTP endpoints."""

    @staticmethod
    def validate_real_mode() -> None:
        """Validate the installed CLOB order API without making a network call.

        Keep this check separate from construction so paper mode never needs
        the real-trading dependency.  The wrapper intentionally uses the V2
        call shape below; accepting an older client here would defer a
        compatibility failure until the first real order.
        """
        try:
            from py_clob_client_v2 import (  # type: ignore
                ClobClient,
                MarketOrderArgs,
                OrderArgs,
                OrderType,
                Side,
            )
        except (ImportError, AttributeError) as exc:
            raise RuntimeError(
                "Real mode cannot start: py-clob-client-v2 is missing or "
                "does not export the required order argument, order type, and side "
                "types. Install or upgrade py-clob-client-v2, then restart "
                "the bot."
            ) from exc

        try:
            inspect.signature(OrderArgs).bind(
                token_id="token",
                price=0.5,
                size=1.0,
                side=Side.BUY,
            )
            inspect.signature(MarketOrderArgs).bind(
                token_id="token",
                amount=1.0,
                price=0.5,
                side=Side.BUY,
                order_type=OrderType.FOK,
            )
            inspect.signature(ClobClient.create_and_post_order).bind(
                object(),
                object(),
                order_type=OrderType.FOK,
            )
            inspect.signature(ClobClient.create_and_post_market_order).bind(
                object(),
                object(),
                order_type=OrderType.FOK,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise RuntimeError(
                "Real mode cannot start: the installed py-clob-client-v2 "
                "order API is incompatible. Expected both limit and market "
                "order methods with FOK support. Upgrade or reinstall "
                "py-clob-client-v2 before enabling real mode."
            ) from exc

    def real_mode_readiness(self) -> RealModeReadiness:
        """Report whether real trading can be enabled without exposing secrets.

        Keep the dependency check delegated to ``validate_real_mode`` so the
        operator-facing status and the activation path cannot drift apart.
        Wallet values are intentionally never included in the result.
        """
        try:
            self.validate_real_mode()
        except RuntimeError as exc:
            return RealModeReadiness(ready=False, reason=str(exc))

        if not self.cfg.wallet.is_configured():
            return RealModeReadiness(
                ready=False,
                reason=(
                    "wallet is not configured; both the private key and "
                    "funder address are required"
                ),
            )

        return RealModeReadiness(
            ready=True,
            reason="CLOB order API is compatible and wallet is configured",
        )

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
        """Fetch active BTC markets, preferring the current short-duration slugs.

        Gamma's ordered active list may contain pre-created markets for the
        following day before the current market. The direct slug lookup keeps
        discovery aligned with the current 5m/15m windows.
        """
        out: List[MarketInfo] = []
        seen: set[str] = set()
        request_failed = False

        now = int(time.time())
        for interval_sec in (300,):
            current_slot = (now // interval_sec) * interval_sec
            for slot in (current_slot - interval_sec, current_slot, current_slot + interval_sec):
                slug = f"{slug_prefix}-{interval_sec // 60}m-{slot}"
                if slug in seen:
                    continue
                seen.add(slug)
                try:
                    r = await self._http.get(f"{self.cfg.gamma_host}/markets/slug/{slug}")
                    if r.status_code == 404:
                        continue
                    r.raise_for_status()
                    info = self._parse_market(r.json())
                    if info is not None:
                        out.append(info)
                except Exception as e:
                    request_failed = True
                    log.debug(f"direct market lookup failed for {slug}: {e}")

        # Direct lookup is authoritative for the short-duration strategy. Do
        # not add future markets from the broad list when current slugs exist.
        if out:
            log.info(f"found {len(out)} active BTC up/down markets via direct slugs")
            return out

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
                request_failed = True
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

        if request_failed and not out:
            raise RuntimeError("active BTC market discovery unavailable")
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

        volume_values = [
            float(m.get(key) or 0.0)
            for key in ("volume", "volumeNum", "volume24hr", "volumeClob")
        ]
        liquidity_values = [
            float(m.get(key) or 0.0)
            for key in ("liquidityNum", "liquidityClob", "liquidity")
        ]
        # Fresh short-duration markets can have very little matched volume
        # while already having a deep order book. Use the larger activity /
        # liquidity value as the market eligibility proxy; the risk manager
        # still checks the actual best ask depth and slippage before ordering.
        market_activity = max(volume_values + liquidity_values + [0.0])

        slug = m.get("slug", "")
        start_ts = float(m.get("startTs") or m.get("start_ts") or 0.0)
        if start_ts <= 0:
            # Current BTC short-window slugs end in "-5m-<unix slot>".
            match = re.search(r"-(?:5m|300s)-(\d+)$", slug)
            if match:
                start_ts = float(match.group(1))

        return MarketInfo(
            condition_id=m.get("conditionId") or m.get("condition_id") or "",
            question=m.get("question", ""),
            slug=slug,
            end_date=m.get("endDate") or m.get("end_date_iso") or "",
            outcome_up_token_id=tokens[up_idx],
            outcome_down_token_id=tokens[down_idx],
            outcomes=outcomes,
            volume=market_activity,
            active=bool(m.get("active", True)),
            start_ts=start_ts,
        )

    # ----- Data API: confirmed wallet positions --------------------
    async def get_confirmed_token_holdings(
        self, markets: List[MarketInfo]
    ) -> List[TokenHolding]:
        """Fetch confirmed token balances for the supplied active markets.

        The Data API is queried by the configured funder wallet and market
        condition IDs.  A successful empty response is a valid flat wallet;
        transport, HTTP, and malformed-response failures are raised so real
        mode can keep entries disabled instead of trading against stale state.
        """
        funder = (self.cfg.wallet.funder or "").strip()
        if not funder:
            raise RuntimeError(
                "cannot reconcile real positions: wallet funder is not configured"
            )
        condition_ids = {
            str(m.condition_id).strip() for m in markets if m.condition_id
        }
        params: Dict[str, Any] = {"user": funder, "limit": 500}
        if condition_ids:
            params["market"] = ",".join(sorted(condition_ids))
        response = await self._http.get(
            f"{self.cfg.data_api_host.rstrip('/')}/positions",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("positions", payload.get("data"))
        if not isinstance(payload, list):
            raise RuntimeError(
                "cannot reconcile real positions: wallet positions response "
                "was not a list"
            )

        market_by_condition = {m.condition_id: m for m in markets}
        token_to_market = {
            token_id: m
            for m in markets
            for token_id in (
                m.outcome_up_token_id,
                m.outcome_down_token_id,
            )
            if token_id
        }
        holdings: Dict[tuple[str, str], TokenHolding] = {}
        for row in payload:
            if not isinstance(row, dict):
                raise RuntimeError(
                    "cannot reconcile real positions: wallet positions "
                    "response contained a non-object row"
                )
            token_id = str(
                row.get("asset")
                or row.get("tokenId")
                or row.get("token_id")
                or ""
            ).strip()
            condition_id = str(
                row.get("conditionId")
                or row.get("condition_id")
                or row.get("market")
                or ""
            ).strip()
            market = market_by_condition.get(condition_id) or token_to_market.get(token_id)
            if market is None or not token_id:
                # The user may hold unrelated positions. They are not part of
                # this bot's active BTC risk universe.
                continue
            if not condition_id:
                condition_id = market.condition_id
            if condition_id != market.condition_id:
                continue

            size = self._position_number(
                row, ("size", "quantity", "balance", "amount")
            )
            if size is None:
                raise RuntimeError(
                    "cannot reconcile real positions: wallet position has "
                    f"no numeric size for token {token_id[:12]}…"
                )
            if size <= 1e-9:
                continue
            avg_price = self._position_number(
                row, ("avgPrice", "avg_price", "averagePrice", "entryPrice")
            )
            if avg_price is None or not 0.0 < avg_price <= 1.0:
                raise RuntimeError(
                    "cannot reconcile real positions: wallet position has "
                    f"no valid average price for token {token_id[:12]}…"
                )

            key = (condition_id, token_id)
            previous = holdings.get(key)
            if previous is None:
                holdings[key] = TokenHolding(
                    condition_id=condition_id,
                    token_id=token_id,
                    size_shares=size,
                    avg_price=avg_price,
                )
            else:
                total_shares = previous.size_shares + size
                weighted_price = (
                    previous.size_shares * previous.avg_price
                    + size * avg_price
                ) / total_shares
                holdings[key] = TokenHolding(
                    condition_id=condition_id,
                    token_id=token_id,
                    size_shares=total_shares,
                    avg_price=weighted_price,
                )
        return list(holdings.values())

    @staticmethod
    def _position_number(
        row: Dict[str, Any], keys: tuple[str, ...]
    ) -> Optional[float]:
        for key in keys:
            value = row.get(key)
            if value is None or value == "":
                continue
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                return parsed
        return None

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
        """Place a real signed order. Requires py-clob-client-v2 installed and
        wallet configured.

        The returned receipt always includes ``order_status`` (one of
        ``accepted``, ``partially_filled``, ``filled``, or ``rejected``) and
        ``filled_size``.  An accepted order is not a fill: callers must only
        update positions from the reported filled size.
        """
        if self._clob_signer is None:
            try:
                # Lazy import — allows paper mode without the dependency
                from py_clob_client_v2 import (  # type: ignore
                    ClobClient,
                    SignatureTypeV2,
                )
            except ImportError as e:
                raise RuntimeError(
                    "Real-mode trading requires py-clob-client-v2: "
                    "install py-clob-client-v2"
                ) from e

            w = self.cfg.wallet
            if not (w.private_key and w.funder):
                raise RuntimeError("wallet not configured for real mode")

            client = ClobClient(
                host=self.cfg.clob_host,
                key=w.private_key,
                chain_id=self.cfg.chain_id,
                # Current Polymarket accounts use the deposit-wallet flow.
                # The configured funder must be the deposit wallet shown by
                # Polymarket, not the signer EOA.
                signature_type=SignatureTypeV2.POLY_1271,
                funder=w.funder,
            )
            # Derive API creds if needed
            try:
                creds = client.create_or_derive_api_key()
                client.set_api_creds(creds)
            except Exception as e:
                log.warning(f"could not derive API creds: {e}")
            self._clob_signer = client

        from py_clob_client_v2 import (  # type: ignore
            MarketOrderArgs,
            OrderArgs,
            OrderType,
            Side,
        )
        side_const = Side.BUY if side.upper() == "BUY" else Side.SELL

        if side.upper() == "BUY":
            # Polymarket validates BUY maker amounts as USDC with max two
            # decimals, while the taker share amount may use four. The
            # regular limit-order builder can produce a maker amount such as
            # 3.9988 from a $4 target, which the API rejects. Use the
            # market-order API with the current ask as its explicit limit
            # price; its builder emits the required 2/4-decimal amounts.
            buy_amount = float(
                (Decimal(str(price)) * Decimal(str(size))).quantize(
                    Decimal("0.01"), rounding=ROUND_DOWN
                )
            )
            if buy_amount <= 0:
                raise ValueError("BUY amount rounds down to zero USDC")
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=buy_amount,
                price=price,
                side=side_const,
                order_type=OrderType.FOK,
            )
        else:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_const,
            )

        # Run the blocking CLOB client call in a thread
        loop = asyncio.get_running_loop()
        # FOK prevents a real order from sitting live after this call returns.
        # The engine records a position only after the CLOB confirms a match.
        if side.upper() == "BUY":
            order = await loop.run_in_executor(
                None,
                lambda: self._clob_signer.create_and_post_market_order(
                    order_args,
                    order_type=OrderType.FOK,
                ),
            )
        else:
            order = await loop.run_in_executor(
                None,
                lambda: self._clob_signer.create_and_post_order(
                    order_args,
                    order_type=OrderType.FOK,
                ),
            )
        receipt = order if isinstance(order, dict) else {"raw": str(order)}
        order_id = str(receipt.get("orderID") or receipt.get("id") or "")

        if self._order_status(receipt, size) not in {
            "filled", "partially_filled", "rejected"
        }:
            # Some responses omit the final status while the CLOB is resolving
            # transaction hashes. Query the order before returning an accepted
            # or partial result.
            for _ in range(3):
                if not order_id:
                    break
                await asyncio.sleep(0.25)
                try:
                    detail = await loop.run_in_executor(
                        None, lambda: self._clob_signer.get_order(order_id)
                    )
                except Exception as e:
                    log.warning(f"could not confirm order {order_id[:10]}…: {e}")
                    continue
                if isinstance(detail, dict):
                    receipt = {**receipt, **detail}
                    if self._order_status(receipt, size) in {
                        "filled", "partially_filled", "rejected"
                    }:
                        break

        status = self._order_status(receipt, size)
        filled_size = self._filled_size(receipt, size, status)
        receipt = {
            **receipt,
            "order_status": status,
            "filled_size": filled_size,
            "fill_price": self._fill_price(receipt, price),
        }

        # FOK should not leave a remainder live. Cancel an accepted or
        # partially-filled remainder before handing the result to the engine.
        if status in {"accepted", "partially_filled"} and order_id:
            await self.cancel_order(order_id)

        log.info(
            f"order result: token={token_id[:8]}… side={side} "
            f"status={status} filled={filled_size:.6f}/{size:.6f}"
        )
        return receipt

    @staticmethod
    def _order_status(order: Dict[str, Any], requested_size: float) -> str:
        """Normalize a CLOB response without treating acceptance as a fill."""
        status = str(order.get("status") or "").strip().lower()
        filled_size = PolymarketClient._filled_size(order, requested_size, "")
        if filled_size >= requested_size * 0.999:
            return "filled"
        if filled_size > 0:
            return "partially_filled"
        if status in {
            "rejected", "reject", "failed", "failure", "cancelled",
            "canceled", "cancel", "expired", "invalid",
        }:
            return "rejected"
        return "accepted"

    @staticmethod
    def _filled_size(
        order: Dict[str, Any], requested_size: float, normalized_status: str
    ) -> float:
        """Read the actual matched size from the CLOB response."""
        for key in (
            "size_matched", "sizeMatched", "filled_size", "filledSize",
            "matched_size", "matchedSize", "executed_size", "executedSize",
        ):
            value = order.get(key)
            if value is None:
                continue
            try:
                return min(requested_size, max(0.0, float(value)))
            except (TypeError, ValueError):
                continue
        # FOK responses can expose only a terminal matched status. In that
        # case the terminal status is the confirmation for the requested size.
        status = str(order.get("status") or "").strip().lower()
        if normalized_status == "filled" or status in {
            "matched", "filled", "executed", "complete", "completed",
        }:
            return requested_size
        return 0.0

    @staticmethod
    def _fill_price(order: Dict[str, Any], fallback: float) -> float:
        for key in ("avg_price", "average_price", "avgPrice", "fill_price", "price"):
            value = order.get(key)
            if value is None:
                continue
            try:
                parsed = float(value)
                if parsed > 0:
                    return parsed
            except (TypeError, ValueError):
                continue
        return fallback

    @staticmethod
    def _order_is_filled(order: Dict[str, Any], requested_size: float) -> bool:
        """Backward-compatible full-fill predicate for callers and tests."""
        return PolymarketClient._order_status(order, requested_size) == "filled"

    async def cancel_order(self, order_id: str) -> bool:
        if self._clob_signer is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            cancel = getattr(self._clob_signer, "cancel_order", None)
            if cancel is None:
                cancel = getattr(self._clob_signer, "cancel")
            await loop.run_in_executor(
                None, lambda: cancel(order_id)
            )
            return True
        except Exception as e:
            log.warning(f"cancel failed for {order_id}: {e}")
            return False
