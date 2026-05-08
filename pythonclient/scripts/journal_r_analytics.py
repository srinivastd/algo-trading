#!/usr/bin/env python3
"""
Closed-trade R-multiple analytics from trade_journal.db (exit calendar days).

Uses the same R definition as post_market_reconcile._estimate_r_multiple:
realized_pnl / (risk_per_share * intended_shares), with stop from journal row.

Example:
  cd pythonclient && PYTHONPATH=src ./py scripts/journal_r_analytics.py \\
    --from-day 2026-05-04 --to-day 2026-05-08
"""
from __future__ import annotations

import argparse
import statistics
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from post_market_reconcile import _estimate_r_multiple  # noqa: E402


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def main() -> int:
    p = argparse.ArgumentParser(description="Journal R analytics by exit fill calendar day.")
    p.add_argument(
        "--journal-db",
        type=Path,
        default=_ROOT / "trade_journal.db",
        help="SQLite journal path",
    )
    p.add_argument("--from-day", required=True, help="Inclusive YYYY-MM-DD (exit_fill_time)")
    p.add_argument("--to-day", required=True, help="Inclusive YYYY-MM-DD")
    args = p.parse_args()
    db = args.journal_db.resolve()
    if not db.is_file():
        print(f"Database not found: {db}", file=sys.stderr)
        return 1

    q = """
        SELECT *
        FROM trades
        WHERE lower(trim(status)) = 'closed'
          AND realized_pnl IS NOT NULL
          AND exit_fill_time IS NOT NULL
          AND trim(exit_fill_time) != ''
          AND substr(trim(exit_fill_time), 1, 10) >= ?
          AND substr(trim(exit_fill_time), 1, 10) <= ?
        ORDER BY exit_fill_time
    """
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(_row_dict(r)) for r in con.execute(q, (args.from_day[:10], args.to_day[:10]))]
    con.close()

    rs: list[float] = []
    missing_r = 0
    for row in rows:
        r = _estimate_r_multiple(row)
        if r is None:
            missing_r += 1
        else:
            rs.append(float(r))

    n = len(rows)
    print(f"Exit-date range: {args.from_day[:10]} .. {args.to_day[:10]} (journal {db})")
    print(f"Closed trades in range: {n}")
    print(f"R computable: {len(rs)}  R unavailable (missing stop/entry/shares/pnl): {missing_r}")
    if not rs:
        return 0

    abs_rs = [abs(x) for x in rs]
    abs_rs.sort()
    wins = sum(1 for x in rs if x > 0)
    losses = sum(1 for x in rs if x < 0)
    flat = sum(1 for x in rs if x == 0)

    def pct(x: float) -> str:
        return f"{100.0 * x:.1f}%"

    print("\n--- Signed R ---")
    print(f"win / loss / flat (R!=0): {wins} / {losses} / {flat}  win-rate: {pct(wins / len(rs))}")

    def quantile(sorted_vals: list[float], qv: float) -> float:
        if not sorted_vals:
            return float("nan")
        idx = min(len(sorted_vals) - 1, max(0, int(round(qv * (len(sorted_vals) - 1)))))
        return sorted_vals[idx]

    sorted_r = sorted(rs)
    print(
        "R percentiles (signed): "
        f"p10={quantile(sorted_r, 0.10):.3f} p50={quantile(sorted_r, 0.50):.3f} "
        f"p90={quantile(sorted_r, 0.90):.3f}"
    )
    print(f"mean R: {statistics.mean(rs):.4f}  stdev: {statistics.pstdev(rs):.4f}")

    print("\n--- |R| distribution (for filtering) ---")
    bins = [(0.0, 0.5), (0.5, 1.0), (1.0, 2.0), (2.0, float("inf"))]
    labels = ["|R|<0.5", "0.5≤|R|<1", "1≤|R|<2", "|R|≥2"]
    for (lo, hi), lab in zip(bins, labels):
        c = sum(1 for a in abs_rs if lo <= a < hi or (hi == float("inf") and a >= lo))
        print(f"  {lab}: {c} ({pct(c / len(rs))})")

    print("\n--- Keep-rate vs min |R| threshold (trade filter) ---")
    for thr in (0.25, 0.5, 1.0, 1.5, 2.0, 3.0):
        kept = sum(1 for a in abs_rs if a >= thr)
        print(f"  min |R| >= {thr}: keep {kept}/{len(rs)} ({pct(kept / len(rs))})")

    print("\nTop 15 by |R| (trade_id, symbol, exit day, R, realized_pnl, exit_reason)")
    enriched = []
    for row in rows:
        r = _estimate_r_multiple(row)
        if r is None:
            continue
        ex = str(row.get("exit_fill_time") or "")[:10]
        enriched.append(
            (
                abs(float(r)),
                float(r),
                str(row.get("trade_id")),
                str(row.get("symbol")),
                ex,
                row.get("realized_pnl"),
                (row.get("exit_reason") or "")[:48],
            )
        )
    enriched.sort(key=lambda x: -x[0])
    for _, r, tid, sym, ex, pnl, er in enriched[:15]:
        print(f"  {tid}  {sym}  exit={ex}  R={r:.3f}  pnl={pnl}  {er}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
