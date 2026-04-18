#!/usr/bin/env python3
"""
HTTP view of journal DB: IB ``daily_snapshots`` (reqPnL) + **activity by symbol**
(wins / losses / flat from closed ``trades`` rows). Supports **?date=YYYY-MM-DD** query param
to view day-specific metrics: activity filters closed trades by exit date; **top P&amp;L cards**
use the last ``daily_snapshots`` row for that calendar ``date`` column (aligned with watch-mode snapshots).
With a date selected, a **per-trade** section lists each ``trade_id`` grouped by symbol: entry/exit
times, intended and filled quantities, P&amp;L, and JSON params (entry snapshot vs any ``exit_*`` keys).
JSON: ``/api/trades-day?date=YYYY-MM-DD`` (optional ``limit=``).

Default: http://127.0.0.1:8765/ (shows latest + all-time activity)

From ``pythonclient``::

    ./py src/journal_dashboard.py
    ./py src/journal_dashboard.py --db /path/to/trade_journal.db --port 8765
    ./py src/journal_dashboard.py --purge-before-date 2026-04-17   # destructive: drop history before that day

If port 8765 is busy, the server tries the next ports (8766, …) automatically, or pass ``--port 8766``.
Use ``--port 0`` to bind an ephemeral free port (URL printed on startup).
"""
from __future__ import annotations

import argparse
import errno
import html
import json
import re
import sys
from collections import defaultdict
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# pythonclient/ (parent of this file's directory)
_PYTHONCLIENT_ROOT = Path(__file__).resolve().parent.parent
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from trade_journal import TradeJournal  # noqa: E402


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
        "<strong>E qty</strong> / <strong>X qty</strong> = Σ <code>fills</code> for entry vs exit legs.</p>",
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
            body += (
                "<tr>"
                f"<td><strong>{touch}</strong></td>"
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
            "<th>Touch</th><th>Trade</th><th>Side</th><th>Status</th>"
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
            '<p class="meta"><strong>IB vs this table</strong> — Top cards use the <strong>last</strong> '
            f"<code>daily_snapshots</code> row for journal calendar date <strong>{selected_date}</strong> "
            "(same <code>date</code> column as watch-mode snapshots). IB account-level <code>reqPnL</code> is "
            "whole-account / all products. Σ realized and Closed counts use only <code>trades</code> rows "
            "marked <code>closed</code> with <code>exit_fill_time</code> on that day. "
            "They diverge if IB realized came from activity not journaled here, or if closes never updated "
            "<code>trades</code>.</p>"
        )
    else:
        ib_vs_journal = (
            '<p class="meta"><strong>IB vs this table</strong> — The cards use the latest '
            "<code>daily_snapshots</code> row: IB account-level <code>reqPnL</code> (whole account, all products). "
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
  </style>
</head>
<body>
  <h1>IBKR P&amp;L (from journal)</h1>
  <div class="date-header">
    <label for="date-select">View metrics for date:</label>
    <select id="date-select" onchange="window.location = '?date=' + this.value">
      <option value="">All Time (latest snapshot)</option>
      {date_options}
    </select>
  </div>
  {api_trades_quick}
  <p>Showing data{date_title}. Latest snapshot: <strong>{ts}</strong>{sid_s}</p>
  <div class="cards">
    <div class="card"><div class="label">Net liquidation</div><div class="value">{nl}</div></div>
    <div class="card"><div class="label">Unrealized (IB)</div><div class="value">{u}</div></div>
    <div class="card"><div class="label">{r_lbl}</div><div class="value">{r}</div></div>
    <div class="card"><div class="label">{d_lbl}</div><div class="value">{d}</div></div>
  </div>
  {pnl_note}
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
  · Auto-refresh every 60s (latest only).</p>
  <script>
    // Pre-select current value if URL has ?date= param
    const urlParams = new URLSearchParams(window.location.search);
    const urlDate = urlParams.get('date');
    if (urlDate) {{
      const select = document.getElementById('date-select');
      if (select) select.value = urlDate;
    }}
    setTimeout(function(){{ 
      // Only auto-refresh if viewing latest (no date filter)
      if (!urlDate) location.reload(); 
    }}, 60000);
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
    args = ap.parse_args()
    db_path = args.db.resolve()
    if args.purge_before_date:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", args.purge_before_date):
            raise SystemExit("--purge-before-date must be YYYY-MM-DD")
        journal = TradeJournal(db_path)
        stats = journal.purge_journal_before_calendar_date(args.purge_before_date)
        print(json.dumps(stats, indent=2))
        print(f"Purge complete (kept {args.purge_before_date} onward). DB: {db_path}")
        return
    journal = TradeJournal(db_path)

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
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            # ``self.path`` may include the query string, an absolute-form request-target
            # (``http://host/path?...``), or a trailing slash — normalize so API routes match.
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path or "/"
            if len(path) > 1 and path.endswith("/"):
                path = path[:-1]
            for _canon in ("/api/latest", "/api/recent", "/api/activity", "/api/dates", "/api/trades-day"):
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
                    snap = journal.get_daily_snapshot_last_for_date(d)
                else:
                    snap = journal.get_latest_daily_snapshot()
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
            m_snap = re.fullmatch(r"/api/snapshot/(\d+)", path)
            if m_snap:
                sid = int(m_snap.group(1))
                snap = journal.get_daily_snapshot_by_id(sid)
                self._send_json(snap if snap else {})
                return
            if path in ("/", "/index.html"):
                if selected_date:
                    cards_snap = journal.get_daily_snapshot_last_for_date(selected_date)
                    recent = journal.get_recent_daily_snapshots_for_date(selected_date, 100)
                else:
                    cards_snap = journal.get_latest_daily_snapshot()
                    recent = journal.get_recent_daily_snapshots(100)
                activity = journal.get_activity_by_symbol(selected_date)
                dates = journal.get_available_dates()
                day_exit = (
                    journal.get_journal_exit_day_aggregate(selected_date)
                    if selected_date
                    else None
                )
                trades_rows = (
                    journal.get_trades_touching_calendar_day(selected_date)
                    if selected_date
                    else []
                )
                trades_detail_html = _build_trades_day_html(trades_rows, selected_date)
                self._send_html(
                    _html_shell(
                        cards_snap,
                        recent,
                        activity,
                        dates,
                        str(db_path),
                        selected_date,
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
