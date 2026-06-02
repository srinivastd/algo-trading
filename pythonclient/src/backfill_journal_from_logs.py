#!/usr/bin/env python3
"""
Backfill trade journal rows from market_data JSONL logs.

This script is intentionally conservative: it reconstructs trade attempts and
execution outcomes from log events and writes them to the SQLite trade journal.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytz

from ibkr.connection import IBKRConnection
from trade_journal import TradeJournal

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


def _to_et_date(value: Any) -> date | None:
    dt = _to_et_datetime(value)
    return dt.date() if dt else None


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _side_from_attempt(trade_details: dict[str, Any]) -> str:
    trade_type = str(trade_details.get("trade_type") or "").upper()
    if trade_type in {"LONG", "SHORT"}:
        return trade_type
    signal = int(trade_details.get("signal") or 0)
    return "LONG" if signal >= 0 else "SHORT"


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class BackfillStats:
    attempts: int = 0
    execution_results: int = 0
    success_results: int = 0
    error_results: int = 0
    unmatched_results: int = 0
    rows_deleted: int = 0
    ib_fills_loaded: int = 0
    entry_fills_applied: int = 0
    closed_trades_applied: int = 0
    lifecycle_status_updates: int = 0
    fallback_exit_closes: int = 0
    hint_exit_closes: int = 0


def _delete_existing_backfill_rows(journal_db: Path, date_str: str) -> int:
    con = sqlite3.connect(journal_db)
    try:
        con.row_factory = sqlite3.Row
        like = f"BF_{date_str.replace('-', '')}_%"
        trade_ids = [str(r["trade_id"]) for r in con.execute("SELECT trade_id FROM trades WHERE trade_id LIKE ?", (like,))]
        if trade_ids:
            placeholders = ",".join("?" for _ in trade_ids)
            con.execute(f"DELETE FROM fills WHERE trade_id IN ({placeholders})", trade_ids)
            con.execute(f"DELETE FROM orders WHERE trade_id IN ({placeholders})", trade_ids)
        cur = con.execute("DELETE FROM trades WHERE trade_id LIKE ?", (like,))
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


async def _load_ib_fills(
    *,
    target_date: date,
    host: str,
    port: int,
    client_id: int,
) -> list[dict[str, Any]]:
    connection = IBKRConnection(host=host, port=port, client_id=client_id)
    if not await connection.connect():
        raise RuntimeError("Failed to connect to IBKR for backfill enrichment")
    try:
        ib = connection.get_ib()
        raw_fills = await ib.reqExecutionsAsync()
    finally:
        await connection.disconnect()

    rows: list[dict[str, Any]] = []
    for item in raw_fills:
        ex = getattr(item, "execution", item)
        contract = getattr(item, "contract", None)
        commission_report = getattr(item, "commissionReport", None)
        et_dt = _to_et_datetime(getattr(ex, "time", None))
        if et_dt is None or et_dt.date() != target_date:
            continue
        order_id = _safe_int(getattr(ex, "orderId", None))
        shares = _to_float(getattr(ex, "shares", None))
        price = _to_float(getattr(ex, "price", None))
        if order_id is None or shares is None or price is None:
            continue
        rows.append(
            {
                "order_id": order_id,
                "exec_id": str(getattr(ex, "execId", "")),
                "symbol": str(getattr(contract, "symbol", "") or getattr(ex, "symbol", "")).upper(),
                "side": str(getattr(ex, "side", "")),
                "shares": float(shares),
                "price": float(price),
                "time": et_dt.isoformat(),
                "commission": _to_float(getattr(commission_report, "commission", None)),
            }
        )
    rows.sort(key=lambda r: (r["time"], r["order_id"]))
    return rows


def _extract_order_status_rows(raw_orders: list[Any]) -> dict[int, str]:
    status_by_order: dict[int, str] = {}
    for item in raw_orders:
        order = getattr(item, "order", item)
        order_status = getattr(item, "orderStatus", None)
        order_id = _safe_int(getattr(order, "orderId", None) or getattr(item, "orderId", None))
        if order_id is None:
            continue
        status = (
            str(getattr(order_status, "status", "") or getattr(item, "status", "") or getattr(order, "status", "")).strip()
        )
        if not status:
            continue
        status_by_order[order_id] = status
    return status_by_order


def _normalize_trade_status(raw: str) -> str:
    s = (raw or "").strip().lower()
    if s in {"submitted", "presubmitted", "pendingsubmit"}:
        return "submitted"
    if s in {"filled", "partiallyfilled"}:
        return "open"
    if s in {"inactive", "cancelled", "apicancelled", "pendingcancel"}:
        return "cancelled"
    if s in {"rejected"}:
        return "rejected"
    return "submitted"


def _parse_iso_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
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


async def _load_ib_order_status_map(
    *,
    host: str,
    port: int,
    client_id: int,
) -> dict[int, str]:
    connection = IBKRConnection(host=host, port=port, client_id=client_id)
    if not await connection.connect():
        raise RuntimeError("Failed to connect to IBKR for order lifecycle enrichment")
    try:
        ib = connection.get_ib()
        open_orders: list[Any] = []
        completed_orders: list[Any] = []
        try:
            open_orders = await ib.reqOpenOrdersAsync()
        except Exception:
            try:
                open_orders = list(ib.openTrades())
            except Exception:
                open_orders = []

        try:
            completed_orders = await ib.reqCompletedOrdersAsync(False)
        except TypeError:
            completed_orders = await ib.reqCompletedOrdersAsync()
        except Exception:
            completed_orders = []
    finally:
        await connection.disconnect()

    status_by_order = _extract_order_status_rows(open_orders)
    # Completed status should override open snapshots for same order id.
    status_by_order.update(_extract_order_status_rows(completed_orders))
    return status_by_order


def _signed_pnl(side: str, entry_px: float, exit_px: float, qty: float) -> float:
    if side.upper() == "SHORT":
        return (entry_px - exit_px) * qty
    return (exit_px - entry_px) * qty


def _apply_ib_fill_enrichment(
    *,
    target_date: date,
    journal_db: Path,
    ib_fills: list[dict[str, Any]],
    order_status_map: dict[int, str],
    exit_hints_by_trade_id: dict[str, list[dict[str, Any]]],
    stats: BackfillStats,
) -> None:
    journal = TradeJournal(journal_db)
    by_order: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fill in ib_fills:
        by_order[int(fill["order_id"])].append(fill)
    stats.ib_fills_loaded = len(ib_fills)

    con = sqlite3.connect(journal_db)
    con.row_factory = sqlite3.Row
    try:
        rows = list(
            con.execute(
                """
                SELECT
                    trade_id, symbol, side, status, intended_entry_price, intended_shares,
                    order_id, stop_order_id, target_order_id, entry_fill_price
                FROM trades
                WHERE trade_id LIKE ?
                ORDER BY created_at ASC
                """,
                (f"BF_{target_date.strftime('%Y%m%d')}_%",),
            )
        )
    finally:
        con.close()

    fills_by_symbol_side: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for fill in ib_fills:
        fills_by_symbol_side[(str(fill["symbol"]).upper(), str(fill["side"]).upper())].append(fill)
    for key in fills_by_symbol_side:
        fills_by_symbol_side[key].sort(key=lambda f: str(f["time"]))

    used_exit_exec_ids: set[str] = set()

    for r in rows:
        trade_id = str(r["trade_id"])
        symbol = str(r["symbol"] or "")
        side = str(r["side"] or "LONG").upper()
        order_id = _safe_int(r["order_id"])
        parent_status = _normalize_trade_status(order_status_map.get(order_id, "")) if order_id is not None else ""
        if order_id is not None and parent_status and str(r["status"] or "").lower() in {"submitted", "open"}:
            journal.update_trade_execution_result(
                trade_id=trade_id,
                status=parent_status,
                error_message=None,
                order_id=order_id,
                stop_order_id=_safe_int(r["stop_order_id"]),
                target_order_id=_safe_int(r["target_order_id"]),
            )
            stats.lifecycle_status_updates += 1
        if order_id is None:
            continue
        entry_fills = [f for f in by_order.get(order_id, []) if str(f.get("symbol", "")).upper() == symbol]
        if not entry_fills:
            continue

        entry_qty = sum(float(f["shares"]) for f in entry_fills)
        if entry_qty <= 0:
            continue
        entry_notional = sum(float(f["shares"]) * float(f["price"]) for f in entry_fills)
        entry_vwap = entry_notional / entry_qty
        entry_time = entry_fills[0]["time"]
        intended_entry = _to_float(r["intended_entry_price"]) or entry_vwap
        slippage_bps = ((entry_vwap - intended_entry) / intended_entry * 10000.0) if intended_entry else None

        journal.mark_trade_entry_fill(
            trade_id=trade_id,
            entry_fill_price=float(entry_vwap),
            entry_fill_time=entry_time,
            slippage_bps=slippage_bps,
            status="open",
        )
        for f in entry_fills:
            journal.record_fill(
                trade_id=trade_id,
                order_id=int(f["order_id"]),
                exec_id=str(f["exec_id"] or ""),
                symbol=symbol,
                side=str(f["side"] or ""),
                qty=float(f["shares"]),
                price=float(f["price"]),
                commission=_to_float(f.get("commission")),
                time=f["time"],
                is_entry=True,
            )
        stats.entry_fills_applied += 1

        # Exit fills are expected on target/stop child orders.
        target_order_id = _safe_int(r["target_order_id"])
        stop_order_id = _safe_int(r["stop_order_id"])
        exit_fills: list[dict[str, Any]] = []
        if target_order_id is not None:
            exit_fills.extend(
                [f for f in by_order.get(target_order_id, []) if str(f.get("symbol", "")).upper() == symbol]
            )
        if stop_order_id is not None:
            exit_fills.extend(
                [f for f in by_order.get(stop_order_id, []) if str(f.get("symbol", "")).upper() == symbol]
            )
        fallback_exit = False
        hint_exit = False
        hint_reason = ""
        if not exit_fills:
            # Fallback: infer exits by symbol + opposite side + time window + qty coverage.
            expected_exit_side = "SELL" if side == "LONG" else "BUY"
            entry_dt = _to_et_datetime(entry_time)
            if entry_dt:
                candidates = [
                    f
                    for f in fills_by_symbol_side.get((symbol, expected_exit_side), [])
                    if (_to_et_datetime(f.get("time")) or entry_dt) >= entry_dt
                    and (_to_et_datetime(f.get("time")) or entry_dt) <= entry_dt + timedelta(hours=12)
                    and str(f.get("exec_id") or "") not in used_exit_exec_ids
                ]
                remaining = float(entry_qty)
                chosen: list[dict[str, Any]] = []
                for c in candidates:
                    c_qty = float(c["shares"])
                    chosen.append(c)
                    remaining -= c_qty
                    if remaining <= entry_qty * 0.02:
                        break
                if chosen and (sum(float(f["shares"]) for f in chosen) / entry_qty) >= 0.98:
                    exit_fills = chosen
                    fallback_exit = True
        if not exit_fills:
            # Fallback #2: use explicit position_exit_attempt hints from logs.
            expected_exit_side = "SELL" if side == "LONG" else "BUY"
            entry_dt = _to_et_datetime(entry_time)
            trade_hints = exit_hints_by_trade_id.get(trade_id, [])
            if entry_dt and trade_hints:
                hint_candidates: list[dict[str, Any]] = []
                all_side_fills = fills_by_symbol_side.get((symbol, expected_exit_side), [])
                for hint in trade_hints:
                    hint_dt = _parse_iso_datetime(hint.get("time"))
                    if not hint_dt:
                        continue
                    # Prefer fills near logged exit attempt, and after entry.
                    w_start = max(entry_dt, hint_dt - timedelta(minutes=5))
                    w_end = hint_dt + timedelta(minutes=45)
                    in_window = [
                        f
                        for f in all_side_fills
                        if str(f.get("exec_id") or "") not in used_exit_exec_ids
                        and w_start <= (_to_et_datetime(f.get("time")) or w_start) <= w_end
                    ]
                    if in_window:
                        qty_hint = _to_float(hint.get("qty"))
                        required_qty = min(entry_qty, qty_hint) if qty_hint and qty_hint > 0 else entry_qty
                        running = 0.0
                        chosen: list[dict[str, Any]] = []
                        for c in in_window:
                            chosen.append(c)
                            running += float(c["shares"])
                            if running >= required_qty * 0.98:
                                break
                        if chosen and running >= required_qty * 0.98:
                            hint_candidates = chosen
                            hint_reason = str(hint.get("reason") or "").strip().lower()
                            break
                if hint_candidates:
                    exit_fills = hint_candidates
                    hint_exit = True
        if not exit_fills:
            continue
        exit_fills.sort(key=lambda f: str(f["time"]))

        exit_qty = sum(float(f["shares"]) for f in exit_fills)
        matched_qty = min(entry_qty, exit_qty)
        if matched_qty <= 0 or (matched_qty / entry_qty) < 0.98:
            # Keep it open if we only see partial close in broker fills.
            continue
        exit_notional = sum(float(f["shares"]) * float(f["price"]) for f in exit_fills)
        exit_vwap = exit_notional / exit_qty
        exit_time = exit_fills[-1]["time"]
        commissions = sum(_to_float(f.get("commission")) or 0.0 for f in entry_fills + exit_fills)
        pnl = _signed_pnl(side=side, entry_px=float(entry_vwap), exit_px=float(exit_vwap), qty=float(matched_qty)) - commissions
        if target_order_id is not None and by_order.get(target_order_id):
            exit_reason = "target"
        elif stop_order_id is not None and by_order.get(stop_order_id):
            exit_reason = "stop_loss"
        elif hint_exit and hint_reason in {"target", "stop_loss", "strategy_signal", "time"}:
            exit_reason = hint_reason
        elif fallback_exit:
            exit_reason = "strategy_signal"
        else:
            exit_reason = "strategy_signal"

        journal.close_trade(
            trade_id=trade_id,
            exit_fill_price=float(exit_vwap),
            exit_fill_time=exit_time,
            exit_reason=exit_reason,
            realized_pnl=float(pnl),
            status="closed",
        )
        for f in exit_fills:
            journal.record_fill(
                trade_id=trade_id,
                order_id=int(f["order_id"]),
                exec_id=str(f["exec_id"] or ""),
                symbol=symbol,
                side=str(f["side"] or ""),
                qty=float(f["shares"]),
                price=float(f["price"]),
                commission=_to_float(f.get("commission")),
                time=f["time"],
                is_entry=False,
            )
            if fallback_exit:
                used_exit_exec_ids.add(str(f.get("exec_id") or ""))
        stats.closed_trades_applied += 1
        if fallback_exit:
            stats.fallback_exit_closes += 1
        if hint_exit:
            stats.hint_exit_closes += 1


def backfill_from_market_log(
    *,
    target_date: date,
    market_log: Path,
    journal_db: Path,
    replace: bool,
    enrich_ib: bool,
    host: str,
    port: int,
    client_id: int,
) -> BackfillStats:
    stats = BackfillStats()
    journal = TradeJournal(journal_db)

    date_key = target_date.isoformat()
    if replace:
        stats.rows_deleted = _delete_existing_backfill_rows(journal_db, date_key)

    run_id = journal.start_run(
        strategy="backfill_log",
        symbols=[],
        params={
            "source": str(market_log),
            "target_date": date_key,
            "replace": replace,
        },
    )

    # Some legacy logs reuse IDs after restarts. Queue by (orig_id, symbol).
    pending_by_key: dict[tuple[str, str], deque[str]] = defaultdict(deque)
    side_by_trade_id: dict[str, str] = {}
    orig_key_by_backfill_trade_id: dict[str, tuple[str, str]] = {}
    attempt_time_by_backfill_trade_id: dict[str, str] = {}
    exit_hints_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    with market_log.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event_date = _to_et_date(event.get("timestamp"))
            if event_date != target_date:
                continue

            action = str(event.get("action") or "")
            symbol = str(event.get("symbol") or "").upper()
            if not symbol:
                continue

            if action == "trade_execution_attempt":
                td = event.get("trade_details") or {}
                if not isinstance(td, dict):
                    continue

                orig_trade_id = str(event.get("trade_id") or "UNKNOWN")
                backfill_trade_id = f"BF_{target_date.strftime('%Y%m%d')}_{line_no}_{orig_trade_id}"
                signal = int(td.get("signal") or 0)
                entry_price = _to_float(td.get("entry_price"))
                shares = _safe_int(td.get("shares"))
                if entry_price is None or shares is None or shares <= 0:
                    continue

                side = _side_from_attempt(td)
                journal.log_trade_attempt(
                    trade_id=backfill_trade_id,
                    run_id=run_id,
                    symbol=symbol,
                    side=side,
                    signal_value=signal,
                    intended_entry_price=entry_price,
                    intended_shares=shares,
                    stop_loss=_to_float(td.get("stop_loss")),
                    target=_to_float(td.get("target")),
                    indicator_snapshot={},
                    memory_consulted=False,
                    memory_verdict=None,
                )
                pending_by_key[(orig_trade_id, symbol)].append(backfill_trade_id)
                side_by_trade_id[backfill_trade_id] = side
                orig_key_by_backfill_trade_id[backfill_trade_id] = (orig_trade_id, symbol)
                ts = str(event.get("timestamp") or "")
                if ts:
                    attempt_time_by_backfill_trade_id[backfill_trade_id] = ts
                stats.attempts += 1
                continue

            if action == "position_exit_attempt":
                orig_trade_id = str(event.get("trade_id") or "UNKNOWN")
                details = event.get("exit_details") or {}
                if not isinstance(details, dict):
                    details = {}
                exit_hints_by_key[(orig_trade_id, symbol)].append(
                    {
                        "time": str(event.get("timestamp") or ""),
                        "qty": _to_float(details.get("exit_quantity")),
                        "reason": str(details.get("exit_reason") or ""),
                        "action": str(details.get("exit_action") or ""),
                    }
                )
                continue

            if action == "trade_execution_result":
                stats.execution_results += 1
                tr = event.get("trade_result") or {}
                if not isinstance(tr, dict):
                    stats.unmatched_results += 1
                    continue
                orig_trade_id = str(event.get("trade_id") or "UNKNOWN")
                queue = pending_by_key.get((orig_trade_id, symbol))
                if not queue:
                    stats.unmatched_results += 1
                    continue
                trade_id = queue.popleft()
                status = str(tr.get("status") or "").lower()
                order_id = _safe_int(tr.get("order_id"))
                target_order_id = _safe_int(tr.get("target_order_id"))
                stop_order_id = _safe_int(tr.get("stop_order_id"))
                if status == "success":
                    side = side_by_trade_id.get(trade_id)
                    journal.update_trade_execution_result(
                        trade_id=trade_id,
                        status="submitted",
                        error_message=None,
                        order_id=order_id,
                        stop_order_id=stop_order_id,
                        target_order_id=target_order_id,
                    )
                    if order_id is not None:
                        journal.upsert_order(
                            order_id=order_id,
                            trade_id=trade_id,
                            symbol=symbol,
                            order_type=None,
                            action="BUY" if side == "LONG" else "SELL",
                            qty=None,
                            limit_price=None,
                            status="submitted",
                            parent_order_id=None,
                        )
                    stats.success_results += 1
                else:
                    journal.update_trade_execution_result(
                        trade_id=trade_id,
                        status="rejected",
                        error_message=str(tr.get("message") or "execution error"),
                        order_id=order_id,
                        stop_order_id=stop_order_id,
                        target_order_id=target_order_id,
                    )
                    stats.error_results += 1

    journal.end_run(run_id)

    if enrich_ib:
        exit_hints_by_trade_id: dict[str, list[dict[str, Any]]] = {}
        for trade_id, orig_key in orig_key_by_backfill_trade_id.items():
            hints = list(exit_hints_by_key.get(orig_key, []))
            attempt_dt = _parse_iso_datetime(attempt_time_by_backfill_trade_id.get(trade_id))
            if attempt_dt:
                # Keep hints after attempt and within a realistic horizon.
                filtered: list[dict[str, Any]] = []
                for h in hints:
                    h_dt = _parse_iso_datetime(h.get("time"))
                    if not h_dt:
                        continue
                    if attempt_dt <= h_dt <= attempt_dt + timedelta(hours=12):
                        filtered.append(h)
                hints = filtered
            if hints:
                exit_hints_by_trade_id[trade_id] = hints
        ib_fills = asyncio.run(
            _load_ib_fills(
                target_date=target_date,
                host=host,
                port=port,
                client_id=client_id,
            )
        )
        order_status_map = asyncio.run(
            _load_ib_order_status_map(
                host=host,
                port=port,
                client_id=client_id,
            )
        )
        _apply_ib_fill_enrichment(
            target_date=target_date,
            journal_db=journal_db,
            ib_fills=ib_fills,
            order_status_map=order_status_map,
            exit_hints_by_trade_id=exit_hints_by_trade_id,
            stats=stats,
        )
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill trades into journal from market_data log")
    parser.add_argument("--date", required=True, help="ET date YYYY-MM-DD")
    parser.add_argument(
        "--market-log",
        type=Path,
        default=_PYTHONCLIENT_ROOT / "market_data_logs" / "market_data.json",
        help="Path to market_data JSONL log",
    )
    parser.add_argument(
        "--journal-db",
        type=Path,
        default=_PYTHONCLIENT_ROOT / "trade_journal.db",
        help="Path to trade_journal.db",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete previous backfill rows for this date before insert",
    )
    parser.add_argument("--enrich-ib", action="store_true", help="Enrich backfilled rows with IB executions")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=909)
    parser.add_argument("--port", type=int, default=4002, help="IBKR port (4002 paper, 7497 live)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target = date.fromisoformat(args.date)
    stats = backfill_from_market_log(
        target_date=target,
        market_log=args.market_log,
        journal_db=args.journal_db,
        replace=args.replace,
        enrich_ib=args.enrich_ib,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
    )
    print(
        "Backfill complete: "
        f"attempts={stats.attempts}, execution_results={stats.execution_results}, "
        f"success_results={stats.success_results}, error_results={stats.error_results}, "
        f"unmatched_results={stats.unmatched_results}, rows_deleted={stats.rows_deleted}, "
        f"ib_fills_loaded={stats.ib_fills_loaded}, entry_fills_applied={stats.entry_fills_applied}, "
        f"closed_trades_applied={stats.closed_trades_applied}, "
        f"lifecycle_status_updates={stats.lifecycle_status_updates}, "
        f"fallback_exit_closes={stats.fallback_exit_closes}, "
        f"hint_exit_closes={stats.hint_exit_closes}"
    )


if __name__ == "__main__":
    main()
