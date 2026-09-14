---
name: CLOB order API
description: Compatibility constraint for submitting signed Polymarket orders with py-clob-client.
---

The installed py-clob-client interface submits orders through `create_and_post_order(OrderArgs(...))`; passing token, price, size, and side as separate keyword arguments is not compatible with current releases.

**Why:** Paper mode bypasses the signer, so this mismatch only appears when switching to real mode and can make every otherwise-valid signal fail before reaching Polymarket.

**How to apply:** When upgrading or changing py-clob-client, inspect the installed `ClobClient.create_and_post_order` signature and keep a no-network regression test that verifies the wrapper constructs `OrderArgs`.