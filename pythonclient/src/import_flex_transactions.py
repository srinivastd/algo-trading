#!/usr/bin/env python3
"""
Download IBKR Trade Confirmation / Flex report via Flex Web Service and save CSV (or raw).

Typical schedule: once EOD or before the US open (cron / launchd).

Environment:
  IBKR_FLEX_TOKEN   Required. Current token from Client Portal -> Flex Web Service Configuration.
  IBKR_FLEX_QUERY_ID Optional. Defaults to 1463542 (DailyTransactions template).

Example:
  export IBKR_FLEX_TOKEN='your_token_here'
  ./py src/import_flex_transactions.py --out-dir logs/flex --sqlite trade_journal.db
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PYTHONCLIENT_ROOT = _SCRIPT_DIR.parent

from flex_web_client import (  # noqa: E402
    FLEX_BASE_DEFAULT,
    USER_AGENT,
    fetch_flex_trade_report,
    flex_rows_look_like_csv,
)


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _rows_from_flex_xml(content: str) -> list[dict[str, Any]]:
    root = ET.fromstring(content)
    rows: list[dict[str, Any]] = []
    for el in root.iter():
        if _local_tag(el.tag).lower() != "trade":
            continue
        if el.attrib:
            rows.append(dict(el.attrib))
            continue
        row: dict[str, Any] = {}
        for child in el:
            name = _local_tag(child.tag)
            row[name] = (child.text or "").strip()
        if row:
            rows.append(row)
    return rows


def _rows_from_ib_flex_framed_csv(text: str) -> list[dict[str, Any]] | None:
    """
    IBKR Flex Web Service often returns a sectioned CSV wire format:
    BOF -> BOA -> BOS (section) -> header line -> data rows -> EOS -> ...
    If no BOS/CONF section is found, returns None so callers can try plain CSV.
    """
    lines = [ln.strip("\r\n") for ln in text.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        try:
            parts = next(csv.reader([line]))
        except csv.Error:
            continue
        if len(parts) >= 2 and parts[0] == "BOS" and parts[1] == "CONF":
            if i + 1 >= len(lines):
                return []
            try:
                header = next(csv.reader([lines[i + 1]]))
            except csv.Error:
                return []
            if not header:
                return []
            out: list[dict[str, Any]] = []
            for j in range(i + 2, len(lines)):
                try:
                    pr = next(csv.reader([lines[j]]))
                except csv.Error:
                    continue
                if not pr:
                    continue
                if pr[0] == "EOS":
                    break
                if pr[0] in ("BOF", "BOA", "BOS", "EOA", "EOF"):
                    continue
                row = dict(zip(header, pr + [""] * max(0, len(header) - len(pr))))
                out.append(row)
            return out
    return None


def _summarize_ib_flex_wire(text: str) -> str | None:
    """
    Parse BOF / EOS lines from IB's framed CSV wire format for operator visibility.
    EOS,CONF,<n>,<m> — the third field is the Trade Confirms data row count (0 = none).
    """
    lines = [ln.strip("\r\n") for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        bof = next(csv.reader([lines[0]]))
    except csv.Error:
        return None
    if not bof or bof[0] != "BOF":
        return None
    acct = bof[1] if len(bof) > 1 else ""
    name = bof[2] if len(bof) > 2 else ""
    d_from = bof[4] if len(bof) > 4 else ""
    d_to = bof[5] if len(bof) > 5 else ""
    conf_n: str | None = None
    for line in lines:
        try:
            p = next(csv.reader([line]))
        except csv.Error:
            continue
        if len(p) >= 3 and p[0] == "EOS" and p[1] == "CONF":
            conf_n = p[2]
            break
    bits = [f"IB Flex wire OK: template={name!r} account={acct}"]
    if d_from or d_to:
        bits.append(f"period={d_from}..{d_to}")
    if conf_n is not None:
        bits.append(f"trade_confirm_rows={conf_n}")
    return "; ".join(bits)


def _rows_from_csv_bytes(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig", errors="replace")
    if not text.strip():
        return []
    framed = _rows_from_ib_flex_framed_csv(text)
    if framed is not None:
        return [dict(r) for r in framed if any((v or "").strip() for v in r.values())]
    delim = "\t" if "\t" in text.splitlines()[0] and text.splitlines()[0].count("\t") > text.splitlines()[0].count(
        ","
    ) else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    return [dict(r) for r in reader if any((v or "").strip() for v in r.values())]


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map Flex column names (several casings) to stable keys."""
    lower_map = {re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_"): v for k, v in row.items() if k is not None}

    def pick(*names: str) -> Any:
        for n in names:
            if n in row and row[n] not in (None, ""):
                return row[n]
            nl = re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")
            if nl in lower_map and lower_map[nl] not in (None, ""):
                return lower_map[nl]
        return None

    exec_id = pick("ExecID", "execId", "exec_id")
    acct = pick("ClientAccountID", "AccountId", "accountId")
    oid = pick("OrderID", "orderId")
    try:
        order_id_int = int(str(oid).strip()) if oid is not None and str(oid).strip().isdigit() else None
    except (TypeError, ValueError):
        order_id_int = None

    qty = pick("Quantity", "TradeQuantity", "quantity")
    price = pick("Price", "tradePrice")
    comm = pick("Commission", "commission")
    try:
        qf = float(str(qty).replace(",", "")) if qty not in (None, "") else None
    except ValueError:
        qf = None
    try:
        pf = float(str(price).replace(",", "")) if price not in (None, "") else None
    except ValueError:
        pf = None
    try:
        cf = float(str(comm).replace(",", "")) if comm not in (None, "") else None
    except ValueError:
        cf = None

    return {
        "client_account_id": str(acct).strip() if acct else None,
        "exec_id": str(exec_id).strip() if exec_id else None,
        "order_id": order_id_int,
        "trade_id_flex": str(pick("TradeID", "tradeId") or "").strip() or None,
        "symbol": str(pick("Symbol", "symbol") or "").strip().upper() or None,
        "asset_class": str(pick("AssetClass", "assetClass") or "").strip() or None,
        "buy_sell": str(pick("Buy/Sell", "BuySell", "buySell") or "").strip() or None,
        "quantity": qf,
        "price": pf,
        "amount": None,
        "commission": cf,
        "exec_datetime": str(pick("Date/Time", "DateTime", "TradeTime", "tradeTime") or "").strip() or None,
        "trade_date": str(pick("TradeDate", "tradeDate") or "").strip() or None,
        "order_time": str(pick("OrderTime", "orderTime") or "").strip() or None,
        "order_type": str(pick("OrderType", "orderType") or "").strip() or None,
        "raw": dict(row),
    }


def _ensure_flex_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS flex_daily_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            imported_at TEXT NOT NULL,
            query_id TEXT NOT NULL,
            client_account_id TEXT,
            exec_id TEXT NOT NULL,
            order_id INTEGER,
            trade_id_flex TEXT,
            symbol TEXT,
            asset_class TEXT,
            buy_sell TEXT,
            quantity REAL,
            price REAL,
            commission REAL,
            exec_datetime TEXT,
            trade_date TEXT,
            order_time TEXT,
            order_type TEXT,
            raw_json TEXT NOT NULL,
            UNIQUE(client_account_id, exec_id)
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_flex_daily_symbol_time ON flex_daily_transactions(symbol, exec_datetime)"
    )


