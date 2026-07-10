"""
Periodic IB ↔ trade-journal reconcile.

Pulls IB executions (and optional net positions), matches them to journal trades by
parent / stop / target order ids, updates fill qty/status when IB shows a flat
position that the journal still has open, and persists ``reconcile_status`` for
the dashboard.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional

import pytz

from trade_journal import TradeJournal

logger = logging.getLogger(__name__)
ET = pytz.timezone("US/Eastern")

# Status values stored on trades.reconcile_status
STATUS_MATCHED = "matched"
STATUS_QTY_MISMATCH = "qty_mismatch"
STATUS_STATUS_MISMATCH = "status_mismatch"
STATUS_JOURNAL_ONLY = "journal_only"
STATUS_UNRECONCILED = "unreconciled"

_QTY_EPS = 0.51  # allow half-share float noise vs whole shares


@dataclass
class ReconcileSyncResult:
    day: str
    trades_checked: int = 0
    matched: int = 0
    qty_mismatch: int = 0
    status_mismatch: int = 0
    journal_only: int = 0
    closed_from_ib: int = 0
    unmatched_ib_order_ids: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "trades_checked": self.trades_checked,
            "matched": self.matched,
            "qty_mismatch": self.qty_mismatch,
            "status_mismatch": self.status_mismatch,
            "journal_only": self.journal_only,
            "closed_from_ib": self.closed_from_ib,
            "unmatched_ib_order_ids": list(self.unmatched_ib_order_ids),
            "notes": list(self.notes),
        }


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


def _vwap(legs: list[dict[str, Any]]) -> tuple[float | None, float, datetime | None]:
    """Return (vwap_price, total_qty, latest_time)."""
    tot_q = 0.0
    tot_pxq = 0.0
    latest: datetime | None = None
    for leg in legs:
        q = float(leg.get("shares") or 0.0)
        p = float(leg.get("price") or 0.0)
        if q <= 0 or p <= 0:
            continue
        tot_q += q
        tot_pxq += q * p
        t = _to_et_datetime(leg.get("time"))
        if t is not None and (latest is None or t > latest):
            latest = t
    if tot_q <= 0:
        return None, 0.0, latest
    return tot_pxq / tot_q, tot_q, latest


def _normalize_side(side: Any) -> str:
    u = str(side or "").strip().upper()
    if u in ("BOT", "BUY"):
        return "BUY"
    if u in ("SLD", "SELL"):
        return "SELL"
    return u


def _parse_ib_execution_item(item: Any) -> dict[str, Any] | None:
    ex = getattr(item, "execution", item)
    contract = getattr(item, "contract", None)
    try:
        order_id = int(getattr(ex, "orderId", 0) or 0)
    except (TypeError, ValueError):
        return None
    if order_id <= 0:
        return None
    try:
        shares = float(getattr(ex, "shares", 0) or 0)
        price = float(getattr(ex, "price", 0) or 0)
    except (TypeError, ValueError):
        return None
    if shares <= 0 or price <= 0:
        return None
    symbol = (
        str(getattr(ex, "symbol", None) or getattr(contract, "symbol", "") or "")
        .strip()
        .upper()
    )
    if not symbol:
        return None
    return {
        "order_id": order_id,
        "exec_id": str(getattr(ex, "execId", "") or ""),
        "symbol": symbol,
        "side": _normalize_side(getattr(ex, "side", "")),
        "shares": shares,
        "price": price,
        "time": getattr(ex, "time", None),
        "raw": item,
    }


def _trade_order_ids(trade: Mapping[str, Any]) -> set[int]:
    ids: set[int] = set()
    for key in ("order_id", "stop_order_id", "target_order_id"):
        v = trade.get(key)
        if v is None:
            continue
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i > 0:
            ids.add(i)
    return ids


def _entry_exit_legs(
    trade: Mapping[str, Any],
    by_order: Mapping[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split IB execs for this trade into entry vs exit legs by order id."""
    entry: list[dict[str, Any]] = []
    exit_legs: list[dict[str, Any]] = []
    parent = trade.get("order_id")
    stop = trade.get("stop_order_id")
    target = trade.get("target_order_id")
    try:
        parent_i = int(parent) if parent is not None else None
    except (TypeError, ValueError):
        parent_i = None
    for oid in _trade_order_ids(trade):
        legs = by_order.get(oid) or []
        if parent_i is not None and oid == parent_i:
            entry.extend(legs)
        elif (stop is not None and oid == int(stop)) or (
            target is not None and oid == int(target)
        ):
            exit_legs.extend(legs)
        else:
            # Unknown child — treat as exit if opposite of entry side, else entry.
            side = str(trade.get("side") or "").upper()
            entry_action = "BUY" if side == "LONG" else "SELL"
            for leg in legs:
                if leg.get("side") == entry_action:
                    entry.append(leg)
                else:
                    exit_legs.append(leg)
    return entry, exit_legs


