import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.calibration import calibrated_probability


def test_market_logit_calibration_is_monotonic_and_bounded():
    kwargs = {
        "market_logit_intercept": 0.18,
        "market_logit_slope": 0.59,
        "raw_model_weight": 0.0,
    }
    low = calibrated_probability(0.99, 0.10, **kwargs)
    middle = calibrated_probability(0.01, 0.50, **kwargs)
    high = calibrated_probability(0.01, 0.90, **kwargs)

    assert 0.001 <= low < middle < high <= 0.999


def test_raw_model_is_disabled_until_holdout_validates_it():
    kwargs = {
        "market_logit_intercept": 0.18,
        "market_logit_slope": 0.59,
        "raw_model_weight": 0.0,
    }
    assert calibrated_probability(0.99, 0.40, **kwargs) == pytest.approx(
        calibrated_probability(0.01, 0.40, **kwargs)
    )


def test_validated_raw_weight_moves_probability_only_when_explicitly_enabled():
    kwargs = {
        "market_logit_intercept": 0.18,
        "market_logit_slope": 0.59,
        "raw_model_weight": 0.25,
    }
    base = calibrated_probability(0.50, 0.40, **{**kwargs, "raw_model_weight": 0.0})
    adjusted = calibrated_probability(0.99, 0.40, **kwargs)
    assert adjusted > base