#!/usr/bin/env python3
"""
Aggregate journal trades closed with Groq zone exits:

  strategy_exit:below_demand_zone  (long)
  strategy_exit:above_supply_zone   (short)

Reads ``exit_*`` keys merged into ``indicator_snapshot_json`` (see
``GroqRecommendationsStrategy.build_zone_exit_journal_patch``).

Usage (from pythonclient/):

  PYTHONPATH=src ./py scripts/analyze_zone_strategy_exits.py
  PYTHONPATH=src ./py scripts/analyze_zone_strategy_exits.py --journal-db /path/to/trade_journal.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path


def _float(x) -> float | None:
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    p = argparse.ArgumentParser(description="Summarize zone strategy exits from trade journal.")
    p.add_argument(
        "--journal-db",
        type=Path,
        default=root / "trade_journal.db",
        help="Path to trade_journal.db",
    )
    args = p.parse_args()
    db = args.journal_db
    if not db.is_file():
        print(f"Database not found: {db}", flush=True)
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT symbol, exit_reason, realized_pnl, indicator_snapshot_json
        FROM trades
        WHERE status = 'closed'
          AND exit_reason IS NOT NULL
          AND (
            exit_reason LIKE '%below_demand_zone%'
            OR exit_reason LIKE '%above_supply_zone%'
          )
        ORDER BY exit_fill_time ASC
        """
    ).fetchall()
    conn.close()

    if not rows:
        print("No closed trades with zone exit reasons found.")
        return 0

    by_sym: dict[str, list[dict]] = defaultdict(list)
    metrics_num = (
        "exit_rsi",
        "exit_macd_histogram",
        "exit_adx",
        "exit_zone_age_hours",
        "exit_zone_breach_pct_of_threshold",
    )

    print(f"Found {len(rows)} zone-exit closed trade(s).\n")

    for r in rows:
        sym = (r["symbol"] or "").strip().upper()
        raw = r["indicator_snapshot_json"] or "{}"
        try:
            snap = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            snap = {}
        exitish = {k: v for k, v in snap.items() if str(k).lower().startswith("exit_")}
        rec = {
            "symbol": sym,
            "exit_reason": r["exit_reason"],
            "realized_pnl": _float(r["realized_pnl"]),
            "exit_keys_count": len(exitish),
        }
        for m in metrics_num:
            rec[m] = _float(exitish.get(m))
        by_sym[sym].append(rec)

    for sym in sorted(by_sym.keys()):
        trades = by_sym[sym]
        pnls = [t["realized_pnl"] for t in trades if t["realized_pnl"] is not None]
        total_pnl = sum(pnls) if pnls else 0.0
        print(f"=== {sym}  (n={len(trades)})  sum_realized_pnl={total_pnl:,.2f} ===")
        for m in metrics_num:
            vals = [t[m] for t in trades if t[m] is not None]
            if len(vals) >= 1:
                med = statistics.median(vals)
                print(f"  {m}: median={med:.4f}  (non-null {len(vals)}/{len(trades)})")
        missing_exit = sum(1 for t in trades if t["exit_keys_count"] < 2)
        if missing_exit:
            print(f"  note: {missing_exit} trade(s) have few exit_* keys (pre-upgrade closes).")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
