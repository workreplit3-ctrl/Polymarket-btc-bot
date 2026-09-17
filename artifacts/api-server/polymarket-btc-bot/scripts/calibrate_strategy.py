#!/usr/bin/env python3
"""Build a chronological calibration report from resolved BTC 5m markets.

Usage:
    python scripts/calibrate_strategy.py \
        --db data/bot.db \
        --out data/calibration_report.json

This is deliberately offline with respect to trading: it only reads SQLite
signals and the public Gamma market-resolution endpoint. It never reads wallet
secrets and never submits orders.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-{}"
ENTRY_MIN_REMAINING = 150.0
ENTRY_MAX_REMAINING = 270.0
TARGET_REMAINING = 210.0


def _clip(p: float) -> float:
    return min(0.999, max(0.001, float(p)))


def _logit(p: float) -> float:
    p = _clip(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def _fetch_outcome(slot: int) -> tuple[int, int | None]:
    url = GAMMA_MARKET_URL.format(slot)
    request = urllib.request.Request(url, headers={"User-Agent": "btc-bot-calibrator/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        outcomes = json.loads(payload.get("outcomes", "[]"))
        prices = json.loads(payload.get("outcomePrices", "[]"))
        labels = {
            str(label).strip().lower(): float(price)
            for label, price in zip(outcomes, prices)
        }
        up_price = labels.get("up", labels.get("yes"))
        if up_price is None or up_price == 0.5:
            return slot, None
        return slot, int(up_price > 0.5)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return slot, None


def _load_selected(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT ts, action, btc_drift_pct, our_prob_up, market_prob_up,
                       actionable_edge_up, actionable_edge_down
                FROM signals
                WHERE ts > 0
                  AND our_prob_up > 0
                  AND our_prob_up < 1
                  AND market_prob_up > 0
                  AND market_prob_up < 1
                ORDER BY ts
                """
            )
        ]

    by_slot: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        slot = int(float(row["ts"]) // 300) * 300
        remaining = slot + 300 - float(row["ts"])
        if ENTRY_MIN_REMAINING <= remaining <= ENTRY_MAX_REMAINING:
            row["slot"] = slot
            row["remaining"] = remaining
            by_slot.setdefault(slot, []).append(row)

    selected = []
    for slot, candidates in by_slot.items():
        selected.append(
            min(candidates, key=lambda row: abs(row["remaining"] - TARGET_REMAINING))
        )
    return sorted(selected, key=lambda row: row["slot"])


def _fit_market_calibration(rows: list[dict[str, Any]]) -> tuple[float, float]:
    """Fit a regularized one-feature logistic calibration with stdlib only."""
    intercept = 0.0
    slope = 0.0
    for _ in range(20_000):
        grad_intercept = 0.0
        grad_slope = 0.0
        for row in rows:
            x = _logit(float(row["market_prob_up"]))
            prediction = _sigmoid(intercept + slope * x)
            error = prediction - int(row["outcome_up"])
            grad_intercept += error
            grad_slope += error * x
        n = max(1, len(rows))
        # Mild L2 penalty keeps a short rolling sample from producing extreme
        # coefficients. The intercept is not penalized.
        intercept -= 0.02 * (grad_intercept / n + 0.5 * intercept / n)
        slope -= 0.02 * (grad_slope / n + 0.5 * slope / n)
    return intercept, slope


def _metrics(rows: list[dict[str, Any]], fn) -> dict[str, float]:
    if not rows:
        return {"brier": 0.0, "log_loss": 0.0, "accuracy": 0.0}
    brier = 0.0
    log_loss = 0.0
    correct = 0
    for row in rows:
        prediction = _clip(fn(row))
        outcome = int(row["outcome_up"])
        brier += (prediction - outcome) ** 2
        log_loss -= outcome * math.log(prediction)
        log_loss -= (1 - outcome) * math.log(1 - prediction)
        correct += int((prediction >= 0.5) == bool(outcome))
    n = len(rows)
    return {
        "brier": brier / n,
        "log_loss": log_loss / n,
        "accuracy": correct / n,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    selected = _load_selected(args.db)
    slots = sorted({int(row["slot"]) for row in selected})
    outcomes: dict[int, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for slot, outcome in executor.map(_fetch_outcome, slots):
            if outcome is not None:
                outcomes[slot] = outcome

    resolved = [
        row for row in selected if row["slot"] in outcomes
    ]
    for row in resolved:
        row["outcome_up"] = outcomes[row["slot"]]

    split = int(len(resolved) * 0.70)
    train = resolved[:split]
    test = resolved[split:]
    intercept, slope = _fit_market_calibration(train) if train else (0.0, 1.0)

    def calibrated(row):
        return _sigmoid(
            intercept + slope * _logit(float(row["market_prob_up"]))
        )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "markets": {
            "selected": len(selected),
            "resolved": len(resolved),
            "train": len(train),
            "test": len(test),
            "up_rate": (
                sum(row["outcome_up"] for row in resolved) / len(resolved)
                if resolved else None
            ),
        },
        "chronological_holdout": {
            "raw_logged_probability": _metrics(
                test, lambda row: float(row["our_prob_up"])
            ),
            "market_mid_probability": _metrics(
                test, lambda row: float(row["market_prob_up"])
            ),
            "calibrated_market_probability": _metrics(test, calibrated),
        },
        "runtime_parameters": {
            # The identity market baseline wins when the fitted map does not
            # improve the chronological holdout. Keep the fitted candidate in
            # the report for inspection, but do not deploy it automatically.
            "calibration_market_logit_intercept": 0.0,
            "calibration_market_logit_slope": 1.0,
            "calibration_raw_model_weight": 0.0,
        },
        "fitted_candidate": {
            "calibration_market_logit_intercept": intercept,
            "calibration_market_logit_slope": slope,
        },
        "warning": (
            "Do not enable real entries from this report alone; require a "
            "larger rolling sample and paper out-of-sample confirmation."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())