def _upsert_rows(
    con: sqlite3.Connection,
    *,
    query_id: str,
    rows: list[dict[str, Any]],
) -> tuple[int, int]:
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    skipped = 0
    for r in rows:
        n = _normalize_row(r)
        ex = n.get("exec_id")
        if not ex:
            skipped += 1
            continue
        acct = (n.get("client_account_id") or "").strip()
        try:
            con.execute(
                """
                INSERT INTO flex_daily_transactions(
                    imported_at, query_id, client_account_id, exec_id, order_id, trade_id_flex,
                    symbol, asset_class, buy_sell, quantity, price, commission,
                    exec_datetime, trade_date, order_time, order_type, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_account_id, exec_id) DO UPDATE SET
                    imported_at = excluded.imported_at,
                    order_id = excluded.order_id,
                    trade_id_flex = excluded.trade_id_flex,
                    symbol = excluded.symbol,
                    asset_class = excluded.asset_class,
                    buy_sell = excluded.buy_sell,
                    quantity = excluded.quantity,
                    price = excluded.price,
                    commission = excluded.commission,
                    exec_datetime = excluded.exec_datetime,
                    trade_date = excluded.trade_date,
                    order_time = excluded.order_time,
                    order_type = excluded.order_type,
                    raw_json = excluded.raw_json
                """,
                (
                    now,
                    query_id,
                    acct,
                    ex,
                    n.get("order_id"),
                    n.get("trade_id_flex"),
                    n.get("symbol"),
                    n.get("asset_class"),
                    n.get("buy_sell"),
                    n.get("quantity"),
                    n.get("price"),
                    n.get("commission"),
                    n.get("exec_datetime"),
                    n.get("trade_date"),
                    n.get("order_time"),
                    n.get("order_type"),
                    json.dumps(n.get("raw") or {}, separators=(",", ":")),
                ),
            )
            inserted += 1
        except sqlite3.Error:
            skipped += 1
    return inserted, skipped


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import IBKR Flex DailyTransactions (or any Trade Confirmation query) via Flex Web Service.",
        epilog=f"Requires User-Agent; this client uses: {USER_AGENT!r}. Set IBKR_FLEX_TOKEN in the environment.",
    )
    p.add_argument(
        "--token",
        default=None,
        help="Flex token (default: env IBKR_FLEX_TOKEN)",
    )
    p.add_argument(
        "--query-id",
        default=None,
        help="Flex query id (default: env IBKR_FLEX_QUERY_ID or 1463542)",
    )
    p.add_argument(
        "--base-url",
        default=FLEX_BASE_DEFAULT,
        help="Flex Web Service base URL",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=_PYTHONCLIENT_ROOT / "logs" / "flex",
        help="Directory to write raw report + parsed CSV",
    )
    p.add_argument(
        "--initial-sleep",
        type=float,
        default=5.0,
        help="Seconds to wait after SendRequest before first GetStatement (IB may need time to generate)",
    )
    p.add_argument(
        "--sqlite",
        type=Path,
        default=None,
        help="Optional trade_journal.db path to upsert flex_daily_transactions",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    token = args.token or __import__("os").environ.get("IBKR_FLEX_TOKEN")
    if not token:
        print("Missing token: set IBKR_FLEX_TOKEN or pass --token", file=sys.stderr)
        return 2
    qid = args.query_id or __import__("os").environ.get("IBKR_FLEX_QUERY_ID") or "1463542"

    raw = fetch_flex_trade_report(
        token=token.strip(),
        query_id=str(qid).strip(),
        base_url=args.base_url,
        initial_sleep_s=args.initial_sleep,
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    raw_path = args.out_dir / f"flex_query_{qid}_raw_{stamp}.txt"
    raw_path.write_bytes(raw)

    text = raw.decode("utf-8", errors="replace")
    wire_summary = _summarize_ib_flex_wire(text)
    if wire_summary:
        print(wire_summary)

    rows: list[dict[str, Any]] = []
    if flex_rows_look_like_csv(text):
        rows = _rows_from_csv_bytes(raw)
        csv_path = args.out_dir / f"flex_query_{qid}_{stamp}.csv"
        if rows:
            fieldnames = list(rows[0].keys())
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            print(f"Wrote parsed CSV ({len(rows)} rows): {csv_path}")
        elif wire_summary:
            print(
                "No data rows imported: Trade Confirms section has 0 executions for this "
                "report period (normal if nothing filled, or period excludes your trades). "
                "Adjust the Flex template date range or run after fills settle."
            )
    else:
        rows = _rows_from_flex_xml(text)
        if rows:
            csv_path = args.out_dir / f"flex_query_{qid}_{stamp}.csv"
            keys = sorted({k for r in rows for k in r})
            with csv_path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            print(f"Wrote parsed XML->CSV ({len(rows)} rows): {csv_path}")
        else:
            print(
                f"No CSV or <Trade> rows parsed; inspect raw: {raw_path}",
                file=sys.stderr,
            )

    if args.sqlite:
        con = sqlite3.connect(args.sqlite)
        try:
            _ensure_flex_table(con)
            ins, sk = _upsert_rows(con, query_id=str(qid), rows=rows)
            con.commit()
            print(f"SQLite flex_daily_transactions upserted={ins} skipped_no_exec={sk} db={args.sqlite}")
        finally:
            con.close()

    print(f"Raw response: {raw_path} ({len(raw)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
