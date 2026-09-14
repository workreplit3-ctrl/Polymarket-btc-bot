---
name: Polymarket market discovery
description: Non-obvious Gamma API behavior relevant to BTC up/down market discovery.
---

The Gamma `/markets` endpoint can return older BTC up/down markets even when `active=true` and `closed=false`. Current crypto markets are reliably found by ordering by `createdAt` descending; current responses may expose `liquidityNum` instead of `volume`.

**Why:** Filtering the default first page produced no tradable markets because the returned BTC series had already expired timestamps despite being marked active.

**How to apply:** Keep market discovery newest-first, reject markets whose parsed end time is stale, and use liquidity fields only as a fallback when volume is absent.