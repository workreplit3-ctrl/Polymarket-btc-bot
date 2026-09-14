---
name: CLOB order API
description: Compatibility constraint for submitting signed Polymarket orders with py-clob-client.
---

Current Polymarket CLOB V2 uses `py-clob-client-v2`; submit orders through its V2 `create_and_post_order(OrderArgs(...), order_type=OrderType.GTC)` API. The archived V1 client can be rejected with an order-version error.

**Why:** Paper mode bypasses the signer, so SDK/API-version mismatches only appear when switching to real mode and can make every otherwise-valid signal fail before reaching Polymarket.

**How to apply:** Use the V2 package and inspect the installed `ClobClient` signature when upgrading. Keep a no-network regression test that verifies the wrapper constructs the V2 `OrderArgs` and uses an explicit order type.