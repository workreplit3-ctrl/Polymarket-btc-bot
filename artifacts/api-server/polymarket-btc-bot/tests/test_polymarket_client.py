import asyncio
import sys
from pathlib import Path
from typing import Any

from py_clob_client_v2 import OrderArgs, OrderType, Side

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.polymarket_client import PolymarketClient


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
