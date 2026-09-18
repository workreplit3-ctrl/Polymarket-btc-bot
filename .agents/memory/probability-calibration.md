---
name: Probability calibration
description: Empirical rule for selecting the BTC 5-minute probability baseline and keeping unsupported model signals out of real trading.
---

Use the market-mid probability as the calibration benchmark, but do not use it
alone as the executable entry forecast: with a valid orderbook, market mid is
at or below ask, so midpoint-only logic cannot create positive buy edge. The
current runtime explicitly restores the independent spot/volatility value
signal, with a conservative entry threshold and a $1 per-trade cap.

**Why:** Short-horizon BTC signals are highly correlated within each market,
and the chronological holdout favored the market prior on pure probability
metrics. That benchmark was mistakenly promoted to the only runtime forecast,
which made real entries mathematically impossible. Restoring the independent
signal fixes execution availability, but it remains a higher-risk operator
override until more rolling evidence is collected.

**How to apply:** Keep the market prior for calibration reports and rerun the
offline evaluator as the resolved sample grows. Validate the independent
signal with executable ask prices, not only midpoint Brier/log loss, and keep
the per-trade cap, one-position limit, reconciliation, pause, and FOK guards
active.