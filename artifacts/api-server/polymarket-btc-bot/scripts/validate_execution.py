#!/usr/bin/env python3
"""Validate logged BTC signals with executable entry prices and settlement P/L.

The bot stores the best-ask actionable edge at each decision. This report
reconstructs the observed ask, joins each market to its resolved Gamma outcome,
and evaluates hold-to-settlement paper P/L across entry thresholds.

This is intentionally read-only with respect to the bot database and never
loads wallet credentials or submits orders.

Usage:
    python scripts/validate_execution.py \
        --db data/bot.db \
        --out data/execution_validation.json
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
from typing import Any, Iterable


GAMMA_MARKET_URL = "https://gamma-api.polymarket.com/markets/slug/btc-updown-5m-{}"
ENTRY_MIN_REMAINING = 150.0
ENTRY_MAX_REMAINING = 270.0
TARGET_REMAINING = 210.0
DEFAULT_THRESHOLDS = (0.0, 0.025, 0.05, 0.10, 0.15)


def _clip(value: float) -> float:
    return min(0.999, max(0.001, float(value)))


def _fetch_outcome(slot: int) -> tuple[int, int | None]:
    request = urllib.request.Request(
        GAMMA_MARKET_URL.format(slot),
        headers={"User-Agent": "btc-bot-execution-validator/1.0"},
    )
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


def _load_signals(db_path: Path) -> list[dict[str, Any]]:
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30.0) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT ts, action, btc_drift_pct, our_prob_up, market_prob_up,
                       actionable_edge_up, actionable_edge_down, reason
                FROM signals
                WHERE ts > 0
                  AND our_prob_up > 0
                  AND our_prob_up < 1
                  AND market_prob_up > 0
                  AND market_prob_up < 1
                  AND actionable_edge_up IS NOT NULL
                  AND actionable_edge_down IS NOT NULL
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

    selected = [
        min(candidates, key=lambda row: abs(row["remaining"] - TARGET_REMAINING))
        for candidates in by_slot.values()
    ]
    return sorted(selected, key=lambda row: row["slot"])


def _prepare_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared = []
    for row in rows:
        our_prob = float(row["our_prob_up"])
        up_gross = float(row["actionable_edge_up"])
        down_gross = float(row["actionable_edge_down"])
        side = "UP" if up_gross >= down_gross else "DOWN"
        gross = up_gross if side == "UP" else down_gross
        hold_value = our_prob if side == "UP" else 1.0 - our_prob
        ask = hold_value - gross
        if not (0.0 < ask < 1.0 and math.isfinite(ask)):
            continue
        prepared.append(
            {
                **row,
                "side": side,
                "gross_edge": gross,
                "ask": ask,
                "hold_value": hold_value,
            }
        )
    return prepared


def _metrics(rows: list[dict[str, Any]], threshold: float, cost_buffer: float) -> dict[str, Any]:
    eligible = [
        row
        for row in rows
        if float(row["gross_edge"]) - cost_buffer >= threshold
    ]
    trades = []
    for row in eligible:
        outcome_up = int(row["outcome_up"])
        won = (row["side"] == "UP" and outcome_up == 1) or (
            row["side"] == "DOWN" and outcome_up == 0
        )
        ask = float(row["ask"])
        shares = 1.0 / ask
        settlement = 1.0 if won else 0.0
        gross_pnl = shares * (settlement - ask)
        net_pnl = shares * (settlement - ask - cost_buffer)
        trades.append(
            {
                "slot": row["slot"],
                "side": row["side"],
                "ask": ask,
                "gross_edge": float(row["gross_edge"]),
                "outcome_up": outcome_up,
                "won": won,
                "gross_pnl_usdc": gross_pnl,
                "net_pnl_usdc": net_pnl,
            }
        )
    pnl = sum(float(trade["net_pnl_usdc"]) for trade in trades)
    wins = sum(bool(trade["won"]) for trade in trades)
    return {
        "threshold": threshold,
        "eligible_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": wins / len(trades) if trades else 0.0,
        "net_pnl_usdc": pnl,
        "avg_net_pnl_usdc": pnl / len(trades) if trades else 0.0,
        "trades": trades,
    }


def _split_metrics(
    rows: list[dict[str, Any]], threshold: float, cost_buffer: float
) -> dict[str, Any]:
    split = int(len(rows) * 0.70)
    train = rows[:split]
    test = rows[split:]
    return {
        "train": _metrics(train, threshold, cost_buffer),
        "chronological_holdout": _metrics(test, threshold, cost_buffer),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cost-buffer", type=float, default=0.02)
    parser.add_argument(
        "--thresholds",
        type=float,
        nargs="+",
        default=list(DEFAULT_THRESHOLDS),
    )
    args = parser.parse_args()

    selected = _prepare_rows(_load_signals(args.db))
    slots = sorted({int(row["slot"]) for row in selected})
    outcomes: dict[int, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for slot, outcome in executor.map(_fetch_outcome, slots):
            if outcome is not None:
                outcomes[slot] = outcome

    resolved = []
    for row in selected:
        if row["slot"] in outcomes:
            resolved.append({**row, "outcome_up": outcomes[row["slot"]]})

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": {
            "signals": "read-only SQLite signal journal",
            "entry_price": "reconstructed from logged best-ask actionable edge",
            "settlement": "resolved Gamma market outcome",
            "exit_model": "hold to settlement; early exits are not inferred",
        },
        "sample": {
            "selected_market_slots": len(selected),
            "resolved_market_slots": len(resolved),
            "train_slots": int(len(resolved) * 0.70),
            "holdout_slots": len(resolved) - int(len(resolved) * 0.70),
        },
        "cost_buffer": args.cost_buffer,
        "thresholds": {
            str(threshold): _split_metrics(resolved, threshold, args.cost_buffer)
            for threshold in args.thresholds
        },
        "warning": (
            "This validates logged historical signals and settlement outcomes. "
            "It is not proof of future profitability and does not model early "
            "exit fills or queue position."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())