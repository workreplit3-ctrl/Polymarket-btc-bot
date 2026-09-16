---
name: Real-market redemption
description: How resolved real Polymarket positions release the bot's one-position limit.
---

Winning BTC Up/Down positions normally leave the wallet through Polymarket redemption rather than a CLOB SELL. The local trade journal may therefore contain only an OPEN row, while the wallet is already flat.

**Why:** Treating redemption as an ordinary close leaves the in-memory risk position open and blocks new entries when `max_open_positions` is one.

**How to apply:** After a tracked market expires, refresh active markets and reconcile wallet holdings. Keep recently observed market metadata in the reconciliation lookup so unredeemed shares still count as exposure; clear the position only when the wallet no longer holds them.