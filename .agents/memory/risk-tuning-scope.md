---
name: Entry aggressiveness versus monetary risk
description: Meaning of the operator-approved risk adjustment and its limits.
---

Treat entry aggressiveness and monetary exposure as separate choices; approval to relax entry thresholds is not approval to increase position size or relax exits.

**Why:** On 2026-09-16 the operator explicitly chose lower entry-edge thresholds instead of higher monetary limits. A percentage reduction in a threshold is not a measured percentage increase in trading frequency, expected loss, or profit.

**How to apply:** Preserve that distinction when describing this adjustment or scoping later risk changes. Do not characterize the resulting strategy as empirically “30% riskier.”

Real entries must remain disabled until the probability model is calibrated on resolved BTC markets and shows positive out-of-sample value after spread, fees, and exit slippage.

**Why:** The lowered threshold produced a recent run of six losing real exits totaling about $10.56; the raw normal model's edge was not evidence of predictive value.

**How to apply:** Treat `real_entries_enabled` as a post-validation gate, not an operator override for an uncalibrated model. Keep position management and exits active while new entries are blocked.