#!/usr/bin/env python3
"""
FIFO realized PnL from ``trade_journal.ib_executions`` — independent of ``trades``
attribution — for cross-checking journal rows.

Walks executions in **chronological order** per symbol (full history replay for each
symbol that appears in your date filter, so overnight inventory is correct).

Each matched lot attributes realized gross PnL to the **closing** execution timestamp.
The report sums those matches whose close date falls in ``--from-day`` ..
``--to-day``, and compares that window to aggregated ``trades.realized_pnl``.

Example:
  cd pythonclient && PYTHONPATH=src ./py scripts/reconcile_executions.py \\
    --from-day 2026-05-04 --to-day 2026-05-08

  ./py scripts/reconcile_executions.py --journal-db ./trade_journal.db \\
    --from-day 2026-05-07 --to-day 2026-05-07 --compare-trades
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import deque
from pathlib import Path
from typing import Deque, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]


def _norm_day(ts: Optional[str]) -> str:
    if not ts:
        return ""
    s = str(ts).strip()
    return s[:10] if len(s) >= 10 else s


def _normalize_buy(side: Optional[str]) -> Optional[bool]:
    if side is None:
        return None
    u = str(side).strip().upper()
    if u in ("BOT", "BUY"):
        return True
    if u in ("SLD", "SELL"):
        return False
    return None


def _fifo_symbol_full(
    fills: list[sqlite3.Row],
) -> tuple[List[Tuple[str, float]], float, float, float]:
    """
    Full-history FIFO for one symbol.

    Returns:
        chunks: (close_time_iso, gross_pnl) for each matched lot (closing leg = current row)
        sum_commissions: sum of commission column over all rows processed
        ending_long_qty, ending_short_qty
    """
    long_q: Deque[Tuple[float, float]] = deque()
    short_q: Deque[Tuple[float, float]] = deque()
    chunks: List[Tuple[str, float]] = []
    comm_sum = 0.0

    for r in fills:
        side = _normalize_buy(r["side"])
        if side is None:
            continue
        qty = float(r["shares"] or 0.0)
        px = float(r["price"] or 0.0)
        cm = float(r["commission"] or 0.0)
        comm_sum += cm
        if qty <= 0:
            continue

        t_close = str(r["time"] or "").strip()
        remaining = qty
        if side:
            while remaining > 1e-12 and short_q:
                sq, sp = short_q[0]
                matched = min(remaining, sq)
                chunks.append((t_close, matched * (sp - px)))
                new_sq = sq - matched
                if new_sq <= 1e-12:
                    short_q.popleft()
                else:
                    short_q[0] = (new_sq, sp)
                remaining -= matched
            if remaining > 1e-12:
                long_q.append((remaining, px))
        else:
            while remaining > 1e-12 and long_q:
                lq, lp = long_q[0]
                matched = min(remaining, lq)
                chunks.append((t_close, matched * (px - lp)))
                new_lq = lq - matched
                if new_lq <= 1e-12:
                    long_q.popleft()
                else:
                    long_q[0] = (new_lq, lp)
                remaining -= matched
            if remaining > 1e-12:
                short_q.append((remaining, px))

    end_long = sum(x[0] for x in long_q)
    end_short = sum(x[0] for x in short_q)
    return chunks, comm_sum, end_long, end_short


def _sum_chunks_in_range(
    chunks: List[Tuple[str, float]], d0: str, d1: str
) -> float:
    s = 0.0
    for ts, amt in chunks:
        day = _norm_day(ts)
        if day and d0 <= day <= d1:
            s += amt
    return s


def main() -> int:
    p = argparse.ArgumentParser(
        description="FIFO PnL from journal ib_executions (attribution-independent)."
    )
    p.add_argument(
        "--journal-db",
        type=Path,
        default=_ROOT / "trade_journal.db",
        help="SQLite journal path",
    )
    p.add_argument(
        "--from-day",
        required=True,
        help="Inclusive YYYY-MM-DD — filter realized chunks by **closing** execution date",
    )
    p.add_argument("--to-day", required=True, help="Inclusive YYYY-MM-DD")
    p.add_argument(
        "--symbol",
        default=None,
        help="Restrict to one ticker (otherwise all symbols in range)",
    )
    p.add_argument(
        "--compare-trades",
        action="store_true",
        help="Also sum trades.realized_pnl for closed rows with exit in the same window",
    )
    args = p.parse_args()
    db = args.journal_db.resolve()
    if not db.is_file():
        print(f"Database not found: {db}", file=sys.stderr)
        return 1

    d0 = args.from_day[:10]
    d1 = args.to_day[:10]
    sym_filter = args.symbol.strip().upper() if args.symbol else None

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    sym_q = " AND upper(trim(symbol)) = ? " if sym_filter else ""
    params: tuple = (d0, d1)
    if sym_filter:
        params = (d0, d1, sym_filter)

    touched = con.execute(
        f"""
        SELECT DISTINCT upper(trim(symbol)) AS s
        FROM ib_executions
        WHERE substr(trim(time), 1, 10) >= ?
          AND substr(trim(time), 1, 10) <= ?
          AND length(trim(symbol)) > 0
          {sym_q}
        ORDER BY s
        """,
        params,
    ).fetchall()
    symbols = [str(r[0]) for r in touched if r[0]]

    print(f"Journal {db}")
    print(f"Close-date filter: {d0} .. {d1} (FIFO matches attributed to closing leg)")
    print(f"Symbols with executions in window: {len(symbols)}")
    if not symbols:
        con.close()
        return 0

    comm_window_params_full: tuple = (d0, d1)
    comm_cf = ""
    if sym_filter:
        comm_cf = " AND upper(trim(symbol)) = ? "
        comm_window_params_full = (d0, d1, sym_filter)
    total_comm_global = float(
        con.execute(
            f"""
            SELECT coalesce(sum(commission), 0)
            FROM ib_executions
            WHERE substr(trim(time), 1, 10) >= ?
              AND substr(trim(time), 1, 10) <= ?
              {comm_cf}
            """,
            comm_window_params_full,
        ).fetchone()[0]
        or 0.0
    )

    comm_by_sym: dict[str, float] = {}
    for r in con.execute(
        f"""
        SELECT upper(trim(symbol)) AS s, coalesce(sum(commission), 0)
        FROM ib_executions
        WHERE substr(trim(time), 1, 10) >= ?
          AND substr(trim(time), 1, 10) <= ?
          {comm_cf}
        GROUP BY upper(trim(symbol))
        """,
        comm_window_params_full,
    ).fetchall():
        if r[0]:
            comm_by_sym[str(r[0])] = float(r[1] or 0.0)

    total_realized = 0.0
    for sym in symbols:
        all_rows = con.execute(
            """
            SELECT exec_id, symbol, side, shares, price, time, commission, trade_id
            FROM ib_executions
            WHERE upper(trim(symbol)) = ?
            ORDER BY time, exec_id
            """,
            (sym,),
        ).fetchall()
        chunks, _life_comm, elo, esh = _fifo_symbol_full(all_rows)
        gw = _sum_chunks_in_range(chunks, d0, d1)
        cw = comm_by_sym.get(sym, 0.0)
        total_realized += gw
        print(
            f"  {sym:6s}  fifo_realized≈{gw:,.2f}  comm_window={cw:,.2f}  "
            f"net≈{gw - cw:,.2f}  replay_rows={len(all_rows)}  open_qty=(L={elo:.4g},S={esh:.4g})"
        )

    net_all = total_realized - total_comm_global
    print(
        f"TOTAL     fifo_realized≈{total_realized:,.2f}  "
        f"comm_window={total_comm_global:,.2f}  net≈{net_all:,.2f}"
    )

    if args.compare_trades:
        tparams: tuple = (d0, d1)
        tq_extra = ""
        if sym_filter:
            tq_extra = " AND upper(trim(symbol)) = ? "
            tparams = (d0, d1, sym_filter)
        journal_sum = float(
            con.execute(
                f"""
                SELECT coalesce(sum(realized_pnl), 0)
                FROM trades
                WHERE lower(trim(status)) = 'closed'
                  AND realized_pnl IS NOT NULL
                  AND exit_fill_time IS NOT NULL
                  AND trim(exit_fill_time) != ''
                  AND substr(trim(exit_fill_time), 1, 10) >= ?
                  AND substr(trim(exit_fill_time), 1, 10) <= ?
                  {tq_extra}
                """,
                tparams,
            ).fetchone()[0]
            or 0.0,
        )
        print(
            f"COMPARE closed trades.realized_pnl (exit date same window)= {journal_sum:,.2f}; "
            f"delta vs fifo net≈ {journal_sum - net_all:,.2f}"
        )

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
