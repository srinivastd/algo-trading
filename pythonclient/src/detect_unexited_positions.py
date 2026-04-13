#!/usr/bin/env python3
"""
Detect entered-but-not-fully-exited trades from the trade journal.

Optional: compare aggregate journal open quantity per symbol with broker
positions from IBKR.
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
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(ET)


def _et_date(value: Any) -> date | None:
    dt = _to_et_datetime(value)
    return dt.date() if dt else None


def _to_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class OpenTrade:
    trade_id: str
    symbol: str
    side: str
    entry_time: str | None
    net_qty: float
    entry_qty: float


def _load_open_trades(
    *,
    journal_db: Path,
    target_date: date | None,
) -> list[OpenTrade]:
    con = sqlite3.connect(journal_db)
    con.row_factory = sqlite3.Row
    try:
        rows = list(
            con.execute(
                """
                SELECT
                    t.trade_id,
                    t.symbol,
                    t.side,
                    t.entry_fill_time,
                    SUM(CASE WHEN f.is_entry = 1 THEN f.qty ELSE 0 END) AS entry_qty,
                    SUM(
                        CASE
                            WHEN UPPER(t.side) = 'LONG' AND f.is_entry = 1 THEN f.qty
                            WHEN UPPER(t.side) = 'LONG' AND f.is_entry = 0 THEN -f.qty
                            WHEN UPPER(t.side) = 'SHORT' AND f.is_entry = 1 THEN -f.qty
                            WHEN UPPER(t.side) = 'SHORT' AND f.is_entry = 0 THEN f.qty
                            ELSE 0
                        END
                    ) AS net_qty
                FROM trades t
                JOIN fills f ON f.trade_id = t.trade_id
                GROUP BY t.trade_id, t.symbol, t.side, t.entry_fill_time
                HAVING ABS(net_qty) > 0.0001
                ORDER BY t.entry_fill_time ASC
                """
            )
        )
    finally:
        con.close()

    out: list[OpenTrade] = []
    for r in rows:
        d_candidates = [
            _et_date(r["entry_fill_time"]),
        ]
        if target_date is not None and target_date not in [d for d in d_candidates if d is not None]:
            continue
        out.append(
            OpenTrade(
                trade_id=str(r["trade_id"]),
                symbol=str(r["symbol"]).upper(),
                side=str(r["side"]).upper(),
                entry_time=str(r["entry_fill_time"]) if r["entry_fill_time"] else None,
                net_qty=_to_float(r["net_qty"]),
                entry_qty=_to_float(r["entry_qty"]),
            )
        )
    return out


def _aggregate_journal_open_by_symbol(open_trades: list[OpenTrade]) -> dict[str, float]:
    agg: dict[str, float] = {}
    for t in open_trades:
        agg[t.symbol] = agg.get(t.symbol, 0.0) + t.net_qty
    return agg


async def _load_broker_positions(
    *,
    host: str,
    port: int,
    client_id: int,
) -> dict[str, float]:
    connection = IBKRConnection(host=host, port=port, client_id=client_id)
    if not await connection.connect():
        raise RuntimeError("Failed to connect to IBKR for position check")
    try:
        ib = connection.get_ib()
        try:
            broker_positions = await ib.reqPositionsAsync()
        except Exception:
            broker_positions = ib.positions()
        out: dict[str, float] = {}
        for p in broker_positions:
            symbol = str(getattr(p.contract, "symbol", "")).upper()
            if not symbol:
                continue
            qty = _to_float(getattr(p, "position", 0.0))
            out[symbol] = out.get(symbol, 0.0) + qty
        return out
    finally:
        await connection.disconnect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect entered-but-not-exited positions from journal fills")
    parser.add_argument("--date", default=None, help="Optional ET date YYYY-MM-DD to filter by entry date")
    parser.add_argument("--journal-db", type=Path, default=_PYTHONCLIENT_ROOT / "trade_journal.db")
    parser.add_argument("--compare-ibkr", action="store_true", help="Compare journal open qty to broker positions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=909)
    parser.add_argument("--port", type=int, default=4002, help="IBKR port (4002 paper, 7497 live)")
    return parser.parse_args()


def _print_report(
    *,
    open_trades: list[OpenTrade],
    journal_by_symbol: dict[str, float],
    broker_by_symbol: dict[str, float] | None,
) -> None:
    print("## Journal Open Trades")
    print(f"count={len(open_trades)}")
    print("trade_id|symbol|side|entry_time|entry_qty|net_qty")
    for t in open_trades[:300]:
        print(
            f"{t.trade_id}|{t.symbol}|{t.side}|{t.entry_time or ''}|"
            f"{t.entry_qty:.4f}|{t.net_qty:.4f}"
        )
    if len(open_trades) > 300:
        print(f"... truncated {len(open_trades) - 300} rows ...")

    print("")
    print("## Journal Open Qty by Symbol")
    print("symbol|journal_net_qty")
    for symbol, qty in sorted(journal_by_symbol.items()):
        print(f"{symbol}|{qty:.4f}")

    if broker_by_symbol is None:
        return

    print("")
    print("## Broker vs Journal")
    print("symbol|journal_qty|broker_qty|delta")
    symbols = sorted(set(journal_by_symbol.keys()) | set(broker_by_symbol.keys()))
    for symbol in symbols:
        j = journal_by_symbol.get(symbol, 0.0)
        b = broker_by_symbol.get(symbol, 0.0)
        print(f"{symbol}|{j:.4f}|{b:.4f}|{(j - b):.4f}")


def main() -> None:
    args = parse_args()
    target_date = date.fromisoformat(args.date) if args.date else None
    open_trades = _load_open_trades(journal_db=args.journal_db, target_date=target_date)
    journal_by_symbol = _aggregate_journal_open_by_symbol(open_trades)
    broker_by_symbol: dict[str, float] | None = None
    if args.compare_ibkr:
        broker_by_symbol = asyncio.run(
            _load_broker_positions(
                host=args.host,
                port=args.port,
                client_id=args.client_id,
            )
        )
    _print_report(
        open_trades=open_trades,
        journal_by_symbol=journal_by_symbol,
        broker_by_symbol=broker_by_symbol,
    )


if __name__ == "__main__":
    main()
