"""Calibrated probability model for short-horizon binary markets.

The market probability remains the prior.  The independent spot/volatility
value model can be blended back in when the operator explicitly chooses a
tradeable runtime configuration; this is necessary because a market midpoint
alone cannot produce positive executable edge against an ask.
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

    ``raw_model_weight`` controls how much the independent value signal can
    move q away from that prior.  The offline report still records the market
    prior as the calibration benchmark; runtime may explicitly choose the
    independent model so the execution layer has a tradeable forecast.
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