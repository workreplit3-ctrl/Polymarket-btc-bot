import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from py_clob_client_v2 import MarketOrderArgs, OrderArgs, OrderType, Side

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


def test_real_mode_readiness_reports_paper_only_without_wallet() -> None:
    cfg = empty_paper_config()

    readiness = PolymarketClient(cfg.polymarket).real_mode_readiness()

    assert readiness.ready is False
    assert "wallet is not configured" in readiness.reason
    assert "private key" in readiness.reason
    assert "None" not in readiness.reason


def test_real_mode_readiness_reports_ready_without_exposing_wallet_values() -> None:
    cfg = empty_paper_config()
    private_key = "test-private-key"
    funder = "0x0000000000000000000000000000000000000001"
    cfg.polymarket.wallet.private_key = private_key
    cfg.polymarket.wallet.funder = funder

    readiness = PolymarketClient(cfg.polymarket).real_mode_readiness()

    assert readiness.ready is True
    assert "compatible" in readiness.reason
    assert private_key not in readiness.reason
    assert funder not in readiness.reason


class RecordingSigner:
    """Accept only the current py-clob-client-v2 order call shape."""

    def __init__(self) -> None:
        self.calls: list[tuple[OrderArgs, OrderType]] = []
        self.market_calls: list[tuple[MarketOrderArgs, OrderType]] = []

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

    def create_and_post_market_order(
        self,
        order_args: MarketOrderArgs,
        *,
        order_type: OrderType,
    ) -> dict[str, Any]:
        self.market_calls.append((order_args, order_type))
        return {
            "orderID": "test-market-order",
            "status": "matched",
            "size_matched": str(order_args.amount / order_args.price),
        }


def test_real_buy_order_uses_market_amount_precision_without_network() -> None:
    """BUY maker amount must be cents-precise while staying FOK."""
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
    assert signer.calls == []
    assert len(signer.market_calls) == 1
    order_args, order_type = signer.market_calls[0]
    assert isinstance(order_args, MarketOrderArgs)
    assert order_args.token_id == "token-123"
    assert order_args.price == 0.42
    assert order_args.amount == 1.05
    assert order_args.side is Side.BUY
    assert order_type is OrderType.FOK


def test_real_sell_order_keeps_limit_order_shape_without_network() -> None:
    """SELL orders continue using the regular limit-order API."""
    client = object.__new__(PolymarketClient)
    signer = RecordingSigner()
    client._clob_signer = signer

    receipt = asyncio.run(
        client.place_order(
            token_id="token-123",
            side="SELL",
            price=0.42,
            size=2.5,
        )
    )

    assert receipt["status"] == "matched"
    assert len(signer.calls) == 1
    assert signer.market_calls == []
    order_args, order_type = signer.calls[0]
    assert isinstance(order_args, OrderArgs)
    assert order_args.token_id == "token-123"
    assert order_args.price == 0.42
    assert order_args.size == 2.5
    assert order_args.side is Side.SELL
    assert order_type is OrderType.FOK


TERMINAL_ORDER_FIXTURES = (
    pytest.param(
        {"orderID": "accepted", "status": "open"},
        "accepted",
        0.0,
        id="terminal-accepted",
    ),
    pytest.param(
        {"orderID": "partial", "status": "matched", "size_matched": 4.0},
        "partially_filled",
        4.0,
        id="terminal-partial-numeric-size",
    ),
    pytest.param(
        {"orderID": "filled", "status": "matched", "filledSize": "10"},
        "filled",
        10.0,
        id="terminal-filled-string-size",
    ),
    pytest.param(
        {"orderID": "rejected", "status": "cancelled"},
        "rejected",
        0.0,
        id="terminal-rejected",
    ),
)


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_filled"),
    TERMINAL_ORDER_FIXTURES,
)
def test_order_status_maps_terminal_response_fixtures(
    payload: dict[str, Any],
    expected_status: str,
    expected_filled: float,
) -> None:
    requested = 10.0

    assert PolymarketClient._order_status(payload, requested) == expected_status
    assert PolymarketClient._filled_size(
        payload, requested, expected_status
    ) == pytest.approx(expected_filled)


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    (
        pytest.param({"orderID": "missing-status"}, "accepted", id="no-status-no-fill"),
        pytest.param(
            {"orderID": "missing-status-partial", "sizeMatched": "2.5"},
            "partially_filled",
            id="no-status-string-partial-fill",
        ),
    ),
)
def test_order_status_handles_missing_status_field(
    payload: dict[str, Any], expected_status: str
) -> None:
    assert PolymarketClient._order_status(payload, 10.0) == expected_status


