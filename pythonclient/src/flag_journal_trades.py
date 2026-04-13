#!/usr/bin/env python3
"""
Flag closed trades in trade_journal.db for post-mortem review.

Uses indicator_snapshot_json + side + realized_pnl. Heuristic "weak entry" for
SHORT losers matches common groq realtime mistakes (low ADX chop, neutral RSI,
bullish DI). Adjust thresholds to match your own review process.

  cd pythonclient && PYTHONPATH=src python3 src/flag_journal_trades.py \\
    --journal-db trade_journal.db --day 2026-04-10 --dry-run

  # Apply labels (wins -> GOOD_WIN, losses -> LOSS_REVIEW / WEAK_ENTRY_LOSS)
  PYTHONPATH=src python3 src/flag_journal_trades.py --journal-db trade_journal.db --day 2026-04-10

  # Only losers (skip profitable / flat closes)
  PYTHONPATH=src python3 src/flag_journal_trades.py --journal-db trade_journal.db --day 2026-04-10 --loss-only
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PYTHONCLIENT_ROOT = _SCRIPT_DIR.parent


def _f(x: Any) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v:  # NaN
            return None
        return v
    except (TypeError, ValueError):
        return None


def classify_row(
    *,
    side: str,
    pnl: float | None,
    ind: dict[str, Any],
    short_weak_adx: float,
    short_weak_rsi: float,
    long_weak_adx: float,
    long_weak_rsi: float,
) -> tuple[str | None, int, str]:
    """
    Returns (classification, score, rationale).
    score: -2 bad weak entry, -1 bad other, 0 neutral, 1 good win
    """
    side_u = (side or "").upper()
    if pnl is None:
        return None, 0, ""

    snap = ", ".join(
        f"{k}={ind.get(k)}"
        for k in ("adx", "rsi", "plus_di", "minus_di", "macd_histogram")
        if k in ind
    )

    if pnl > 0:
        return "GOOD_WIN", 1, f"pnl={pnl:.2f}; {snap}"
    if pnl == 0:
        return "BREAKEVEN", 0, f"pnl=0.00; {snap}"

    # Losers
    adx = _f(ind.get("adx"))
    rsi = _f(ind.get("rsi"))
    pdi = _f(ind.get("plus_di"))
    mdi = _f(ind.get("minus_di"))

    if side_u == "SHORT":
        weak = False
        reasons: list[str] = []
        # Chop + not extended: matches typical bad shorts (e.g. low ADX, mid RSI).
        if (
            adx is not None
            and rsi is not None
            and adx < short_weak_adx
            and rsi < short_weak_rsi
        ):
            weak = True
            reasons.append(f"adx={adx:.2f}<{short_weak_adx} & rsi={rsi:.2f}<{short_weak_rsi}")
        # Mild bullish pressure without overbought RSI (DI alone is not enough — high RSI shorts stay LOSS_REVIEW).
        elif (
            pdi is not None
            and mdi is not None
            and rsi is not None
            and adx is not None
            and pdi >= mdi
            and rsi < 52
            and adx < min(short_weak_adx + 5.0, 28.0)
        ):
            weak = True
            reasons.append(f"plus_di>=minus_di & rsi={rsi:.2f}<52 & adx={adx:.2f} (chop bias)")
        if weak:
            return "WEAK_ENTRY_LOSS", -2, f"pnl={pnl:.2f}; " + "; ".join(reasons) + f"; {snap}"
        return "LOSS_REVIEW", -1, f"pnl={pnl:.2f}; {snap}"

    if side_u == "LONG":
        weak = False
        reasons = []
        if adx is not None and adx < long_weak_adx:
            weak = True
            reasons.append(f"adx={adx:.2f}<{long_weak_adx}")
        if rsi is not None and rsi > long_weak_rsi:
            weak = True
            reasons.append(f"rsi={rsi:.2f}>{long_weak_rsi}")
        if pdi is not None and mdi is not None and mdi >= pdi:
            weak = True
            reasons.append("minus_di>=plus_di")
        if weak:
            return "WEAK_ENTRY_LOSS", -2, f"pnl={pnl:.2f}; " + "; ".join(reasons) + f"; {snap}"
        return "LOSS_REVIEW", -1, f"pnl={pnl:.2f}; {snap}"

    return "LOSS_REVIEW", -1, f"pnl={pnl:.2f}; side={side_u}; {snap}"


def main() -> None:
    p = argparse.ArgumentParser(description="Flag closed trades in SQLite journal")
    p.add_argument("--journal-db", type=Path, default=_PYTHONCLIENT_ROOT / "trade_journal.db")
    p.add_argument("--day", required=True, help="ET calendar day YYYY-MM-DD (matches exit_fill_time prefix)")
    p.add_argument("--dry-run", action="store_true", help="Print actions only")
    p.add_argument(
        "--short-weak-adx-below",
        type=float,
        default=20.0,
        help="SHORT loss flagged WEAK if ADX below this (chop)",
    )
    p.add_argument(
        "--short-weak-rsi-below",
        type=float,
        default=55.0,
        help="SHORT loss flagged WEAK if RSI below this (not overbought)",
    )
    p.add_argument("--long-weak-adx-below", type=float, default=20.0)
    p.add_argument("--long-weak-rsi-above", type=float, default=45.0)
    p.add_argument(
        "--loss-only",
        action="store_true",
        help="Only label losing closes (skip GOOD_WIN / BREAKEVEN)",
    )
    args = p.parse_args()

    db = args.journal_db.resolve()
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT trade_id, symbol, side, status, realized_pnl, indicator_snapshot_json, exit_fill_time
        FROM trades
        WHERE status = 'closed' AND substr(exit_fill_time, 1, 10) = ?
        """,
        (args.day,),
    ).fetchall()

    updated = 0
    to_flag = 0
    for r in rows:
        raw = r["indicator_snapshot_json"] or "{}"
        try:
            ind = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            ind = {}
        if not isinstance(ind, dict):
            ind = {}

        pnl = _f(r["realized_pnl"])
        cls, score, rationale = classify_row(
            side=str(r["side"] or ""),
            pnl=pnl,
            ind=ind,
            short_weak_adx=args.short_weak_adx_below,
            short_weak_rsi=args.short_weak_rsi_below,
            long_weak_adx=args.long_weak_adx_below,
            long_weak_rsi=args.long_weak_rsi_above,
        )
        if cls is None:
            continue
        if args.loss_only and pnl is not None and pnl >= 0:
            continue
        to_flag += 1
        rationale = (rationale or "")[:4000]
        print(f"{r['trade_id']}\t{cls}\t{score}\t{rationale[:120]}...")
        if not args.dry_run:
            con.execute(
                """
                UPDATE trades
                SET classification = ?, classification_score = ?, classification_rationale = ?, updated_at = datetime('now')
                WHERE trade_id = ?
                """,
                (cls, score, rationale, r["trade_id"]),
            )
            updated += 1

    if not args.dry_run:
        con.commit()
    con.close()
    print(
        f"{'Would update' if args.dry_run else 'Updated'} {to_flag if args.dry_run else updated} "
        f"row(s) on {args.day} (scanned {len(rows)} closed)"
    )


if __name__ == "__main__":
    main()