def _realized_pnl(
    *,
    side: str,
    entry_px: float,
    exit_px: float,
    qty: float,
) -> float:
    if side.upper() == "LONG":
        return (exit_px - entry_px) * qty
    return (entry_px - exit_px) * qty


async def sync_journal_with_ib(
    *,
    journal: TradeJournal,
    connection: Any,
    day: date | None = None,
    apply_closes: bool = True,
    qty_eps: float = _QTY_EPS,
) -> ReconcileSyncResult:
    """
    Ingest IB executions, classify open/today journal trades, optionally close
    journal rows when IB shows a completed exit (target/stop filled / flat).
    """
    target_day = day or datetime.now(ET).date()
    day_s = target_day.isoformat()
    result = ReconcileSyncResult(day=day_s)

    if journal is None or connection is None:
        result.notes.append("missing journal or connection")
        return result

    ib = connection.get_ib()
    try:
        raw_execs = await ib.reqExecutionsAsync()
    except Exception as e:
        logger.warning("reconcile: reqExecutions failed: %s", e)
        result.notes.append(f"reqExecutions failed: {e}")
        return result

    # Persist into ib_executions / fills first.
    try:
        n = journal.backfill_executions_from_ib_fills(raw_execs or [])
        if n:
            logger.info("reconcile: ingested/updated %d IB execution(s)", n)
    except Exception as e:
        logger.warning("reconcile: backfill_executions failed: %s", e)
        result.notes.append(f"backfill failed: {e}")

    parsed: list[dict[str, Any]] = []
    for item in raw_execs or []:
        row = _parse_ib_execution_item(item)
        if row is None:
            continue
        # Keep all recent execs for order matching (overnight holds); day filter
        # applies to which *journal* trades we update.
        parsed.append(row)

    by_order: dict[int, list[dict[str, Any]]] = {}
    for row in parsed:
        by_order.setdefault(int(row["order_id"]), []).append(row)

    # Live net positions (symbol -> shares).
    net_pos: dict[str, float] = {}
    try:
        positions = await ib.reqPositionsAsync()
        for p in positions or []:
            c = getattr(p, "contract", None)
            if c is None:
                continue
            if getattr(c, "secType", "") not in ("STK", ""):
                continue
            sym = str(getattr(c, "symbol", "") or "").strip().upper()
            if not sym:
                continue
            net_pos[sym] = net_pos.get(sym, 0.0) + float(getattr(p, "position", 0.0) or 0.0)
    except Exception as e:
        logger.debug("reconcile: reqPositions failed: %s", e)
        result.notes.append(f"positions unavailable: {e}")

    trades = journal.list_trades_for_reconcile(day_yyyy_mm_dd=day_s, include_open=True)
    result.trades_checked = len(trades)

    journal_order_ids: set[int] = set()
    for t in trades:
        journal_order_ids |= _trade_order_ids(t)

    # IB orders on this calendar day with no journal parent/stop/target.
    day_ib_oids: set[int] = set()
    for row in parsed:
        t = _to_et_datetime(row.get("time"))
        if t is not None and t.date() == target_day:
            day_ib_oids.add(int(row["order_id"]))
    result.unmatched_ib_order_ids = sorted(day_ib_oids - journal_order_ids)

    now = datetime.utcnow()

    for trade in trades:
        tid = str(trade["trade_id"])
        status = str(trade.get("status") or "").lower()
        side = str(trade.get("side") or "").upper()
        symbol = str(trade.get("symbol") or "").upper()
        oids = _trade_order_ids(trade)

        entry_legs, exit_legs = _entry_exit_legs(trade, by_order)
        entry_px, ib_entry_qty, entry_t = _vwap(entry_legs)
        exit_px, ib_exit_qty, exit_t = _vwap(exit_legs)

        journal_entry_qty = float(trade.get("entry_fill_qty") or 0.0)
        if journal_entry_qty <= 0 and trade.get("intended_shares"):
            journal_entry_qty = float(trade["intended_shares"])
        journal_exit_qty = float(trade.get("exit_fill_qty") or 0.0)

        has_ib_entry = ib_entry_qty > qty_eps
        has_ib_exit = ib_exit_qty > qty_eps
        flat_in_ib = abs(float(net_pos.get(symbol, 0.0))) <= qty_eps

        notes_parts: list[str] = []
        patch: dict[str, Any] = {
            "trade_id": tid,
            "reconcile_status": STATUS_UNRECONCILED,
            "reconciled_at": now,
            "ib_entry_qty": ib_entry_qty if has_ib_entry else (0.0 if oids else None),
            "ib_exit_qty": ib_exit_qty if has_ib_exit else (0.0 if oids else None),
        }

        if not oids:
            patch["reconcile_status"] = STATUS_JOURNAL_ONLY
            patch["reconcile_notes"] = "no order_id/stop/target on journal row"
            journal.update_trade_reconcile(**patch)
            result.journal_only += 1
            continue

        if not has_ib_entry and not has_ib_exit:
            patch["reconcile_status"] = STATUS_JOURNAL_ONLY
            patch["reconcile_notes"] = f"no IB executions for order_ids={sorted(oids)}"
            journal.update_trade_reconcile(**patch)
            result.journal_only += 1
            continue

        # Optionally close journal when IB shows a completed exit and position is flat.
        closed_now = False
        if (
            apply_closes
            and status in ("open", "submitted")
            and has_ib_exit
            and flat_in_ib
        ):
            use_entry_px = entry_px
            if use_entry_px is None:
                use_entry_px = trade.get("entry_fill_price") or trade.get(
                    "intended_entry_price"
                )
            use_exit_px = exit_px
            use_qty = min(ib_entry_qty or journal_entry_qty, ib_exit_qty) if has_ib_entry else ib_exit_qty
            if use_entry_px and use_exit_px and use_qty > 0:
                pnl = _realized_pnl(
                    side=side,
                    entry_px=float(use_entry_px),
                    exit_px=float(use_exit_px),
                    qty=float(use_qty),
                )
                patch["status"] = "closed"
                patch["exit_fill_price"] = float(use_exit_px)
                patch["exit_fill_time"] = exit_t or now
                patch["exit_reason"] = "reconcile:ib_exit_fill"
                patch["realized_pnl"] = pnl
                if has_ib_entry and entry_px is not None:
                    patch["entry_fill_price"] = float(entry_px)
                    if entry_t is not None:
                        patch["entry_fill_time"] = entry_t
                closed_now = True
                result.closed_from_ib += 1
                notes_parts.append(
                    f"closed from IB exit qty={use_qty:.0f} pnl={pnl:.2f}"
                )
                status = "closed"

        # Classify match quality.
        qty_ok = True
        if has_ib_entry and journal_entry_qty > 0:
            if abs(ib_entry_qty - journal_entry_qty) > qty_eps:
                qty_ok = False
                notes_parts.append(
                    f"entry qty IB={ib_entry_qty:.2f} journal_fills={journal_entry_qty:.2f}"
                )
        intended = float(trade.get("intended_shares") or 0.0)
        if has_ib_entry and intended > 0 and abs(ib_entry_qty - intended) > qty_eps:
            # Intended vs IB is informational; only force qty_mismatch if fills also disagree
            # or fills are empty (dashboard was using intended as size).
            if journal_entry_qty <= qty_eps or abs(ib_entry_qty - journal_entry_qty) > qty_eps:
                qty_ok = False
                notes_parts.append(
                    f"entry qty IB={ib_entry_qty:.2f} intended={intended:.0f}"
                )

        if status == "closed" and has_ib_exit and journal_exit_qty > 0:
            if abs(ib_exit_qty - journal_exit_qty) > qty_eps:
                qty_ok = False
                notes_parts.append(
                    f"exit qty IB={ib_exit_qty:.2f} journal_fills={journal_exit_qty:.2f}"
                )

        status_ok = True
        if status in ("open", "submitted") and has_ib_exit and flat_in_ib and not closed_now:
            status_ok = False
            notes_parts.append("journal open but IB exit filled and position flat")
        if status == "closed" and not flat_in_ib and abs(net_pos.get(symbol, 0.0)) > qty_eps:
            # Another open trade on same symbol may explain residual — soft note only.
            notes_parts.append(
                f"journal closed but IB net {symbol}={net_pos.get(symbol, 0.0):.2f}"
            )

        if not status_ok:
            patch["reconcile_status"] = STATUS_STATUS_MISMATCH
            result.status_mismatch += 1
        elif not qty_ok:
            patch["reconcile_status"] = STATUS_QTY_MISMATCH
            result.qty_mismatch += 1
        else:
            patch["reconcile_status"] = STATUS_MATCHED
            result.matched += 1
            if not notes_parts:
                notes_parts.append("IB executions match journal")

        patch["reconcile_notes"] = "; ".join(notes_parts)[:500]
        try:
            journal.update_trade_reconcile(**patch)
        except Exception as e:
            logger.warning("reconcile: update failed for %s: %s", tid, e)
            result.notes.append(f"update {tid}: {e}")

    logger.info(
        "RECONCILE_SYNC day=%s checked=%d matched=%d qty_mismatch=%d "
        "status_mismatch=%d journal_only=%d closed_from_ib=%d unmatched_ib=%d",
        day_s,
        result.trades_checked,
        result.matched,
        result.qty_mismatch,
        result.status_mismatch,
        result.journal_only,
        result.closed_from_ib,
        len(result.unmatched_ib_order_ids),
    )
    return result


async def _main_cli() -> int:
    import argparse
    from pathlib import Path

    from ibkr.connection import IBKRConnection

    root = Path(__file__).resolve().parent.parent
    p = argparse.ArgumentParser(description="One-shot IB ↔ journal reconcile sync")
    p.add_argument("--journal-db", type=Path, default=root / "trade_journal.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=4002)
    p.add_argument("--client-id", type=int, default=91)
    p.add_argument("--day", default=None, help="YYYY-MM-DD (default: today ET)")
    p.add_argument("--no-apply-closes", action="store_true")
    args = p.parse_args()

    day = None
    if args.day:
        day = date.fromisoformat(args.day)

    journal = TradeJournal(args.journal_db)
    conn = IBKRConnection(host=args.host, port=args.port, client_id=args.client_id)
    if not await conn.connect():
        print("Failed to connect to IBKR")
        return 1
    try:
        res = await sync_journal_with_ib(
            journal=journal,
            connection=conn,
            day=day,
            apply_closes=not args.no_apply_closes,
        )
        print(res.as_dict())
    finally:
        await conn.disconnect()
    return 0


if __name__ == "__main__":
    import asyncio
    import sys

    sys.exit(asyncio.run(_main_cli()))
