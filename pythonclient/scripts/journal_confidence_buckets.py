#!/usr/bin/env python3
"""
Bucket **paper / watch-mode** closed trades by entry ``decision_confidence`` (and profile),
using the same confidence bands as ``GroqRecommendationsStrategy._confidence_bucket``.

Data source: ``trades.indicator_snapshot_json`` — populated on ``log_trade_attempt`` with
``decision_zone.decision_confidence`` from realtime zone entries (not backtest exports).

Example:
  cd pythonclient && PYTHONPATH=src ./py scripts/journal_confidence_buckets.py \\
    --from-day 2026-05-04 --to-day 2026-05-08
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from strategies.groq_recommendations_strategy import (  # noqa: E402
    GroqRecommendationsStrategy,
)


def _parse_snapshot(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if isinstance(raw, str) else {}
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def _entry_decision_confidence(snap: dict[str, Any]) -> Optional[float]:
    dz = snap.get("decision_zone")
    if isinstance(dz, dict):
        v = dz.get("decision_confidence")
        if v is None:
            return None
        try:
            # Avoid float drift (e.g. 0.799999…) putting trades in the wrong CONF_BUCKET band.
            return round(float(v), 4)
        except (TypeError, ValueError):
            return None
    return None


def _entry_stock_profile_score(snap: dict[str, Any]) -> Optional[float]:
    v = snap.get("stock_profile_score")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Confidence buckets from journal indicator_snapshot_json (paper/live trades)."
    )
    p.add_argument("--journal-db", type=Path, default=_ROOT / "trade_journal.db")
    p.add_argument("--from-day", required=True)
    p.add_argument("--to-day", required=True)
    p.add_argument(
        "--anchor",
        choices=("exit", "entry"),
        default="exit",
        help="Filter closed trades by exit_fill_time or entry_fill_time calendar day (default: exit).",
    )
    args = p.parse_args()
    db = args.journal_db.resolve()
    if not db.is_file():
        print(f"Database not found: {db}", file=sys.stderr)
        return 1

    day_lo = args.from_day[:10]
    day_hi = args.to_day[:10]
    col = "exit_fill_time" if args.anchor == "exit" else "entry_fill_time"

    q = f"""
        SELECT trade_id, symbol, realized_pnl, exit_reason, status,
               entry_fill_time, exit_fill_time, indicator_snapshot_json
        FROM trades
        WHERE lower(trim(status)) = 'closed'
          AND realized_pnl IS NOT NULL
          AND {col} IS NOT NULL AND trim({col}) != ''
          AND substr(trim({col}), 1, 10) >= ?
          AND substr(trim({col}), 1, 10) <= ?
    """

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(q, (day_lo, day_hi)))
    con.close()

    print(f"Journal: {db}")
    print(f"Closed trades with {col[:5]}… day in [{day_lo}, {day_hi}]: {len(rows)}\n")

    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        snap = _parse_snapshot(r["indicator_snapshot_json"])
        conf = _entry_decision_confidence(snap)
        bucket = GroqRecommendationsStrategy._confidence_bucket(conf)
        by_bucket[bucket].append({
            "trade_id": r["trade_id"],
            "symbol": r["symbol"],
            "realized_pnl": float(r["realized_pnl"] or 0),
            "exit_reason": (r["exit_reason"] or "")[:60],
            "entry_confidence": conf,
            "profile": _entry_stock_profile_score(snap),
        })

    # Sort buckets in logical order
    order = ["0.50-0.59", "0.60-0.69", "0.70-0.79", "0.80-0.89", "0.90-1.00", "unknown"]
    print("--- By entry decision_confidence (same bands as CONF_BUCKET / backtest) ---")
    for b in order:
        if b not in by_bucket:
            continue
        tr = by_bucket[b]
        pnls = [t["realized_pnl"] for t in tr]
        wins = sum(1 for x in pnls if x > 0)
        n = len(tr)
        wr = 100.0 * wins / n if n else 0.0
        print(
            f"  {b}: n={n}  win%={wr:.1f}  total_pnl={sum(pnls):.2f}  avg={sum(pnls)/n:.2f}"
        )

    for b in sorted(by_bucket.keys()):
        if b in order:
            continue
        tr = by_bucket[b]
        pnls = [t["realized_pnl"] for t in tr]
        wins = sum(1 for x in pnls if x > 0)
        n = len(tr)
        wr = 100.0 * wins / n if n else 0.0
        print(
            f"  {b}: n={n}  win%={wr:.1f}  total_pnl={sum(pnls):.2f}  avg={sum(pnls)/n:.2f}"
        )

    # Rank buckets by total_pnl (paper outcome)
    ranked = sorted(
        ((b, sum(t["realized_pnl"] for t in by_bucket[b])) for b in by_bucket),
        key=lambda x: x[1],
        reverse=True,
    )
    if ranked:
        print("\nBuckets ranked by total realized PnL (this window, paper):")
        for b, tp in ranked:
            print(f"  {b}: {tp:.2f}")

    print("\nPer-trade (for spot-check):")
    for r in rows:
        snap = _parse_snapshot(r["indicator_snapshot_json"])
        conf = _entry_decision_confidence(snap)
        b = GroqRecommendationsStrategy._confidence_bucket(conf)
        prof = _entry_stock_profile_score(snap)
        print(
            f"  {r['symbol']}\t{b}\tconf={conf}\tprofile={prof}\tpnl={float(r['realized_pnl'] or 0):.2f}\t{r['trade_id'][:40]}…"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
