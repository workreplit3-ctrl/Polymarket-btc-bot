---
name: Polymarket market discovery
description: Non-obvious Gamma API behavior relevant to BTC up/down market discovery.
---

The Gamma `/markets` endpoint can return pre-created BTC up/down markets for the following day even when `active=true` and `closed=false`. Direct lookup of the current rounded 5m slug is more reliable; responses may expose high liquidity with very low matched volume.

**Why:** The ordered active list can surface future markets before the current one, while a fresh current market may have little matched volume but a deep order book.

**How to apply:** Prefer direct current 5m slug discovery, enforce a narrow resolution-time window, reject future/long-horizon markets, and use liquidity as the eligibility proxy when it is stronger than matched volume.