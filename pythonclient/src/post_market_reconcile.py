#!/usr/bin/env python3
"""
Phase 3: Post-market reconciliation using IBKR APIs + trade journal.

This script validates journaled trades against broker executions and emits a
daily Markdown report for review.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytz

from ibkr.connection import IBKRConnection

ET = pytz.timezone("US/Eastern")
_SCRIPT_DIR = Path(__file__).resolve().parent
_PYTHONCLIENT_ROOT = _SCRIPT_DIR.parent


def _to_et_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Journal timestamps are currently written as UTC-naive.
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(ET)


def _et_date(value: Any) -> date | None:
    dt = _to_et_datetime(value)
    return dt.date() if dt else None


def _fmt_money(value: float | None) -> str:
    if value is None:
        return ""
    return f"${value:,.2f}"


def _classify_trade(row: dict[str, Any]) -> str:
    pnl = row.get("realized_pnl")
    slippage = row.get("slippage_bps")
    reason = str(row.get("exit_reason") or "").lower()
    if pnl is None:
        return "NEUTRAL"
    if pnl > 0 and reason in {"target", "strategy_signal", "signal"} and (slippage is None or slippage < 20):
        return "GOOD"
    if pnl < 0 and (reason == "time" or (slippage is not None and slippage > 50)):
        return "BAD"
    if pnl < 0:
        return "BAD"
    return "NEUTRAL"


@dataclass
class ReconcileResult:
    target_date: str
    journal_trades: list[dict[str, Any]]
    ib_exec_rows: list[dict[str, Any]]
    unmatched_ib_order_ids: list[int]
    matched_trades: int
    closed_trades: int
    closed_pnl: float
    output_path: Path


def _load_journal_trades(conn: sqlite3.Connection, target: date) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT
            trade_id, run_id, symbol, side, status, order_id, stop_order_id, target_order_id,
            intended_entry_price, intended_shares, entry_fill_price, entry_fill_time,
            exit_fill_price, exit_fill_time, exit_reason, realized_pnl, slippage_bps, created_at, updated_at
        FROM trades
        ORDER BY created_at ASC
        """
    )
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        d_candidates = [
            _et_date(row.get("entry_fill_time")),
            _et_date(row.get("exit_fill_time")),
            _et_date(row.get("created_at")),
        ]
        if target not in [d for d in d_candidates if d is not None]:
            continue
        rows.append(row)
    return rows


async def _load_ib_executions(connection: IBKRConnection, target: date) -> list[dict[str, Any]]:
    ib = connection.get_ib()
    execs = await ib.reqExecutionsAsync()
    rows: list[dict[str, Any]] = []
    for ex in execs:
        et_dt = _to_et_datetime(ex.time)
        if et_dt is None or et_dt.date() != target:
            continue
        rows.append(
            {
                "order_id": int(ex.orderId),
                "exec_id": str(ex.execId),
                "symbol": str(ex.symbol).upper(),
                "side": str(ex.side),
                "shares": float(ex.shares),
                "price": float(ex.price),
                "time_et": et_dt.isoformat(),
            }
        )
    rows.sort(key=lambda x: x["time_et"])
    return rows


