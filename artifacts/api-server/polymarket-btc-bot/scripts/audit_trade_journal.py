#!/usr/bin/env python3
"""Audit recorded real fills for inventory and P/L consistency.

This is a read-only audit of the local execution journal. It does not submit
orders, access wallet secrets, or mutate the database. It detects close rows
that cannot be matched to recorded filled opens, over-closes, missing order IDs,
and per-market inventory mismatches. New engine rows include requested and
filled sizes in raw JSON so this audit remains useful for future partial fills.

Usage:
    python scripts/audit_trade_journal.py --db data/bot.db
    python scripts/audit_trade_journal.py --db data/bot.db --out data/trade_audit.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


def _raw(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["raw"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def audit(db_path: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, ts, mode, condition_id, slug, side, action, price,
                   size_shares, size_usdc, pnl, order_id, order_status, raw
            FROM trades
            WHERE mode='real'
            ORDER BY ts, id
            """
        ).fetchall()

    inventory: dict[tuple[str, str], float] = defaultdict(float)
    cost_basis: dict[tuple[str, str], float] = defaultdict(float)
    anomalies: list[dict[str, Any]] = []
    missing_order_ids: list[int] = []
    markets: dict[tuple[str, str], dict[str, Any]] = {}
    total_pnl = 0.0

    for row in rows:
        key = (str(row["condition_id"]), str(row["side"]))
        shares = max(0.0, float(row["size_shares"] or 0.0))
        status = str(row["order_status"] or "").lower()
        if row["action"] in {"OPEN", "CLOSE"} and status not in {
            "filled",
            "partially_filled",
        }:
            anomalies.append(
                {
                    "type": "non_fill_recorded_as_trade",
                    "trade_id": row["id"],
                    "status": row["order_status"],
                }
            )
        if row["action"] in {"OPEN", "CLOSE"} and not str(row["order_id"] or ""):
            missing_order_ids.append(int(row["id"]))

        market = markets.setdefault(
            key,
            {
                "condition_id": row["condition_id"],
                "slug": row["slug"],
                "side": row["side"],
                "open_shares": 0.0,
                "close_shares": 0.0,
                "pnl": 0.0,
            },
        )
        if row["action"] == "OPEN":
            inventory[key] += shares
            cost_basis[key] += shares * float(row["price"] or 0.0)
            market["open_shares"] += shares
        elif row["action"] == "CLOSE":
            available = inventory[key]
            if available <= 1e-9:
                anomalies.append(
                    {
                        "type": "close_without_recorded_open",
                        "trade_id": row["id"],
                        "slug": row["slug"],
                        "side": row["side"],
                        "close_shares": shares,
                        "entry_price_in_raw": _raw(row).get("entry_price"),
                    }
                )
            elif shares > available + max(1e-8, available * 1e-8):
                anomalies.append(
                    {
                        "type": "close_exceeds_recorded_inventory",
                        "trade_id": row["id"],
                        "slug": row["slug"],
                        "side": row["side"],
                        "close_shares": shares,
                        "available_shares": available,
                    }
                )
            consumed = min(shares, available)
            inventory[key] = max(0.0, available - consumed)
            cost_basis[key] = max(
                0.0,
                cost_basis[key]
                - consumed
                * (
                    cost_basis[key] / available
                    if available > 0
                    else float(row["price"] or 0.0)
                ),
            )
            market["close_shares"] += shares
            market["pnl"] += float(row["pnl"] or 0.0)
            total_pnl += float(row["pnl"] or 0.0)

    open_positions = []
    for key, shares in inventory.items():
        if shares > 1e-9:
            condition_id, side = key
            open_positions.append(
                {
                    "condition_id": condition_id,
                    "side": side,
                    "shares": shares,
                    "cost_basis_usdc": cost_basis[key],
                }
            )

    return {
        "trades": len(rows),
        "total_realized_pnl_usdc": total_pnl,
        "markets": list(markets.values()),
        "unmatched_open_inventory": open_positions,
        "missing_order_ids": missing_order_ids,
        "anomalies": anomalies,
        "status": "clean" if not anomalies else "review_required",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = audit(args.db)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())