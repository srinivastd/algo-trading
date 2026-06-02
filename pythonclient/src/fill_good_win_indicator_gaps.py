#!/usr/bin/env python3
"""
Merge ADX / RSI / +DI / −DI into journal rows that are missing them, using:

1. market_data JSONL (per-line JSON): last ``market_context.indicators`` per symbol,
   then ``trade_execution_attempt`` / ``position_bootstrap`` lines that carry ``trade_id``.
2. SQLite ``signals`` rows for the same ``run_id`` + symbol at or before entry time.

Typical use after ``flag_journal_trades.py`` labels profitable closes as GOOD_WIN:

  cd pythonclient && PYTHONPATH=src python3 src/fill_good_win_indicator_gaps.py \\
    --journal-db trade_journal.db \\
    --log market_data_logs/market_data_0410.json \\
    --dry-run

  # Include all profitable closes (classification not set yet)
  PYTHONPATH=src python3 src/fill_good_win_indicator_gaps.py --journal-db trade_journal.db \\
    --log market_data_logs/market_data_0410.json --any-profitable

  # Only closes on a calendar day (matches exit_fill_time date prefix)
  PYTHONPATH=src python3 src/fill_good_win_indicator_gaps.py --journal-db trade_journal.db \\
    --log market_data_logs/market_data_0410.json --day 2026-04-10
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from trade_journal import TradeJournal, _finite_number, _sanitize_json

_SCRIPT_DIR = Path(__file__).resolve().parent
_PYTHONCLIENT_ROOT = _SCRIPT_DIR.parent

_FILL_KEYS = ("adx", "rsi", "plus_di", "minus_di")


def _snapshot_incomplete(raw: str | None) -> bool:
    try:
        ind = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return True
    if not isinstance(ind, dict):
        return True
    return any(not _finite_number(ind.get(k)) for k in _FILL_KEYS)


def _ingest_log_line(
    obj: dict[str, Any],
    *,
    last_indicators_by_symbol: dict[str, dict[str, Any]],
    indicators_by_trade_id: dict[str, dict[str, Any]],
) -> None:
    sym = str(obj.get("symbol") or "").strip().upper()
    mc = obj.get("market_context")
    if sym and isinstance(mc, dict):
        ind = mc.get("indicators")
        if isinstance(ind, dict) and ind:
            last_indicators_by_symbol[sym] = ind

    tid = obj.get("trade_id")
    action = obj.get("action")
    if not tid or not sym:
        return
    if action not in ("trade_execution_attempt", "position_bootstrap"):
        return
    snap = last_indicators_by_symbol.get(sym)
    if not snap:
        return
    indicators_by_trade_id[str(tid)] = dict(snap)


def _scan_log_files(paths: list[Path]) -> dict[str, dict[str, Any]]:
    last: dict[str, dict[str, Any]] = {}
    by_trade: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] != "{":
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                _ingest_log_line(obj, last_indicators_by_symbol=last, indicators_by_trade_id=by_trade)
    return by_trade


def _signals_patch(con: sqlite3.Connection, *, run_id: str, symbol: str, before_iso: str) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT indicators_json FROM signals
        WHERE run_id = ? AND symbol = ? AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (run_id, symbol.upper(), before_iso),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        ind = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    if isinstance(ind, dict):
        return ind
    return None


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill ADX/RSI/DI into winning journal snapshots")
    p.add_argument("--journal-db", type=Path, default=_PYTHONCLIENT_ROOT / "trade_journal.db")
    p.add_argument(
        "--log",
        type=Path,
        nargs="*",
        default=[],
        help="market_data JSONL file(s); default: all *.json under pythonclient/market_data_logs",
    )
    p.add_argument(
        "--classification",
        default="GOOD_WIN",
        help="Only rows with this classification (use empty string with --any-profitable)",
    )
    p.add_argument(
        "--any-profitable",
        action="store_true",
        help="All closed trades with realized_pnl > 0 (ignore --classification)",
    )
    p.add_argument(
        "--day",
        metavar="YYYY-MM-DD",
        help="Only trades whose exit_fill_time starts with this calendar day (ET-style ISO in DB)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    db_path = args.journal_db.resolve()
    log_paths = [Path(x).resolve() for x in args.log]
    if not log_paths:
        default_dir = _PYTHONCLIENT_ROOT / "market_data_logs"
        if default_dir.is_dir():
            log_paths = sorted(default_dir.glob("*.json"))

    by_trade = _scan_log_files(log_paths) if log_paths else {}

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        q = """
            SELECT trade_id, symbol, run_id, entry_fill_time, created_at, indicator_snapshot_json, classification
            FROM trades
            WHERE status = 'closed' AND realized_pnl > 0
        """
        params: list[Any] = []
        if not args.any_profitable and args.classification:
            q += " AND classification = ?"
            params.append(args.classification)
        if args.day:
            q += " AND substr(exit_fill_time, 1, 10) = ?"
            params.append(args.day)
        rows = con.execute(q, params).fetchall()
    finally:
        con.close()

    journal = TradeJournal(db_path)
    updated = 0
    skipped_complete = 0
    no_patch = 0

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        for r in rows:
            tid = str(r["trade_id"])
            raw_snap = r["indicator_snapshot_json"]
            if not _snapshot_incomplete(raw_snap):
                skipped_complete += 1
                continue

            sym = str(r["symbol"] or "").upper()
            run_id = str(r["run_id"] or "")
            ref_t = r["entry_fill_time"] or r["created_at"] or ""

            patch: dict[str, Any] = {}
            log_patch = by_trade.get(tid)
            if isinstance(log_patch, dict) and log_patch:
                patch.update(_sanitize_json(log_patch))

            if any(k not in patch or patch.get(k) is None for k in _FILL_KEYS) and run_id and ref_t:
                sig = _signals_patch(con, run_id=run_id, symbol=sym, before_iso=str(ref_t))
                if isinstance(sig, dict):
                    for k, v in _sanitize_json(sig).items():
                        if k not in patch or patch.get(k) is None:
                            patch[k] = v

            if not patch:
                no_patch += 1
                print(f"{tid}\tno_indicator_source")
                continue

            if args.dry_run:
                print(f"{tid}\twould_merge\tfrom_log={tid in by_trade}")
                updated += 1
                continue

            if journal.merge_indicator_snapshot(trade_id=tid, patch=patch, fill_keys=_FILL_KEYS):
                updated += 1
                print(f"{tid}\tmerged")
            else:
                print(f"{tid}\tno_change_after_merge")
    finally:
        con.close()

    print(
        f"Done: merged {updated}, skipped_already_complete {skipped_complete}, "
        f"no_indicator_source {no_patch}, scanned_trades {len(rows)}, log_files {len(log_paths)}"
    )


if __name__ == "__main__":
    main()