async def reconcile_post_market(
    *,
    target: date,
    journal_db: Path,
    output_path: Path,
    host: str,
    port: int,
    client_id: int,
) -> ReconcileResult:
    con = sqlite3.connect(journal_db)
    con.row_factory = sqlite3.Row
    try:
        journal_rows = _load_journal_trades(con, target)
        by_order = {int(r["order_id"]): r for r in journal_rows if r.get("order_id") is not None}

        connection = IBKRConnection(host=host, port=port, client_id=client_id)
        if not await connection.connect():
            raise RuntimeError("Failed to connect to IBKR for reconciliation")
        try:
            ib_exec_rows = await _load_ib_executions(connection, target)
        finally:
            await connection.disconnect()

        ib_order_ids = {r["order_id"] for r in ib_exec_rows}
        matched = sum(1 for r in journal_rows if r.get("order_id") in ib_order_ids)
        unmatched_ib = sorted(list(ib_order_ids - set(by_order.keys())))

        closed_trades = 0
        closed_pnl = 0.0
        for row in journal_rows:
            cls = _classify_trade(row)
            con.execute("UPDATE trades SET classification = ? WHERE trade_id = ?", (cls, row["trade_id"]))
            row["classification"] = cls
            if str(row.get("status", "")).lower() == "closed" and row.get("realized_pnl") is not None:
                closed_trades += 1
                closed_pnl += float(row["realized_pnl"])

        con.commit()
        md = _build_markdown(
            target=target,
            journal_rows=journal_rows,
            ib_exec_rows=ib_exec_rows,
            unmatched_ib=unmatched_ib,
            matched=matched,
            closed_trades=closed_trades,
            closed_pnl=closed_pnl,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(md, encoding="utf-8")

        return ReconcileResult(
            target_date=target.isoformat(),
            journal_trades=journal_rows,
            ib_exec_rows=ib_exec_rows,
            unmatched_ib_order_ids=unmatched_ib,
            matched_trades=matched,
            closed_trades=closed_trades,
            closed_pnl=closed_pnl,
            output_path=output_path,
        )
    finally:
        con.close()


def _build_markdown(
    *,
    target: date,
    journal_rows: list[dict[str, Any]],
    ib_exec_rows: list[dict[str, Any]],
    unmatched_ib: list[int],
    matched: int,
    closed_trades: int,
    closed_pnl: float,
) -> str:
    lines: list[str] = []
    lines.append(f"# Post-market reconciliation ({target.isoformat()})")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Journal trades on date: **{len(journal_rows)}**")
    lines.append(f"- Journal trades matched by parent order_id to IB executions: **{matched}**")
    lines.append(f"- IB executions on date: **{len(ib_exec_rows)}**")
    lines.append(f"- Closed journal trades: **{closed_trades}**")
    lines.append(f"- Closed P&L (journal): **{_fmt_money(closed_pnl)}**")
    lines.append(f"- IB order_ids not present as journal parent order_id: **{len(unmatched_ib)}**")
    lines.append("")

    lines.append("## Journal Trades")
    lines.append("")
    lines.append("| trade_id | symbol | side | status | order_id | entry | exit | pnl | slippage_bps | class |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |")
    for r in journal_rows:
        pnl = r.get("realized_pnl")
        pnl_s = f"{float(pnl):.2f}" if pnl is not None else ""
        sl = r.get("slippage_bps")
        sl_s = f"{float(sl):.2f}" if sl is not None else ""
        entry = r.get("entry_fill_price")
        exit_px = r.get("exit_fill_price")
        lines.append(
            f"| {r.get('trade_id')} | {r.get('symbol')} | {r.get('side')} | {r.get('status')} | "
            f"{r.get('order_id') or ''} | {entry or ''} | {exit_px or ''} | {pnl_s} | {sl_s} | {r.get('classification','')} |"
        )
    if not journal_rows:
        lines.append("| *(none)* | | | | | | | | | |")
    lines.append("")

    lines.append("## IB Executions")
    lines.append("")
    lines.append("| order_id | exec_id | symbol | side | shares | price | time_et |")
    lines.append("| ---: | --- | --- | --- | ---: | ---: | --- |")
    for ex in ib_exec_rows[:500]:
        lines.append(
            f"| {ex['order_id']} | {ex['exec_id']} | {ex['symbol']} | {ex['side']} | "
            f"{ex['shares']:.0f} | {ex['price']:.4f} | {ex['time_et']} |"
        )
    if not ib_exec_rows:
        lines.append("| *(none)* | | | | | | |")
    elif len(ib_exec_rows) > 500:
        lines.append(f"| ... | *(truncated {len(ib_exec_rows) - 500} rows)* | | | | | |")
    lines.append("")

    lines.append("## Unmatched IB Order IDs")
    lines.append("")
    if unmatched_ib:
        lines.append(", ".join(str(x) for x in unmatched_ib))
    else:
        lines.append("*None.*")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Post-market reconcile: IBKR executions vs trade journal")
    parser.add_argument("--date", default=datetime.now(ET).strftime("%Y-%m-%d"), help="ET date YYYY-MM-DD")
    parser.add_argument("--journal-db", type=Path, default=_PYTHONCLIENT_ROOT / "trade_journal.db")
    parser.add_argument("--output", type=Path, default=None, help="Output markdown path (default logs/daily_<date>.md)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=909)
    parser.add_argument("--port", type=int, default=4002, help="IBKR port (4002 paper, 7497 live)")
    return parser.parse_args()


async def _amain() -> None:
    args = parse_args()
    target = date.fromisoformat(args.date)
    out = args.output or (_PYTHONCLIENT_ROOT / "logs" / f"daily_{target.isoformat()}.md")
    result = await reconcile_post_market(
        target=target,
        journal_db=args.journal_db,
        output_path=out,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
    )
    print(f"Wrote {result.output_path}")
    print(
        f"Journal trades={len(result.journal_trades)}, IB execs={len(result.ib_exec_rows)}, "
        f"matched={result.matched_trades}, closed_pnl={result.closed_pnl:.2f}"
    )


if __name__ == "__main__":
    asyncio.run(_amain())
