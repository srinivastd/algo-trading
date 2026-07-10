#!/usr/bin/env python3
"""
Backfill closed paper trades from the trade journal into Chroma trade memory.

The watch loop auto-captures closed trades into the ``trade_decisions`` collection,
but runs launched with ``--no-memory`` skip capture entirely. This script replays
closed trades from ``trade_journal.db`` so they become available to
``TradeMemory.pre_trade_recall`` for future entry/exit context.

Notes on building useful memory:
  - By default this captures **all** closed trades (winners and losers). Recall's
    ``win_rate_similar`` signal is only meaningful with both — a winners-only memory
    biases every recall to ~100% win rate and is misleading.
  - ``--min-abs-pnl`` / ``--standout-pnl`` let you flag or restrict to standout
    trades; the captured records carry a ``standout`` metadata flag either way.
  - Re-running is safe: records already present (matched by ``trade_id``) are
    skipped unless ``--force`` is passed.

Usage (from pythonclient/):
  PYTHONPATH=src ./py src/backfill_trade_memory.py --since 2026-06-22
  PYTHONPATH=src ./py src/backfill_trade_memory.py --since 2026-06-22 --min-abs-pnl 200
  PYTHONPATH=src ./py src/backfill_trade_memory.py --symbols QBTS --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trade_journal import TradeJournal
from trade_memory import TradeMemory

DEFAULT_DB = Path(__file__).resolve().parent.parent / "trade_journal.db"
# Must match main.py's --memory-db default (pythonclient/chroma_db) so backfilled
# records land in the same collection the live watch loop queries via pre_trade_recall.
DEFAULT_CHROMA = Path(__file__).resolve().parent.parent / "chroma_db"
# The backtest-results Chroma store (src/chroma_db). Trade memory must NEVER be
# written here — guarded against so a stray --memory-db can't pollute backtests.
BACKTEST_STORE = Path(__file__).resolve().parent / "chroma_db"


def _guard_memory_path(memory_db: Path) -> Path:
    """Refuse to write trade memory into the backtest-results store."""
    resolved = memory_db.resolve()
    if resolved == BACKTEST_STORE.resolve():
        raise SystemExit(
            f"Refusing to write trade memory to the backtest-results store: {resolved}\n"
            f"Use the live trade-memory DB instead (default: {DEFAULT_CHROMA})."
        )
    return resolved

# Matches the live capture path, which uses type(strategy).__name__ so recall query
# text aligns. Override with --strategy-name if backfilling a different strategy.
DEFAULT_STRATEGY_NAME = "GroqRecommendationsStrategy"


def _parse_snapshot(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _entry_indicators(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Drop exit_* keys so recall embeds entry-time context only."""
    return {k: v for k, v in snapshot.items() if not str(k).lower().startswith("exit_")}


