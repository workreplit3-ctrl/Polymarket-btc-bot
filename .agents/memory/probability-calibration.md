---
name: Probability calibration
description: Empirical rule for selecting the BTC 5-minute probability baseline and keeping unsupported model signals out of real trading.
---

Use the market-mid probability as the runtime baseline unless a chronological
holdout shows that an added model component improves Brier/log loss and
selective net expectancy after executable prices and costs. The current
resolved-market sample did not validate the raw normal approximation or a
learned market-logit remap, so the raw-model weight remains zero and real
entries stay disabled.

**Why:** Short-horizon BTC signals are highly correlated within each market,
and weighting every tick overstates evidence. One entry-window snapshot per
resolved market with a chronological split exposed the raw model's
overconfidence and prevented a misleading in-sample improvement from reaching
real trading.

**How to apply:** Re-run the offline calibrator as the resolved sample grows.
Only replace the identity market baseline after a larger out-of-sample
validation confirms improvement; retain paper mode and the real-entry gate
until then.