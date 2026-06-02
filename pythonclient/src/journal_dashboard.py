#!/usr/bin/env python3
"""
HTTP view of journal DB: IB ``daily_snapshots`` (reqPnL) + **activity by symbol**
(wins / losses / flat from closed ``trades`` rows). Supports **?date=YYYY-MM-DD** query param
to view day-specific metrics: activity filters closed trades by exit date; **top P&amp;L cards**
use the last ``daily_snapshots`` row for that calendar ``date`` column (aligned with watch-mode snapshots).
With a date selected, a **per-trade** section lists each ``trade_id`` grouped by symbol: entry/exit
times, intended and filled quantities, P&amp;L, and JSON params (entry snapshot vs any ``exit_*`` keys).
JSON: ``/api/trades-day?date=YYYY-MM-DD`` (optional ``limit=``) ·
``/api/trade?trade_id=…`` (full entry/exit snapshot, no truncation) ·
``/api/trade-zone-chart?symbol=…&day=…`` (zone chart + journal markers).

Default: http://127.0.0.1:8765/ (shows latest + all-time activity)

From ``pythonclient``::

    ./py src/journal_dashboard.py
    ./py src/journal_dashboard.py --db /path/to/trade_journal.db --port 8765
    ./py src/journal_dashboard.py --purge-before-date 2026-04-17   # destructive: drop history before that day

If port 8765 is busy, the server tries the next ports (8766, …) automatically, or pass ``--port 8766``.
Use ``--port 0`` to bind an ephemeral free port (URL printed on startup). If a separate
``zone_validation_app`` (or any process) is already bound to 8765, start it on e.g. 8766
(see ``run_zone_validation_server.sh``) so the journal URL matches the port printed on startup.

**Per-trade Review** link opens a panel with the trade snapshot and a supply/demand + journal chart via
``/api/trade-zone-chart?symbol=…&day=…`` (optional ``run_id=``, ``bar_size=``), same
logic as the zone validation tool; requires a matching bar CSV under ``historical_data/``.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import html
import json
import re
import sys
import threading
import time
from collections import defaultdict
import urllib.parse
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# pythonclient/ (parent of this file's directory)
_PYTHONCLIENT_ROOT = Path(__file__).resolve().parent.parent
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from trade_journal import TradeJournal  # noqa: E402

ET = ZoneInfo("America/New_York")


def _journal_api_log(msg: str) -> None:
    print(f"[journal-api] {msg}", file=sys.stderr, flush=True)


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{float(v):,.2f}"


def _fmt_qty_cell(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if abs(x - round(x)) < 1e-6:
        return str(int(round(x)))
    return f"{x:,.2f}"


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _safe_float(v: object) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _json_pre_block(obj: object, max_chars: int = 1200) -> str:
    try:
        s = json.dumps(obj, indent=2, default=str)
    except TypeError:
        s = str(obj)
    if len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return f"<pre class='json-block'>{html.escape(s)}</pre>"


def _build_trades_day_html(trades: list[dict], selected_date: str | None) -> str:
    """
    Per-trade rows for a calendar day: entry/exit times, quantities, prices, reasons,
    and indicator snapshot (entry keys vs ``exit_*`` keys in JSON).
    """
    if not selected_date:
        return (
            "<h2>Per-trade detail (entry / exit / qty / params)</h2>"
            '<p class="meta">Choose a <strong>date</strong> above. Each row is a <code>trade_id</code> whose '
            "<strong>entry</strong> or <strong>exit</strong> fill timestamp falls on that day. Quantities "
            "use <code>intended_shares</code> plus Σ <code>fills</code> qty (entry vs exit legs). "
            "Params come from <code>indicator_snapshot_json</code> at trade creation (entry block); "
            "separate exit-bar indicators are only shown if stored under <code>exit_*</code> keys.</p>"
        )
    d_esc = html.escape(selected_date)
    if not trades:
        return (
            f"<h2>Per-trade detail for {d_esc}</h2>"
            "<p class=\"meta\">No <code>trades</code> rows with entry or exit on this day.</p>"
        )
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for row in trades:
        by_sym[str(row.get("symbol") or "")].append(row)
    parts: list[str] = [
        f"<h2>Per-trade detail for {d_esc}</h2>",
        '<p class="meta">Grouped by symbol. <strong>Touch</strong> = whether the row’s <code>entry_fill_time</code> '
        "and/or <code>exit_fill_time</code> falls on this day. "
        "<strong>E qty</strong> / <strong>X qty</strong> = Σ <code>fills</code> for entry vs exit legs. "
        "Use <strong>Review</strong> to open a panel with entry snapshot, exact decision-zone overlay when captured, and supply/demand context chart "
        "(<code>historical_data/</code> CSV; if the view date is past the file end, candles use the last day in the file).</p>",
    ]
    for sym in sorted(by_sym.keys()):
        rows = by_sym[sym]
        sym_e = html.escape(sym or "?")
        body = ""
        for t in rows:
            raw_tid = str(t.get("trade_id") or "")
            tid_disp = html.escape(raw_tid[:22] + ("…" if len(raw_tid) > 22 else ""))
            tid_title = html.escape(raw_tid)
            touch = html.escape(str(t.get("touch") or "—"))
            ep = t.get("entry_params") or {}
            xp_snap = t.get("exit_params_from_snapshot") or {}
            exit_struct = {
                "exit_fill_price": t.get("exit_fill_price"),
                "exit_fill_time": t.get("exit_fill_time"),
                "exit_fill_qty": t.get("exit_fill_qty"),
                "exit_reason": t.get("exit_reason"),
                "realized_pnl": t.get("realized_pnl"),
                "params_from_snapshot_exit_keys": xp_snap if xp_snap else None,
            }
            entry_struct = {
                "intended_entry_price": t.get("intended_entry_price"),
                "intended_shares": t.get("intended_shares"),
                "entry_fill_price": t.get("entry_fill_price"),
                "entry_fill_time": t.get("entry_fill_time"),
                "entry_fill_qty": t.get("entry_fill_qty"),
                "stop_loss": t.get("stop_loss"),
                "target": t.get("target"),
                "signal_value": t.get("signal_value"),
                "order_id": t.get("order_id"),
                "indicators": ep,
            }
            pnl = t.get("realized_pnl")
            pnl_cls = ""
            if pnl is not None:
                pnl_cls = "pnl-pos" if float(pnl) > 0 else ("pnl-neg" if float(pnl) < 0 else "")
            sym_d = (sym or "").strip()
            run_raw = t.get("run_id")
            run_id_s = (str(run_raw) if run_raw is not None else "").strip()
            d_attr = html.escape(selected_date, quote=True)
            s_attr = html.escape(sym_d, quote=True)
            r_attr = html.escape(run_id_s, quote=True) if run_id_s else ""
            tr_start = f'<tr class="trade-row" data-symbol="{s_attr}" data-day="{d_attr}"'
            if r_attr:
                tr_start += f' data-run-id="{r_attr}"'
            tr_start += f' data-trade-id="{html.escape(raw_tid, quote=True)}"'
            tr_start += ">"
            _run_data_attr = f' data-run-id="{r_attr}"' if r_attr else ""
            rev = (
                f'<a class="trade-ctx-link" href="#trade-context" '
                f'data-symbol="{s_attr}" data-day="{d_attr}"{_run_data_attr} '
                f'data-trade-id="{html.escape(str(raw_tid), quote=True)}"'
                f">Review</a>"
            )
            body += (
                tr_start
                + f"<td><strong>{touch}</strong></td>"
                + f"<td class=\"ctx-cell\">{rev}</td>"
                f"<td><code title='{tid_title}'>{tid_disp}</code></td>"
                f"<td>{html.escape(str(t.get('side') or ''))}</td>"
                f"<td>{html.escape(str(t.get('status') or ''))}</td>"
                f"<td class='num'>{_fmt_qty_cell(t.get('intended_shares'))}</td>"
                f"<td class='num'>{_fmt_qty_cell(t.get('entry_fill_qty'))}</td>"
                f"<td class='num'>{_fmt_qty_cell(t.get('exit_fill_qty'))}</td>"
                f"<td class='num'>{_fmt_money(t.get('entry_fill_price'))}</td>"
                f"<td style='font-size:0.8rem'>{html.escape(str(t.get('entry_fill_time') or '—'))}</td>"
                f"<td class='num'>{_fmt_money(t.get('exit_fill_price'))}</td>"
                f"<td style='font-size:0.8rem'>{html.escape(str(t.get('exit_fill_time') or '—'))}</td>"
                f"<td class='num {pnl_cls}'>{_fmt_money(float(pnl)) if pnl is not None else '—'}</td>"
                f"<td style='font-size:0.8rem'>{html.escape(str(t.get('exit_reason') or '—'))}</td>"
                "<td>"
                "<details><summary>Entry</summary>"
                f"{_json_pre_block(entry_struct)}"
                "</details>"
                "<details><summary>Exit</summary>"
                f"{_json_pre_block(exit_struct)}"
                "</details>"
                "</td>"
                "</tr>"
            )
        parts.append(f"<h3>{sym_e}</h3>")
        parts.append('<div class="table-wrap"><table>')
        parts.append(
            "<thead><tr>"
            "<th>Touch</th><th>Review</th><th>Trade</th><th>Side</th><th>Status</th>"
            "<th class='num'>Intend Q</th><th class='num'>E qty</th><th class='num'>X qty</th>"
            "<th class='num'>Entry px</th><th>Entry time</th><th class='num'>Exit px</th><th>Exit time</th>"
            "<th class='num'>Realized</th><th>Exit reason</th><th>Params</th>"
            "</tr></thead>"
        )
        parts.append(f"<tbody>{body}</tbody></table></div>")
    parts.append(
        f'<p class="meta">JSON: <a href="/api/trades-day?date={urllib.parse.quote(selected_date)}">'
        f"/api/trades-day?date={d_esc}</a></p>"
    )
    return "\n".join(parts)


async def _fetch_live_ib_snapshot_async(args: argparse.Namespace) -> dict[str, Any]:
    from ibkr.connection import IBKRConnection, _account_summary_tag_float

    connection = IBKRConnection(
        host=args.ib_host,
        port=args.ib_port,
        client_id=args.ib_client_id,
    )
    connected = False
    try:
        connected = await connection.connect(max_retries=1, retry_delay=0)
        if not connected:
            raise RuntimeError("IBKR connect failed")
        ib = connection.get_ib()
        accounts = ib.managedAccounts() or []
        acct = accounts[0] if accounts else ""
        account_summary = await ib.accountSummaryAsync(acct)
        pnl = await connection.get_account_pnl_ib_snapshot()
        account_values: list[Any] = []
        portfolio_items: list[Any] = []
        try:
            ib.client.reqAccountUpdates(True, acct)
            await asyncio.sleep(max(0.0, float(args.live_ib_account_wait)))
            try:
                account_values = list(ib.accountValues(acct))
            except TypeError:
                account_values = list(ib.accountValues())
            try:
                portfolio_items = list(ib.portfolio(acct))
            except TypeError:
                portfolio_items = list(ib.portfolio())
        finally:
            try:
                ib.client.reqAccountUpdates(False, acct)
            except Exception:
                pass
        positions = _portfolio_items_to_positions(portfolio_items)
        if not positions:
            positions = await connection.get_current_positions()
        margin = await connection.get_account_margin_gate_snapshot()
        net_liq = _safe_float(margin.get("net_liquidation"))
        if net_liq is None:
            net_liq = _account_summary_tag_float(account_summary, "NetLiquidation")
        if net_liq is None:
            net_liq = _safe_float(await connection.get_account_equity())
        # The IBKR app's Account Window "Realized P&L" / "Unrealized P&L" aligns
        # with account summary tags more reliably than reqPnL.realizedPnL after a
        # fresh dashboard-only API connection.
        values_realized = _account_summary_tag_float(account_values, "RealizedPnL")
        summary_realized = _account_summary_tag_float(account_summary, "RealizedPnL")
        values_unrealized = _account_summary_tag_float(account_values, "UnrealizedPnL")
        summary_unrealized = _account_summary_tag_float(account_summary, "UnrealizedPnL")
        portfolio_unrealized = None
        pnl_values = [
            _safe_float(getattr(item, "unrealizedPNL", None))
            for item in portfolio_items
            if _safe_float(getattr(item, "unrealizedPNL", None)) is not None
        ]
        if pnl_values:
            portfolio_unrealized = sum(pnl_values)
        realized_pnl = values_realized
        if realized_pnl is None:
            realized_pnl = summary_realized
        if realized_pnl is None:
            realized_pnl = _safe_float(pnl.get("realized_pnl"))
        unrealized_pnl = values_unrealized
        if unrealized_pnl is None:
            unrealized_pnl = summary_unrealized
        if unrealized_pnl is None:
            unrealized_pnl = portfolio_unrealized
        if unrealized_pnl is None:
            unrealized_pnl = _safe_float(pnl.get("unrealized_pnl"))
        now = datetime.now(ET)
        return {
            "snapshot_id": None,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.isoformat(),
            "net_liquidation": net_liq,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl_today": realized_pnl,
            "ib_daily_pnl": _safe_float(pnl.get("daily_pnl")),
            "positions": positions,
            "snapshot_source": "live_ib",
            "pnl_source": {
                "realized_pnl_today": (
                    "accountValues.RealizedPnL"
                    if values_realized is not None
                    else ("accountSummary.RealizedPnL" if summary_realized is not None else "reqPnL.realizedPnL")
                ),
                "unrealized_pnl": (
                    "accountValues.UnrealizedPnL"
                    if values_unrealized is not None
                    else (
                        "accountSummary.UnrealizedPnL"
                        if summary_unrealized is not None
                        else ("portfolio.unrealizedPNL" if portfolio_unrealized is not None else "reqPnL.unrealizedPnL")
                    )
                ),
                "ib_daily_pnl": "reqPnL.dailyPnL",
            },
        }
    finally:
        if connected:
            await connection.disconnect()


def _fetch_live_ib_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(
        asyncio.wait_for(
            _fetch_live_ib_snapshot_async(args),
            timeout=max(1.0, float(args.live_ib_timeout)),
        )
    )


def _portfolio_items_to_positions(items: list[Any]) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    for item in items:
        pos = _safe_float(getattr(item, "position", None))
        if pos is None or abs(pos) < 0.001:
            continue
        contract = getattr(item, "contract", None)
        symbol = getattr(contract, "symbol", "UNKNOWN").upper() if contract else "UNKNOWN"
        positions.append(
            {
                "symbol": symbol,
                "secType": getattr(contract, "secType", "STK") if contract else "STK",
                "position": pos,
                "avgCost": _safe_float(getattr(item, "averageCost", None)),
                "unrealizedPNL": _safe_float(getattr(item, "unrealizedPNL", None)),
                "realizedPNL": _safe_float(getattr(item, "realizedPNL", None)),
                "marketValue": _safe_float(getattr(item, "marketValue", None)),
                "marketPrice": _safe_float(getattr(item, "marketPrice", None)),
            }
        )
    return positions


def _html_shell(
    snap: dict | None,
    recent: list,
    activity: list[dict],
    available_dates: list[str],
    db_path: str,
    selected_date: str | None = None,
    day_exit: dict | None = None,
    trades_detail_html: str = "",
) -> str:
    u = _fmt_money(snap.get("unrealized_pnl") if snap else None)
    r = _fmt_money(snap.get("realized_pnl_today") if snap else None)
    d = _fmt_money(snap.get("ib_daily_pnl") if snap else None)
    nl = _fmt_money(snap.get("net_liquidation") if snap else None)
    ts = (snap or {}).get("time") or "—"
    sid = (snap or {}).get("snapshot_id")
    sid_s = f" (id={sid})" if sid is not None else ""
    is_live = bool(snap and snap.get("snapshot_source") == "live_ib")
    live_error = (snap or {}).get("live_ib_error")
    snapshot_label = "Live IBKR as of" if is_live else "Latest stored snapshot"
    source_note = (
        '<p class="meta"><strong>Source:</strong> Live IBKR Gateway/API. '
        "Cards and positions are pulled on page refresh; Realized/Unrealized prefer IB account-summary "
        "Account Window tags, Daily total uses IB reqPnL. Journal tables below still come from SQLite.</p>"
        if is_live
        else '<p class="meta"><strong>Source:</strong> Stored <code>daily_snapshots</code> row. '
        + (
            f"Live IBKR fetch failed: <code>{html.escape(str(live_error))}</code>. "
            if live_error
            else ""
        )
        + "Start Gateway / use a free client id to refresh cards and positions live.</p>"
    )
    r_lbl = "Realized today (IB)"
    d_lbl = "Daily total (IB)"
    if snap and snap.get("realized_pnl_today_src") == "journal":
        r_lbl = "Realized today (journal)"
    if snap and snap.get("ib_daily_pnl_src") == "journal":
        d_lbl = "Daily total (journal)"
    pnl_note = ""
    if snap and (
        snap.get("realized_pnl_today_src") == "journal"
        or snap.get("ib_daily_pnl_src") == "journal"
    ):
        pnl_note = (
            '<p class="meta">Realized / daily use sums of closed <code>trades</code> on this calendar day '
            "when IB P&amp;L was not stored. Unrealized still requires IB (or a new snapshot after the fix).</p>"
        )

    if selected_date:
        ib_vs_journal = (
            '<p class="meta"><strong>IB vs this table</strong> — Top cards use '
            + ("<strong>live IBKR</strong>" if is_live else "the <strong>last stored</strong> <code>daily_snapshots</code> row")
            + f" for calendar date <strong>{selected_date}</strong>. IB account-level <code>reqPnL</code> is "
            "whole-account / all products. Σ realized and Closed counts use only <code>trades</code> rows "
            "marked <code>closed</code> with <code>exit_fill_time</code> on that day. "
            "They diverge if IB realized came from activity not journaled here, or if closes never updated "
            "<code>trades</code>.</p>"
        )
    else:
        ib_vs_journal = (
            '<p class="meta"><strong>IB vs this table</strong> — The cards use '
            + ("<strong>live IBKR</strong>" if is_live else "the latest <code>daily_snapshots</code> row")
            + ": IB account-level <code>reqPnL</code> (whole account, all products). "
            "Σ realized and Closed counts use only <code>trades</code> rows your process marked "
            "<code>closed</code> with an <code>exit_fill_time</code> on the selected day. "
            "They diverge if IB realized came from activity not journaled here, or if closes never updated "
            "<code>trades</code> (many <code>open</code> rows usually mean duplicate or unstitched lifecycle).</p>"
        )
    day_exit_block = ""
    if day_exit:
        nc = int(day_exit.get("closed_trade_rows") or 0)
        sr = _fmt_money(float(day_exit.get("sum_realized_pnl") or 0.0))
        on = int(day_exit.get("open_trade_rows_total") or 0)
        dday = day_exit.get("date") or ""
        day_exit_block = (
            f'<p class="meta"><strong>Journal totals for calendar exit day {dday}</strong> — '
            f"{nc} closed <code>trades</code> row(s) with Σ realized {sr}; "
            f"rows still <code>open</code> in the DB (all symbols): {on}. "
            "If the first number is 0 but IB realized is not, that day had no closed journal exits matching the date filter.</p>"
        )

    # Date selector options
    date_options = ""
    current_label = selected_date or "All Time (latest)"
    for dt in available_dates:
        selected = ' selected' if dt == selected_date else ''
        date_options += f'<option value="{dt}"{selected}>{dt}</option>'

    rows_html = ""
    for row in recent[:80]:
        rows_html += (
            f"<tr><td>{row.get('time', '')}</td>"
            f"<td class='num'>{_fmt_money(row.get('net_liquidation'))}</td>"
            f"<td class='num'>{_fmt_money(row.get('unrealized_pnl'))}</td>"
            f"<td class='num'>{_fmt_money(row.get('realized_pnl_today'))}</td>"
            f"<td class='num'>{_fmt_money(row.get('ib_daily_pnl'))}</td></tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='5'>No snapshots yet — run watch mode with journaling.</td></tr>"

    act_rows = ""
    tot_open = tot_closed = tot_w = tot_l = tot_f = 0
    tot_open_long = tot_open_short = tot_closed_long = tot_closed_short = 0
    tot_opened = tot_exited = 0
    tot_pnl = 0.0
    date_title = f" for <strong>{selected_date}</strong>" if selected_date else " (all-time / cumulative)"
    entry_exit_note = (
        '<p class="meta"><strong>Opened / Exited</strong> — With a date selected, <strong>Opened</strong> counts '
        "<code>trades</code> whose <code>entry_fill_time</code> is on that day; <strong>Exited</strong> counts "
        "rows closed that day (same as <strong>Closed</strong> under the filter). All-time: "
        "<strong>Opened</strong> = total rows per symbol; <strong>Exited</strong> = lifetime closed count.</p>"
    )
    for a in activity:
        sym = a.get("symbol", "")
        o = int(a.get("open") or 0)
        ol = int(a.get("open_long") or 0)
        os_ = int(a.get("open_short") or 0)
        opn = int(a.get("opened") or 0)
        ex = int(a.get("exited") or 0)
        c = int(a.get("closed") or 0)
        cl = int(a.get("closed_long") or 0)
        cs = int(a.get("closed_short") or 0)
        w = int(a.get("wins") or 0)
        l = int(a.get("losses") or 0)
        f = int(a.get("flat") or 0)
        pnl = float(a.get("total_realized_pnl") or 0.0)
        tot_open += o
        tot_open_long += ol
        tot_open_short += os_
        tot_opened += opn
        tot_exited += ex
        tot_closed += c
        tot_closed_long += cl
        tot_closed_short += cs
        tot_w += w
        tot_l += l
        tot_f += f
        tot_pnl += pnl
        pnl_cls = "pnl-pos" if pnl > 0 else ("pnl-neg" if pnl < 0 else "")
        act_rows += (
            f"<tr><td><strong>{sym}</strong></td>"
            f"<td class='num'>{o}</td><td class='num'>{ol}</td><td class='num'>{os_}</td>"
            f"<td class='num'>{opn}</td><td class='num'>{ex}</td>"
            f"<td class='num'>{c}</td><td class='num'>{cl}</td><td class='num'>{cs}</td>"
            f"<td class='num win'>{w}</td><td class='num loss'>{l}</td><td class='num'>{f}</td>"
            f"<td class='num {pnl_cls}'>{_fmt_money(pnl)}</td></tr>"
        )
    side_summary = (
        f"<p class=\"meta\"><strong>Long vs short (this view)</strong> — "
        f"Open: LONG {tot_open_long}, SHORT {tot_open_short} &nbsp;|&nbsp; "
        f"Closed: LONG {tot_closed_long}, SHORT {tot_closed_short}</p>"
    )
    if not act_rows:
        act_rows = (
            "<tr><td colspan='13'>No trades in journal yet — closed trades get win/loss counts here.</td></tr>"
        )
        side_summary = ""
        entry_exit_note = ""
    else:
        tp_cls = "pnl-pos" if tot_pnl > 0 else ("pnl-neg" if tot_pnl < 0 else "")
        act_rows += (
            f"<tr class='totals'><td><strong>All symbols</strong></td>"
            f"<td class='num'>{tot_open}</td><td class='num'>{tot_open_long}</td><td class='num'>{tot_open_short}</td>"
            f"<td class='num'>{tot_opened}</td><td class='num'>{tot_exited}</td>"
            f"<td class='num'>{tot_closed}</td><td class='num'>{tot_closed_long}</td><td class='num'>{tot_closed_short}</td>"
            f"<td class='num win'>{tot_w}</td><td class='num loss'>{tot_l}</td><td class='num'>{tot_f}</td>"
            f"<td class='num {tp_cls}'>{_fmt_money(tot_pnl)}</td></tr>"
        )

    # Live IBKR Positions panel (from snapshot.positions or latest snapshot) - matches logs
    positions_html = ""
    positions = (snap or {}).get("positions", [])
    if positions:
        pos_rows = ""
        for p in positions:
            sym = html.escape(p.get("symbol", ""))
            qty = p.get("qty") or p.get("position", 0)
            is_short = qty < 0
            side = "SHORT" if is_short else "LONG"
            side_cls = "loss" if is_short else "win"
            pos_rows += (
                f"<tr><td><strong>{sym}</strong></td>"
                f"<td class='num {side_cls}'>{side}</td>"
                f"<td class='num'>{abs(int(qty))}</td>"
                f"<td class='num'>{_fmt_money(p.get('avgCost'))}</td>"
                f"<td class='num'>{_fmt_money(p.get('unrealizedPNL'))}</td></tr>"
            )
        positions_html = f'''
  <h2>{"Live IBKR Positions" if is_live else "IBKR Positions (from stored snapshot)"}</h2>
  <p class="meta">{"Pulled directly from IBKR <code>reqPositionsAsync()</code> on this page refresh." if is_live else "Loaded from the latest stored snapshot written by watch mode."} Positive qty = LONG, negative = SHORT. Journal "open" rows may include stale/historical from previous runs.</p>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Avg Cost</th><th>Unrealized PnL</th></tr></thead>
      <tbody>{pos_rows}</tbody>
    </table>
  </div>
'''
    else:
        positions_html = '<p class="meta">No IBKR positions available from this refresh/snapshot.</p>'

    _api_example_day = "2026-04-17"
    api_trades_quick = (
        f'<p class="meta"><strong>Per-trade JSON</strong> (same host/port as this page): '
        f'<a href="/api/trades-day?date={_api_example_day}"><code>/api/trades-day?date={_api_example_day}</code></a>'
    )
    if selected_date and selected_date != _api_example_day:
        api_trades_quick += (
            f' · <a href="/api/trades-day?date={html.escape(selected_date, quote=True)}">'
            f"<code>/api/trades-day?date={html.escape(selected_date)}</code></a> (current view)"
        )
    api_trades_quick += "</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Journal · IB P&amp;L</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 1.5rem; max-width: 960px; }}
    h1 {{ font-size: 1.25rem; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: 1rem 0; }}
    .card {{ border: 1px solid #ccc; border-radius: 8px; padding: 0.75rem 1rem; min-width: 10rem; }}
    .label {{ color: #555; font-size: 0.8rem; }}
    .value {{ font-size: 1.35rem; font-variant-numeric: tabular-nums; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 0.9rem; }}
    th, td {{ border-bottom: 1px solid #eee; padding: 0.4rem 0.5rem; text-align: left; }}
    th {{ background: #f6f6f6; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    td.win {{ color: #0a6; }}
    td.loss {{ color: #a30; }}
    .pnl-pos {{ color: #0a6; }}
    .pnl-neg {{ color: #a30; }}
    tr.totals {{ font-weight: 600; border-top: 2px solid #ddd; }}
    .table-wrap {{ overflow-x: auto; margin: 0.5rem 0; }}
    .meta {{ color: #666; font-size: 0.85rem; margin-top: 1rem; }}
    code {{ background: #f0f0f0; padding: 0.1rem 0.35rem; border-radius: 4px; }}
    select {{ padding: 0.5rem; font-size: 1rem; margin: 0.5rem 0; }}
    .date-header {{ display: flex; align-items: center; gap: 1rem; }}
    .json-block {{ font-size: 0.72rem; white-space: pre-wrap; word-break: break-word; margin: 0.35rem 0; max-height: 14rem; overflow: auto; background: #fafafa; border: 1px solid #eee; padding: 0.4rem 0.5rem; border-radius: 4px; }}
    details {{ margin: 0.2rem 0; }}
    a.trade-ctx-link {{ font-size: 0.85rem; white-space: nowrap; }}
    td.ctx-cell {{ white-space: nowrap; }}
    #zone-hover-panel {{
      position: fixed; right: 0.5rem; bottom: 0.5rem; z-index: 10000;
      width: min(96vw, 920px); height: min(78vh, 720px);
      background: #fff; border: 1px solid #ccc; border-radius: 8px;
      box-shadow: 0 6px 32px rgba(0,0,0,0.16);
      display: none; flex-direction: column; max-width: 100%;
    }}
    #zone-hover-panel.visible {{ display: flex; }}
    .zone-hover-head {{ display: flex; justify-content: space-between; align-items: center;
      padding: 0.35rem 0.6rem; border-bottom: 1px solid #e5e5e5; font-size: 0.8rem; background: #fafafa; border-radius: 8px 8px 0 0; flex-shrink: 0; }}
    .zone-hover-head button {{ background: none; border: none; font-size: 1.2rem; line-height: 1; cursor: pointer; padding: 0 0.2rem; color: #555; }}
    #zone-hover-plot {{ flex: 1; min-height: 200px; }}
    #zone-hover-decision {{
      flex: 0 0 auto; max-height: 32%;
      margin: 0 0.5rem 0.4rem; padding: 0.35rem 0.5rem; font-size: 0.75rem; line-height: 1.35;
      overflow: auto; background: #fafbfc; border: 1px solid #e8e8e8; border-radius: 4px;
      color: #222; font-family: ui-sans-serif, system-ui, sans-serif;
    }}
    #zone-hover-decision .zh-line {{ margin: 0.12rem 0; }}
    #zone-hover-decision .zh-pre {{ margin: 0.25rem 0 0 0; white-space: pre-wrap; word-break: break-word; font-size: 0.7rem; max-height: 10rem; overflow: auto; font-family: ui-monospace, Menlo, monospace; }}
  </style>
  <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
</head>
<body>
  <h1>IBKR P&amp;L</h1>
  <div class="date-header">
    <label for="date-select">View metrics for date:</label>
    <select id="date-select" onchange="window.location = '?date=' + this.value">
      <option value="">All Time (latest snapshot)</option>
      {date_options}
    </select>
  </div>
  {api_trades_quick}
  <p>Showing data{date_title}. {snapshot_label}: <strong>{ts}</strong>{sid_s}</p>
  <div class="cards">
    <div class="card"><div class="label">Net liquidation</div><div class="value">{nl}</div></div>
    <div class="card"><div class="label">Unrealized (IB)</div><div class="value">{u}</div></div>
    <div class="card"><div class="label">{r_lbl}</div><div class="value">{r}</div></div>
    <div class="card"><div class="label">{d_lbl}</div><div class="value">{d}</div></div>
  </div>
  {source_note}
  {pnl_note}
  {positions_html}
  {ib_vs_journal}
  {day_exit_block}
  <h2>Activity by symbol{date_title}</h2>
  <p class="meta">From <code>trades</code>: open = still open; closed = exited on this day (if filtered). Win/loss/flat use realized P&amp;L on closed rows. Open L/S and Cls L/S split rows by <code>side</code>.</p>
  {entry_exit_note}
  {side_summary}
  <div class="table-wrap">
  <table>
    <thead><tr><th>Symbol</th><th>Open</th><th>Open L</th><th>Open S</th><th>Opened</th><th>Exited</th><th>Closed</th><th>Cls L</th><th>Cls S</th><th>Wins</th><th>Losses</th><th>Flat</th><th>Σ realized</th></tr></thead>
    <tbody>{act_rows}</tbody>
  </table>
  </div>
  {trades_detail_html}
  <h2>Recent snapshots</h2>
  <table>
    <thead><tr><th>Time</th><th>Net Liq</th><th>Unrealized</th><th>Realized today</th><th>Daily (IB)</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p class="meta">DB: <code>{db_path}</code> · JSON:
  <a href="/api/latest">/api/latest</a> (optional <code>?date=YYYY-MM-DD</code>) · <a href="/api/activity">/api/activity</a> · <a href="/api/trades-day">/api/trades-day</a> (requires <code>?date=</code>) · <a href="/api/dates">/api/dates</a> · <code>/api/snapshot/&lt;id&gt;</code>
  · <code>/api/trade?trade_id=…</code> (full entry/exit snapshot, no truncation) ·
  <code>/api/trade-zone-chart?symbol=…&amp;day=…</code> (optional <code>run_id</code>, <code>bar_size</code>)
  · Auto-refresh every 60s (latest only).</p>
  <div id="zone-hover-panel" aria-live="polite" aria-label="Trade context and zone chart">
    <div class="zone-hover-head">
      <span id="zone-hover-title">Trade + zones</span>
      <button type="button" id="zone-hover-close" title="Close">×</button>
    </div>
    <p id="zone-hover-status" class="meta" style="margin:0.3rem 0.6rem; font-size:0.75rem; color:#555;"></p>
    <div id="zone-hover-plot" aria-label="Candles: last available day in cache if the view date is missing. Entry/exit markers from journal."></div>
    <div id="zone-hover-decision"></div>
  </div>
  <script>
    (function() {{
      var panel = document.getElementById('zone-hover-panel');
      var plotEl = document.getElementById('zone-hover-plot');
      var statusEl = document.getElementById('zone-hover-status');
      var decisionEl = document.getElementById('zone-hover-decision');
      var titleEl = document.getElementById('zone-hover-title');
      var closeBtn = document.getElementById('zone-hover-close');
      var ac = null;
      var loadSeq = 0;
      var chartTimeoutId = null;
      var chartTimedOut = false;
      var CHART_FETCH_TIMEOUT_MS = 600000;
      var cache = {{}};
      function clearChartTimeout() {{
        if (chartTimeoutId) {{ clearTimeout(chartTimeoutId); chartTimeoutId = null; }}
        chartTimedOut = false;
      }}
      function showP() {{
        if (!panel) return;
        panel.classList.add('visible');
        panel.style.display = 'flex';
      }}
      function hideP() {{
        if (!panel) return;
        clearChartTimeout();
        loadSeq++;
        if (ac) try {{ ac.abort(); }} catch (e) {{}}
        ac = null;
        panel.classList.remove('visible');
        panel.style.display = 'none';
        if (statusEl) statusEl.textContent = '';
        if (decisionEl) decisionEl.innerHTML = '';
        if (plotEl) try {{ Plotly.purge(plotEl); }} catch (e) {{}}
      }}
      function keyFor(tr) {{
        if (!tr || !tr.dataset) return '';
        return (tr.dataset.symbol || '') + '|' + (tr.dataset.day || '') + '|' + (tr.dataset.runId || '');
      }}
      function loadChart(el) {{
        if (!el || !el.dataset || !el.dataset.symbol || !el.dataset.day) return;
        if (typeof Plotly === 'undefined') {{
          if (statusEl) statusEl.textContent = 'Plotly failed to load (network?)';
          if (decisionEl) decisionEl.textContent = '';
          showP();
          return;
        }}
        var mySeq = ++loadSeq;
        clearChartTimeout();
        var tr = el;
        var k = keyFor(tr);
        var tid = tr.dataset.tradeId;
        if (titleEl) titleEl.textContent = (tr.dataset.symbol || '') + ' · ' + (tr.dataset.day || '');
        showP();
        if (statusEl) statusEl.textContent = 'Loading snapshot…';
        if (decisionEl) decisionEl.innerHTML = '';
        if (plotEl) try {{ Plotly.purge(plotEl); }} catch (e) {{}}
        if (ac) try {{ ac.abort(); }} catch (e) {{}}
        ac = new AbortController();
        var sig = ac.signal;
        var p = new URLSearchParams();
        p.set('symbol', tr.dataset.symbol);
        p.set('day', tr.dataset.day);
        if (tr.dataset.runId) p.set('run_id', tr.dataset.runId);
        function apiMsg(j, r) {{
          if (!j || typeof j !== 'object') return (r && r.status ? r.status + ' ' + r.statusText : 'Error');
          if (j.error != null) return (typeof j.error === 'string') ? j.error : JSON.stringify(j.error);
          if (j.detail != null) {{
            if (typeof j.detail === 'string') return j.detail;
            if (Array.isArray(j.detail)) return j.detail.map(function(d) {{ return d.msg != null ? d.msg : String(d); }}).join(' ');
            return JSON.stringify(j.detail);
          }}
          return (r && r.status ? r.status + ' ' + r.statusText : 'Error');
        }}
        function parseRes(r) {{
          return r.text().then(function(t) {{
            var j = {{}};
            if (t) try {{ j = JSON.parse(t); }} catch (e) {{ j = {{ error: t || r.statusText }}; }}
            return {{ r: r, j: j, raw: t }};
          }});
        }}
        function escHtml(s) {{
          if (s == null) return '';
          return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }}
        var chartUrl = new URL('/api/trade-zone-chart', window.location.origin);
        chartUrl.search = p.toString();
        function getChart() {{
          if (cache[k]) {{
            return Promise.resolve({{ r: {{ ok: true, status: 200 }}, j: {{ figure: cache[k], meta: {{}} }}, raw: '' }});
          }}
          return fetch(chartUrl, {{ signal: sig, credentials: 'same-origin' }}).then(parseRes);
        }}
        var pTrade;
        if (tid) {{
          var tu = new URL('/api/trade', window.location.origin);
          tu.searchParams.set('trade_id', tid);
          pTrade = fetch(tu, {{ signal: sig, credentials: 'same-origin' }}).then(parseRes);
        }} else {{
          pTrade = Promise.resolve({{ r: {{ ok: true, status: 200 }}, j: null, raw: '' }});
        }}
        pTrade
          .catch(function(e) {{
            if (e && e.name === 'AbortError') return Promise.reject(e);
            if (mySeq !== loadSeq) return Promise.reject(e);
            return {{ r: {{ ok: false, status: 0 }}, j: {{ error: (e && e.message) || String(e) }} }};
          }})
          .then(function(tR) {{
            if (mySeq !== loadSeq) return;
            if (decisionEl) {{
              if (!tid) {{
                decisionEl.textContent = 'This row has no data-trade-id (use per-trade view with a date selected).';
              }} else if (!tR || !tR.r.ok) {{
                var errTxt = tR ? apiMsg(tR.j, tR.r) : 'Failed to load trade';
                decisionEl.textContent = errTxt;
              }} else if (tR.j) {{
                var o = tR.j;
                var h = [];
                h.push('<div class="zh-line"><strong>At decision</strong> signal=' + escHtml(o.signal_value) + ' side=' + escHtml(o.side) + ' status=' + escHtml(o.status) + '</div>');
                h.push('<div class="zh-line">risk intended ' + escHtml(o.intended_shares) + ' @ ' + escHtml(o.intended_entry_price) + ' · stop ' + escHtml(o.stop_loss != null ? o.stop_loss : '—') + ' · target ' + escHtml(o.target != null ? o.target : '—') + '</div>');
                var dz = o.entry_snapshot && o.entry_snapshot.decision_zone;
                if (dz) {{
                  var dzLine = 'decision zone: ' + escHtml(dz.decision_rule || dz.decision_zone_type || 'zone')
                    + ' [' + escHtml(dz.decision_zone_low) + ' - ' + escHtml(dz.decision_zone_high) + ']';
                  if (dz.decision_distance_abs != null) dzLine += ' · distance ' + escHtml(dz.decision_distance_abs);
                  if (dz.decision_distance_pct != null) dzLine += ' (' + escHtml(dz.decision_distance_pct) + '%)';
                  h.push('<div class="zh-line"><strong>' + dzLine + '</strong></div>');
                }} else {{
                  h.push('<div class="zh-line">decision zone: not captured for this historical trade; chart uses reconstructed context zones.</div>');
                }}
                if (o.entry_fill_time) h.push('<div class="zh-line">fills entry: ' + escHtml(o.entry_fill_time) + (o.entry_fill_price != null ? (' @ ' + escHtml(o.entry_fill_price)) : '') + '</div>');
                if (o.exit_fill_time) h.push('<div class="zh-line">fills exit: ' + escHtml(o.exit_fill_time) + (o.exit_fill_price != null ? (' @ ' + escHtml(o.exit_fill_price)) : '') + '</div>');
                h.push('<details><summary style="cursor:pointer; font-size:0.8rem; color:#444">Indicator snapshot (JSON from DB)</summary>');
                h.push('<pre class="zh-pre">' + escHtml(JSON.stringify(o.entry_snapshot || {{}}, null, 2)) + '</pre>');
                if (o.exit_snapshot && Object.keys(o.exit_snapshot).length) {{
                  h.push('<div class="zh-line">exit_snapshot</div><pre class="zh-pre">' + escHtml(JSON.stringify(o.exit_snapshot, null, 2)) + '</pre>');
                }}
                if (o.fills && o.fills.length) h.push('<div class="zh-line">fills</div><pre class="zh-pre">' + escHtml(JSON.stringify(o.fills, null, 2)) + '</pre>');
                if (o.memory_consulted && o.memory_verdict) {{
                  h.push('<div class="zh-line">memory_verdict</div><pre class="zh-pre">' + escHtml(JSON.stringify(o.memory_verdict, null, 2)) + '</pre>');
                }}
                h.push('</details>');
                decisionEl.innerHTML = h.join('');
              }}
            }}
            if (mySeq !== loadSeq) return;
            if (statusEl) statusEl.textContent = 'Loading zone chart… (see [journal-api] in server terminal; large files are trimmed server-side after 45k rows)';
            clearChartTimeout();
            chartTimedOut = false;
            var acSnap = ac;
            chartTimeoutId = setTimeout(function() {{
              if (mySeq !== loadSeq) return;
              chartTimedOut = true;
              try {{ if (acSnap === ac && ac) ac.abort(); }} catch (err) {{}}
            }}, CHART_FETCH_TIMEOUT_MS);
            return getChart();
          }})
          .then(function(cR) {{
            if (mySeq !== loadSeq) return;
            if (cR === undefined) return;
            clearChartTimeout();
            if (!cR.r || !cR.r.ok) throw new Error(cR ? apiMsg(cR.j, cR.r) : 'Chart: no response');
            var fig = cR.j.figure;
            if (!fig || !fig.data) throw new Error('Invalid chart: ' + (cR.raw && cR.raw.slice ? cR.raw.slice(0, 160) : ''));
            if (!cache[k]) cache[k] = fig;
            if (cR.j.meta && statusEl) {{
              var m0 = cR.j.meta;
              var ex = (m0.supply_zones != null && m0.demand_zones != null) ? (m0.supply_zones + ' supply, ' + m0.demand_zones + ' demand') : '';
              var c0 = m0.csv ? String(m0.csv) : '';
              var li0 = c0.lastIndexOf('/'); var lb0 = c0.lastIndexOf('\\\\');
              var lo0 = (li0 > lb0) ? li0 : ((lb0 > li0) ? lb0 : -1);
              var cap0 = (lo0 >= 0) ? c0.substring(lo0 + 1) : c0;
              var parts = ['Chart: ' + ((ex && cap0) ? (ex + ' · ' + cap0) : (ex || cap0 || 'from cache'))];
              if (m0.plot_session_day && tr.dataset.day && m0.plot_session_day !== tr.dataset.day) {{
                parts.push('candles ' + m0.plot_session_day + ' (view ' + tr.dataset.day + ' has no bars in file yet)');
              }}
              if (m0.day_note) parts.push(m0.day_note);
              if (m0.ohlcv_note) parts.push(m0.ohlcv_note);
              if (m0.zone_plot_note) parts.push(m0.zone_plot_note);
              if (m0.decision_zone_note) parts.push(m0.decision_zone_note);
              if (m0.fill_marker_note) parts.push(m0.fill_marker_note);
              statusEl.textContent = parts.join(' — ');
            }} else if (statusEl) statusEl.textContent = 'Session from IB cache; entry/exit markers from journal when times are set.';
            if (mySeq !== loadSeq) return;
            return Plotly.newPlot('zone-hover-plot', fig.data, fig.layout, {{ responsive: true, displayModeBar: true }});
          }})
          .catch(function(e) {{
            if (mySeq !== loadSeq) return;
            var wasChartTimeout = chartTimedOut;
            clearChartTimeout();
            if (e && e.name === 'AbortError') {{
              if (statusEl) statusEl.textContent = wasChartTimeout
                ? 'Zone chart request timed out after 10m. See terminal: [journal-api] GET /api/trade-zone-chart. Prefer 15m cache; avoid multi-year 5s unless needed.'
                : 'Request cancelled (closed panel or opened another trade).';
              return;
            }}
            var s = (e && e.message) ? e.message : String(e);
            if (e && e.name === 'TypeError' && /Failed to fetch|Load failed|NetworkError|fetch/i.test(s)) {{
              s = s + ' — use the host/port printed when starting the journal server.';
            }}
            if (statusEl) statusEl.textContent = s;
          }});
      }}
      function onReady() {{
        document.addEventListener('click', function(ev) {{
          var a = ev.target && ev.target.closest('a.trade-ctx-link');
          if (!a) return;
          ev.preventDefault();
          loadChart(a);
        }});
        if (closeBtn) closeBtn.addEventListener('click', function() {{ hideP(); }});
        document.addEventListener('keydown', function(ev) {{
          if (ev.key === 'Escape' && panel && panel.classList.contains('visible')) hideP();
        }});
      }}
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', onReady);
      else onReady();
    }})();
    // Pre-select current value if URL has ?date= param
    const urlParams = new URLSearchParams(window.location.search);
    const urlDate = urlParams.get('date');
    if (urlDate) {{
      const select = document.getElementById('date-select');
      if (select) select.value = urlDate;
    }}
    (function schedulePageRefresh() {{
      setTimeout(function doRefresh() {{
        if (urlDate) return;
        var p = document.getElementById('zone-hover-panel');
        if (p && p.classList.contains('visible')) {{
          setTimeout(doRefresh, 20000);
          return;
        }}
        location.reload();
      }}, 60000);
    }})();
  </script>
</body>
</html>"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Journal dashboard: IB snapshots + per-symbol trade activity.")
    ap.add_argument(
        "--db",
        type=Path,
        default=_PYTHONCLIENT_ROOT / "trade_journal.db",
        help="Path to trade_journal.db",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument(
        "--port",
        type=int,
        default=8765,
        metavar="N",
        help="Listen port (default 8765). If busy, tries N+1… up to 30 times unless N=0 (OS picks a port).",
    )
    ap.add_argument(
        "--purge-before-date",
        metavar="YYYY-MM-DD",
        help="Destructive: delete journal data strictly before this calendar day (runs, trades, snapshots, …), then exit.",
    )
    ap.add_argument(
        "--purge-strict",
        action="store_true",
        help="When used with --purge-before-date, also purge trades by their own timestamps (entry/exit/created_at) and clean orphan rows.",
    )
    ap.add_argument(
        "--no-live-ib",
        action="store_true",
        help="Disable live IBKR cards/positions; show only stored daily_snapshots.",
    )
    ap.add_argument("--ib-host", default="127.0.0.1", help="IBKR Gateway/TWS host for live dashboard cards.")
    ap.add_argument("--ib-port", type=int, default=4002, help="IBKR paper port for live dashboard cards.")
    ap.add_argument("--ib-client-id", type=int, default=98, help="Client id used by the dashboard live IBKR connection.")
    ap.add_argument(
        "--live-ib-timeout",
        type=float,
        default=12.0,
        help="Max seconds to spend fetching live IBKR cards/positions before falling back to DB.",
    )
    ap.add_argument(
        "--live-ib-cache-sec",
        type=float,
        default=20.0,
        help="Seconds to cache one successful/failed live IBKR fetch between page/API requests.",
    )
    ap.add_argument(
        "--live-ib-account-wait",
        type=float,
        default=1.5,
        help="Seconds to wait for IB account update/portfolio P&L after subscribing.",
    )
    args = ap.parse_args()
    db_path = args.db.resolve()
    if args.purge_before_date:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.purge_before_date):
            raise SystemExit("--purge-before-date must be YYYY-MM-DD")
        journal = TradeJournal(db_path)
        stats = journal.purge_journal_before_calendar_date(
            args.purge_before_date,
            strict_trade_time=bool(args.purge_strict),
        )
        print(json.dumps(stats, indent=2))
        print(f"Purge complete (kept {args.purge_before_date} onward). DB: {db_path}")
        return
    journal = TradeJournal(db_path)
    live_lock = threading.Lock()
    live_cache: dict[str, Any] = {"at": 0.0, "snapshot": None, "error": None}

    def _should_use_live_ib(day: str | None) -> bool:
        return not args.no_live_ib and (day is None or day == _today_et())

    def _snapshot_with_live_ib(stored: dict[str, Any] | None, day: str | None) -> dict[str, Any] | None:
        if not _should_use_live_ib(day):
            return stored
        now = time.monotonic()
        with live_lock:
            cache_age = now - float(live_cache.get("at") or 0.0)
            if cache_age > max(0.0, float(args.live_ib_cache_sec)):
                try:
                    live_cache["snapshot"] = _fetch_live_ib_snapshot(args)
                    live_cache["error"] = None
                except Exception as e:
                    live_cache["snapshot"] = None
                    live_cache["error"] = f"{type(e).__name__}: {e}"
                live_cache["at"] = time.monotonic()
            live_snap = live_cache.get("snapshot")
            live_error = live_cache.get("error")
        if isinstance(live_snap, dict):
            return live_snap
        if stored is None:
            if live_error:
                return {"time": "—", "positions": [], "live_ib_error": live_error}
            return None
        if live_error:
            fallback = dict(stored)
            fallback["live_ib_error"] = live_error
            return fallback
        return stored

    # Load ``zone_validation`` + ``ib_insync`` on the **main** thread only. ``ThreadingHTTPServer``
    # runs ``do_GET`` in worker threads; a lazy import there triggers
    # ``eventkit`` / ``get_event_loop()`` in that thread and raises on Python 3.10+.
    zone_render: Callable[..., Any] | None = None
    zone_err_cls: type[BaseException] | None = None
    zone_load_error: str | None = None
    try:
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())
        from zone_validation.service import ZoneChartError, render_zone_trade_chart

        zone_render = render_zone_trade_chart
        zone_err_cls = ZoneChartError
    except Exception as e:
        zone_load_error = f"{type(e).__name__}: {e}"
        print(f"Note: /api/trade-zone-chart disabled — {zone_load_error}", file=sys.stderr)

    if zone_render is not None:
        print(
            "Journal: zone chart API v2 loaded — if the view date has no bars in "
            "historical_data, the chart uses the last day on/before that date in the CSV. "
            "GET /api/trade-zone-chart JSON meta includes chart_api_version: 2. "
            "Stop this process and start again if you still see 'No bars for session …' for a future day.",
            file=sys.stderr,
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a: object) -> None:
            return

        def _send_json(self, payload: object, status: int = 200) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_GET(self) -> None:
            # ``self.path`` may include the query string, an absolute-form request-target
            # (``http://host/path?...``), or a trailing slash — normalize so API routes match.
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path or "/"
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            for _canon in (
                "/api/latest",
                "/api/live",
                "/api/recent",
                "/api/activity",
                "/api/dates",
                "/api/trades-day",
                "/api/trade",
                "/api/trade-zone-chart",
            ):
                if path.lower() == _canon.lower():
                    path = _canon
                    break
            qs = urllib.parse.parse_qs(parts.query)

            # Support optional date filter (YYYY-MM-DD)
            selected_date = qs.get("date", [None])[0]
            if selected_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", selected_date):
                selected_date = None

            if path == "/api/latest":
                d = qs.get("date", [None])[0]
                if d and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    snap = _snapshot_with_live_ib(journal.get_daily_snapshot_last_for_date(d), d)
                else:
                    snap = _snapshot_with_live_ib(journal.get_latest_daily_snapshot(), None)
                self._send_json(snap if snap else {})
                return
            if path == "/api/live":
                snap = _snapshot_with_live_ib(None, _today_et())
                self._send_json(snap if snap else {})
                return
            if path == "/api/recent":
                try:
                    n = int(qs.get("n", ["100"])[0])
                except ValueError:
                    n = 100
                d = qs.get("date", [None])[0]
                if d and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    rows = journal.get_recent_daily_snapshots_for_date(d, n)
                else:
                    rows = journal.get_recent_daily_snapshots(n)
                self._send_json({"snapshots": rows})
                return
            if path == "/api/activity":
                activity = journal.get_activity_by_symbol(selected_date)
                day_exit = (
                    journal.get_journal_exit_day_aggregate(selected_date)
                    if selected_date
                    else None
                )
                self._send_json({
                    "by_symbol": activity,
                    "selected_date": selected_date or "all-time",
                    "available_dates": journal.get_available_dates(),
                    "journal_exit_day_aggregate": day_exit,
                })
                return
            if path == "/api/dates":
                self._send_json({"dates": journal.get_available_dates()})
                return
            if path == "/api/trades-day":
                d_day = qs.get("date", [None])[0]
                if not d_day or not re.match(r"^\d{4}-\d{2}-\d{2}$", d_day):
                    self._send_json({"error": "required query: date=YYYY-MM-DD"}, status=400)
                    return
                try:
                    lim = int(qs.get("limit", ["400"])[0])
                except ValueError:
                    lim = 400
                trows = journal.get_trades_touching_calendar_day(d_day, limit=lim)
                self._send_json({"date": d_day, "count": len(trows), "trades": trows})
                return
            if path == "/api/trade":
                rid = (qs.get("trade_id", [""])[0] or "").strip()
                if not rid:
                    self._send_json(
                        {"error": "Required query: trade_id=<id>"},
                        400,
                    )
                    return
                t0 = time.perf_counter()
                _journal_api_log(f"GET /api/trade begin trade_id={rid!r}")
                full = journal.get_trade_full(rid)
                if full is None:
                    _journal_api_log(
                        f"GET /api/trade 404 unknown trade_id={rid!r} in {(time.perf_counter() - t0) * 1e3:.0f}ms"
                    )
                    self._send_json({"error": f"Unknown trade_id: {rid!r}"}, 404)
                    return
                self._send_json(full)
                _journal_api_log(
                    f"GET /api/trade OK in {(time.perf_counter() - t0) * 1e3:.0f}ms trade_id={rid!r}"
                )
                return
            if path == "/api/trade-zone-chart":
                sym = (qs.get("symbol", [""])[0] or "").strip().upper()
                day = (qs.get("day", [""])[0] or "").strip()[:10]
                br = qs.get("bar_size", ["15 mins"])[0] or "15 mins"
                bar_size = str(br).strip() or "15 mins"
                rid = qs.get("run_id", [None])[0]
                run_id = str(rid).strip() if rid is not None and str(rid).strip() else None
                if not sym or not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
                    self._send_json(
                        {"error": "Required query: symbol= and day=YYYY-MM-DD"},
                        400,
                    )
                    return
                if zone_render is None:
                    self._send_json(
                        {
                            "error": (
                                f"Zone chart not loaded on startup. {zone_load_error or ''}".strip()
                            ),
                            "meta": {},
                        },
                        503,
                    )
                    return
                t0 = time.perf_counter()
                _journal_api_log(
                    f"GET /api/trade-zone-chart begin symbol={sym!r} day={day!r} run_id={run_id!r} bar_size={bar_size!r}"
                )
                try:
                    fig, meta = zone_render(
                        symbol=sym,
                        day_yyyy_mm_dd=day,
                        pythonclient_root=_PYTHONCLIENT_ROOT,
                        data_dir=_PYTHONCLIENT_ROOT / "historical_data",
                        csv_path=None,
                        bar_size=bar_size,
                        journal_db=db_path,
                        no_journal=False,
                        run_id=run_id,
                        strategy_params=None,
                    )
                    t1 = time.perf_counter()
                    _journal_api_log(
                        f"GET /api/trade-zone-chart render() done in {(t1 - t0) * 1e3:.0f}ms; figure.to_json() next"
                    )
                    fig_dict = json.loads(fig.to_json())
                    t2 = time.perf_counter()
                    psd = meta.get("plot_session_day") if isinstance(meta, dict) else None
                    chart_ver = meta.get("chart_api_version") if isinstance(meta, dict) else None
                    _journal_api_log(
                        f"GET /api/trade-zone-chart OK total {(t2 - t0) * 1e3:.0f}ms "
                        f"(to_json {(t2 - t1) * 1e3:.0f}ms) plot_session_day={psd!r} "
                        f"chart_api_version={chart_ver!r}"
                    )
                except Exception as e:
                    if zone_err_cls is not None and isinstance(e, zone_err_cls):
                        _journal_api_log(
                            f"GET /api/trade-zone-chart ZoneChartError after {(time.perf_counter() - t0) * 1e3:.0f}ms: {e!r}"
                        )
                        self._send_json({"error": str(e), "meta": {}}, 400)
                    else:
                        _journal_api_log(
                            f"GET /api/trade-zone-chart error after {(time.perf_counter() - t0) * 1e3:.0f}ms: {e!r}"
                        )
                        self._send_json({"error": str(e)}, 500)
                    return
                self._send_json({"figure": fig_dict, "meta": meta})
                return
            m_snap = re.fullmatch(r"/api/snapshot/(\d+)", path)
            if m_snap:
                sid = int(m_snap.group(1))
                snap = journal.get_daily_snapshot_by_id(sid)
                self._send_json(snap if snap else {})
                return
            if path in ("/", "/index.html"):
                dates = journal.get_available_dates()
                effective_date = selected_date or (dates[0] if dates else None)
                if effective_date:
                    cards_snap = journal.get_daily_snapshot_last_for_date(effective_date)
                    recent = journal.get_recent_daily_snapshots_for_date(effective_date, 100)
                else:
                    cards_snap = journal.get_latest_daily_snapshot()
                    recent = journal.get_recent_daily_snapshots(100)
                activity = journal.get_activity_by_symbol(effective_date)
                day_exit = (
                    journal.get_journal_exit_day_aggregate(effective_date)
                    if effective_date
                    else None
                )
                trades_rows = (
                    journal.get_trades_touching_calendar_day(effective_date)
                    if effective_date
                    else []
                )
                cards_snap = _snapshot_with_live_ib(cards_snap, effective_date)
                trades_detail_html = _build_trades_day_html(trades_rows, effective_date)
                self._send_html(
                    _html_shell(
                        cards_snap,
                        recent,
                        activity,
                        dates,
                        str(db_path),
                        effective_date,
                        day_exit,
                        trades_detail_html=trades_detail_html,
                    )
                )
                return
            self.send_error(404, "Not found")

    httpd: ThreadingHTTPServer | None = None
    bound_port: int
    if args.port == 0:
        httpd = ThreadingHTTPServer((args.host, 0), Handler)
        bound_port = int(httpd.server_port)
    else:
        last_err: OSError | None = None
        for candidate in range(args.port, args.port + 30):
            try:
                httpd = ThreadingHTTPServer((args.host, candidate), Handler)
                bound_port = candidate
                break
            except OSError as e:
                last_err = e
                if e.errno != errno.EADDRINUSE:
                    raise
        if httpd is None:
            raise SystemExit(
                f"Could not bind {args.host}:{args.port}–{args.port + 29} (address in use). "
                f"Stop the other process or use: ./py src/journal_dashboard.py --port 0\nLast error: {last_err}"
            ) from last_err
        if bound_port != args.port:
            print(
                f"Note: port {args.port} was in use; listening on {bound_port} instead.",
                file=sys.stderr,
            )

    print(f"Journal dashboard  http://{args.host}:{bound_port}/  (db: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