def fetch_closed_trades(
    journal: TradeJournal,
    *,
    since: str | None,
    until: str | None,
    symbols: set[str] | None,
    min_abs_pnl: float,
) -> list[dict[str, Any]]:
    """Closed trades (with realized exit) ordered by exit time, applying filters."""
    clauses = [
        "LOWER(TRIM(t.status)) = 'closed'",
        "t.exit_fill_time IS NOT NULL",
        "length(trim(t.exit_fill_time)) >= 10",
    ]
    params: list[Any] = []
    if since:
        clauses.append("substr(trim(t.exit_fill_time), 1, 10) >= ?")
        params.append(since[:10])
    if until:
        clauses.append("substr(trim(t.exit_fill_time), 1, 10) <= ?")
        params.append(until[:10])
    where = " AND ".join(clauses)

    with journal._conn() as con:
        rows = con.execute(
            f"""
            SELECT
                t.trade_id, t.run_id, t.symbol, t.side, t.signal_value,
                t.entry_fill_price, t.exit_fill_price, t.realized_pnl,
                t.intended_shares, t.entry_fill_time, t.exit_fill_time, t.exit_reason,
                t.indicator_snapshot_json,
                (
                    SELECT COALESCE(SUM(f.qty), 0) FROM fills f
                    WHERE f.trade_id = t.trade_id AND f.is_entry = 1
                ) AS entry_qty_sum
            FROM trades t
            WHERE {where}
            ORDER BY t.exit_fill_time ASC
            """,
            params,
        ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        rec = dict(r)
        sym = (rec.get("symbol") or "").strip().upper()
        if symbols and sym not in symbols:
            continue
        pnl = rec.get("realized_pnl")
        if min_abs_pnl > 0 and (pnl is None or abs(float(pnl)) < min_abs_pnl):
            continue
        rec["symbol"] = sym
        out.append(rec)
    return out


def backfill(args: argparse.Namespace) -> int:
    memory_path = _guard_memory_path(Path(args.memory_db))
    journal = TradeJournal(Path(args.journal_db))
    memory = TradeMemory(memory_path)
    if not memory.enabled:
        print(f"Trade memory unavailable (init failed): {memory.init_error}", file=sys.stderr)
        return 2

    symbols = {s.strip().upper() for s in args.symbols} if args.symbols else None
    trades = fetch_closed_trades(
        journal,
        since=args.since,
        until=args.until,
        symbols=symbols,
        min_abs_pnl=float(args.min_abs_pnl),
    )

    if not trades:
        print("No closed trades matched the given filters.")
        return 0

    captured = skipped_dup = skipped_filter = 0
    for rec in trades:
        tid = rec["trade_id"]
        pnl = rec.get("realized_pnl")
        pnl_f = float(pnl) if pnl is not None else None

        if not args.force and memory.has_trade(tid):
            skipped_dup += 1
            if args.verbose:
                print(f"  skip (already in memory): {tid} {rec['symbol']}")
            continue

        snapshot = _entry_indicators(_parse_snapshot(rec.get("indicator_snapshot_json")))
        shares = rec.get("entry_qty_sum") or rec.get("intended_shares")
        is_standout = (
            pnl_f is not None
            and args.standout_pnl > 0
            and abs(pnl_f) >= float(args.standout_pnl)
        )
        extra = {
            "source": "backfill",
            "run_id": rec.get("run_id") or "",
            "standout": bool(is_standout),
            "backfilled_at": datetime.utcnow().isoformat(),
        }

        side = (rec.get("side") or "").strip().upper() or (
            "LONG" if int(rec.get("signal_value") or 0) >= 0 else "SHORT"
        )

        win = "WIN" if (pnl_f or 0) > 0 else ("LOSS" if (pnl_f or 0) < 0 else "FLAT")
        star = " *standout*" if is_standout else ""
        print(
            f"  capture {rec['symbol']:<6} {side:<5} {win:<4} "
            f"pnl={pnl_f if pnl_f is not None else 'NA':>10} "
            f"entry={rec.get('entry_fill_price')} exit={rec.get('exit_fill_price')} "
            f"reason={rec.get('exit_reason')}{star}  [{tid}]"
        )

        if args.dry_run:
            captured += 1
            continue

        memory.capture_closed_trade(
            trade_id=tid,
            symbol=rec["symbol"],
            strategy=args.strategy_name,
            side=side,
            signal_value=int(rec.get("signal_value") or 0),
            entry_price=rec.get("entry_fill_price"),
            exit_price=rec.get("exit_fill_price"),
            realized_pnl=pnl_f,
            shares=shares,
            entry_time=rec.get("entry_fill_time"),
            exit_time=rec.get("exit_fill_time"),
            entry_regime=snapshot.get("entry_regime"),
            exit_reason=rec.get("exit_reason"),
            indicators=snapshot,
            extra_metadata=extra,
        )
        captured += 1

    action = "Would capture" if args.dry_run else "Captured"
    print(
        f"\n{action} {captured} trade(s); skipped {skipped_dup} already in memory."
        + (f" (min_abs_pnl={args.min_abs_pnl})" if args.min_abs_pnl else "")
    )
    if args.dry_run:
        print("Dry run — no records written. Re-run without --dry-run to persist.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill closed journal trades into Chroma trade memory")
    p.add_argument("--journal-db", default=str(DEFAULT_DB))
    p.add_argument("--memory-db", default=str(DEFAULT_CHROMA), help="Path to ChromaDB for trade memory")
    p.add_argument("--since", help="Only trades closed on/after this date (YYYY-MM-DD, by exit_fill_time)")
    p.add_argument("--until", help="Only trades closed on/before this date (YYYY-MM-DD)")
    p.add_argument("--days", type=int, help="Convenience: set --since to N days ago (UTC)")
    p.add_argument("--symbols", nargs="*", help="Restrict to these tickers (e.g. QBTS AMD)")
    p.add_argument(
        "--min-abs-pnl",
        type=float,
        default=0.0,
        help="Only capture trades with |realized_pnl| >= this (default 0 = all; recommended to keep winners+losers)",
    )
    p.add_argument(
        "--standout-pnl",
        type=float,
        default=200.0,
        help="Tag trades with |realized_pnl| >= this as standout=True in memory metadata",
    )
    p.add_argument("--strategy-name", default=DEFAULT_STRATEGY_NAME, help="Strategy name embedded in memory text/query")
    p.add_argument("--force", action="store_true", help="Capture even if a record for the trade_id already exists")
    p.add_argument("--dry-run", action="store_true", help="Show what would be captured without writing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.days and not args.since:
        args.since = (datetime.now(timezone.utc) - timedelta(days=args.days)).date().isoformat()

    sys.exit(backfill(args))


if __name__ == "__main__":
    main()
