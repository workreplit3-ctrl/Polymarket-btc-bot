import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from py_clob_client_v2 import OrderArgs, OrderType, Side

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.polymarket_client import PolymarketClient
from src.config import empty_paper_config
from src.polymarket_client import MarketInfo


def test_real_mode_accepts_installed_order_interface() -> None:
    PolymarketClient.validate_real_mode()


def test_real_mode_rejects_legacy_create_order_signature(monkeypatch) -> None:
    from py_clob_client_v2 import ClobClient

    def legacy_create_order(self, token_id, price, size, side):
        return None

    monkeypatch.setattr(ClobClient, "create_and_post_order", legacy_create_order)

    with pytest.raises(RuntimeError, match="order API is incompatible"):
        PolymarketClient.validate_real_mode()


class RecordingSigner:
    """Accept only the current py-clob-client-v2 order call shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[OrderArgs, OrderType]] = []

    def create_and_post_order(
        self,
        order_args: OrderArgs,
        *,
        order_type: OrderType,
    ) -> dict[str, Any]:
        self.calls.append((order_args, order_type))
        return {
            "orderID": "test-order",
            "status": "matched",
            "size_matched": str(order_args.size),
        }


def test_real_order_uses_current_order_args_shape_without_network() -> None:
    """The wrapper must not fall back to legacy keyword order arguments."""
    client = object.__new__(PolymarketClient)
    signer = RecordingSigner()
    client._clob_signer = signer

    receipt = asyncio.run(
        client.place_order(
            token_id="token-123",
            side="BUY",
            price=0.42,
            size=2.5,
        )
    )

    assert receipt["status"] == "matched"
    assert len(signer.calls) == 1
    order_args, order_type = signer.calls[0]
    assert isinstance(order_args, OrderArgs)
    assert order_args.token_id == "token-123"
    assert order_args.price == 0.42
    assert order_args.size == 2.5
    assert order_args.side is Side.BUY
    assert order_type is OrderType.FOK


def test_order_status_requires_actual_fill_amount() -> None:
    requested = 10.0
    assert PolymarketClient._order_status(
        {"status": "accepted", "orderID": "accepted"}, requested
    ) == "accepted"
    assert PolymarketClient._order_status(
        {"status": "matched", "size_matched": "4"}, requested
    ) == "partially_filled"
    assert PolymarketClient._order_status(
        {"status": "matched", "size_matched": "10"}, requested
    ) == "filled"
    assert PolymarketClient._order_status(
        {"status": "rejected", "orderID": "rejected"}, requested
    ) == "rejected"


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.payload


class FakeHttp:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, *, params: dict[str, Any]) -> FakeResponse:
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def _holding_market() -> MarketInfo:
    return MarketInfo(
        condition_id="condition-1",
        question="BTC up or down?",
        slug="btc-updown-5m-test",
        end_date="2099-01-01T00:00:00Z",
        outcome_up_token_id="up-token",
        outcome_down_token_id="down-token",
        outcomes=["Up", "Down"],
        volume=1000.0,
        active=True,
    )


def test_confirmed_token_holdings_filter_active_markets_and_ignore_zero_balances() -> None:
    cfg = empty_paper_config().polymarket
    cfg.wallet.funder = "0xfunder"
    client = object.__new__(PolymarketClient)
    client.cfg = cfg
    client._http = FakeHttp([
        {
            "conditionId": "condition-1",
            "asset": "up-token",
            "size": "12.5",
            "avgPrice": "0.42",
        },
        {
            "conditionId": "condition-1",
            "asset": "down-token",
            "size": "0",
            "avgPrice": "0.58",
        },
        {
            "conditionId": "other-market",
            "asset": "other-token",
            "size": "100",
            "avgPrice": "0.5",
        },
    ])

    holdings = asyncio.run(client.get_confirmed_token_holdings([_holding_market()]))

    assert len(holdings) == 1
    assert holdings[0].condition_id == "condition-1"
    assert holdings[0].token_id == "up-token"
    assert holdings[0].size_shares == 12.5
    assert holdings[0].avg_price == 0.42
    url, params = client._http.calls[0]
    assert url.endswith("/positions")
    assert params["user"] == "0xfunder"
    assert params["market"] == "condition-1"


def test_confirmed_token_holdings_failure_is_not_silently_flat() -> None:
    cfg = empty_paper_config().polymarket
    cfg.wallet.funder = "0xfunder"
    client = object.__new__(PolymarketClient)
    client.cfg = cfg
    client._http = FakeHttp([
        {
            "conditionId": "condition-1",
            "asset": "up-token",
            "size": "12.5",
        }
    ])

    with pytest.raises(RuntimeError, match="no valid average price"):
        asyncio.run(client.get_confirmed_token_holdings([_holding_market()]))
