"""Calibrated probability model for short-horizon binary markets.

The first production calibration uses the market probability as the prior.
The raw spot/volatility normal model is retained as an input, but its learned
weight is currently zero because it degraded the chronological holdout.
"""
from __future__ import annotations

import math


def _clip_probability(value: float) -> float:
    return min(0.999, max(0.001, float(value)))


def _logit(value: float) -> float:
    p = _clip_probability(value)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, value))))


def calibrated_probability(
    raw_model_prob_up: float,
    market_prob_up: float,
    *,
    market_logit_intercept: float,
    market_logit_slope: float,
    raw_model_weight: float,
) -> float:
    """Return a probability calibrated against resolved-market outcomes.

    The market calibration is a regularized logistic map:

        logit(q) = intercept + slope * logit(market_probability)

    ``raw_model_weight`` allows a future out-of-sample validated value signal
    to move q away from that prior.  It is intentionally zero in the current
    calibration because the raw normal model was worse than the market prior
    on the chronological holdout.
    """
    market_q = _sigmoid(
        market_logit_intercept
        + market_logit_slope * _logit(market_prob_up)
    )
    weight = min(1.0, max(0.0, float(raw_model_weight)))
    blended = market_q + weight * (
        _clip_probability(raw_model_prob_up) - market_q
    )
    return min(0.999, max(0.001, blended))