class ResponseSigner(RecordingSigner):
    """Return deterministic create/get/cancel responses without network access."""

    def __init__(
        self, initial: dict[str, Any], polled: list[dict[str, Any]]
    ) -> None:
        super().__init__()
        self.initial = initial
        self.polled = list(polled)
        self.get_order_calls: list[str] = []
        self.cancelled: list[str] = []

    def create_and_post_order(
        self,
        order_args: OrderArgs,
        *,
        order_type: OrderType,
    ) -> dict[str, Any]:
        self.calls.append((order_args, order_type))
        return dict(self.initial)

    def create_and_post_market_order(
        self,
        order_args: MarketOrderArgs,
        *,
        order_type: OrderType,
    ) -> dict[str, Any]:
        self.market_calls.append((order_args, order_type))
        return dict(self.initial)

    def get_order(self, order_id: str) -> dict[str, Any]:
        self.get_order_calls.append(order_id)
        if self.polled:
            return dict(self.polled.pop(0))
        return {}

    def cancel_order(self, order_id: str) -> None:
        self.cancelled.append(order_id)


POLLED_ORDER_FIXTURES = (
    pytest.param(
        {"orderID": "polled-accepted", "status": "open"},
        [{"status": "open"}],
        "accepted",
        0.0,
        id="polled-accepted",
    ),
    pytest.param(
        {"orderID": "polled-partial", "status": "open"},
        [{"status": "matched", "sizeMatched": "4"}],
        "partially_filled",
        4.0,
        id="polled-partial-string-size",
    ),
    pytest.param(
        {"orderID": "polled-filled", "status": "open"},
        [{"status": "matched", "size_matched": 10.0}],
        "filled",
        10.0,
        id="polled-filled-numeric-size",
    ),
    pytest.param(
        {"orderID": "polled-rejected", "status": "open"},
        [{"status": "cancelled"}],
        "rejected",
        0.0,
        id="polled-rejected",
    ),
)


@pytest.mark.parametrize(
    ("initial", "polled", "expected_status", "expected_filled"),
    POLLED_ORDER_FIXTURES,
)
def test_place_order_maps_polled_response_fixtures(
    monkeypatch: pytest.MonkeyPatch,
    initial: dict[str, Any],
    polled: list[dict[str, Any]],
    expected_status: str,
    expected_filled: float,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("src.polymarket_client.asyncio.sleep", no_sleep)
    client = object.__new__(PolymarketClient)
    signer = ResponseSigner(initial, polled)
    client._clob_signer = signer

    receipt = asyncio.run(
        client.place_order(
            token_id="token-123",
            side="BUY",
            price=0.42,
            size=10.0,
        )
    )

    assert receipt["order_status"] == expected_status
    assert receipt["filled_size"] == pytest.approx(expected_filled)
    expected_poll_count = 3 if expected_status == "accepted" else 1
    assert signer.get_order_calls == [initial["orderID"]] * expected_poll_count
    if expected_status in {"accepted", "partially_filled"}:
        assert signer.cancelled == [initial["orderID"]]
    else:
        assert signer.cancelled == []


def test_place_order_cancels_remainder_after_partial_fill() -> None:
    client = object.__new__(PolymarketClient)
    signer = ResponseSigner(
        {"orderID": "partial-order", "status": "matched", "sizeMatched": "4"},
        [],
    )
    client._clob_signer = signer

    receipt = asyncio.run(
        client.place_order(
            token_id="token-123",
            side="BUY",
            price=0.42,
            size=10.0,
        )
    )

    assert receipt["order_status"] == "partially_filled"
    assert receipt["filled_size"] == 4.0
    assert signer.cancelled == ["partial-order"]


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
