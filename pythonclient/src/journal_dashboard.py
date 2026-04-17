#!/usr/bin/env python3
"""
HTTP view of journal DB: IB ``daily_snapshots`` (reqPnL) + **activity by symbol**
(wins / losses / flat from closed ``trades`` rows). Supports **?date=YYYY-MM-DD** query param
to view day-specific metrics (filters closed trades by exit date).

Default: http://127.0.0.1:8765/ (shows latest + all-time activity)

From ``pythonclient``::

    ./py src/journal_dashboard.py
    ./py src/journal_dashboard.py --db /path/to/trade_journal.db --port 8765

If port 8765 is busy, the server tries the next ports (8766, …) automatically, or pass ``--port 8766``.
Use ``--port 0`` to bind an ephemeral free port (URL printed on startup).
"""
from __future__ import annotations

import argparse
import errno
import json
import re
import sys
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


def _html_shell(
    latest: dict | None,
    recent: list,
    activity: list[dict],
    available_dates: list[str],
    db_path: str,
    selected_date: str | None = None,
    day_exit: dict | None = None,
) -> str:
    u = _fmt_money(latest.get("unrealized_pnl") if latest else None)
    r = _fmt_money(latest.get("realized_pnl_today") if latest else None)
    d = _fmt_money(latest.get("ib_daily_pnl") if latest else None)
    nl = _fmt_money(latest.get("net_liquidation") if latest else None)
    ts = (latest or {}).get("time") or "—"
    sid = (latest or {}).get("snapshot_id")
    sid_s = f" (id={sid})" if sid is not None else ""
    r_lbl = "Realized today (IB)"
    d_lbl = "Daily total (IB)"
    if latest and latest.get("realized_pnl_today_src") == "journal":
        r_lbl = "Realized today (journal)"
    if latest and latest.get("ib_daily_pnl_src") == "journal":
        d_lbl = "Daily total (journal)"
    pnl_note = ""
    if latest and (
        latest.get("realized_pnl_today_src") == "journal"
        or latest.get("ib_daily_pnl_src") == "journal"
    ):
        pnl_note = (
            '<p class="meta">Realized / daily use sums of closed <code>trades</code> on this calendar day '
            "when IB P&amp;L was not stored. Unrealized still requires IB (or a new snapshot after the fix).</p>"
        )

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
    tot_pnl = 0.0
    date_title = f" for <strong>{selected_date}</strong>" if selected_date else " (all-time / cumulative)"
    for a in activity:
        sym = a.get("symbol", "")
        o = int(a.get("open") or 0)
        ol = int(a.get("open_long") or 0)
        os_ = int(a.get("open_short") or 0)
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
            "<tr><td colspan='11'>No trades in journal yet — closed trades get win/loss counts here.</td></tr>"
        )
        side_summary = ""
    else:
        tp_cls = "pnl-pos" if tot_pnl > 0 else ("pnl-neg" if tot_pnl < 0 else "")
        act_rows += (
            f"<tr class='totals'><td><strong>All symbols</strong></td>"
            f"<td class='num'>{tot_open}</td><td class='num'>{tot_open_long}</td><td class='num'>{tot_open_short}</td>"
            f"<td class='num'>{tot_closed}</td><td class='num'>{tot_closed_long}</td><td class='num'>{tot_closed_short}</td>"
            f"<td class='num win'>{tot_w}</td><td class='num loss'>{tot_l}</td><td class='num'>{tot_f}</td>"
            f"<td class='num {tp_cls}'>{_fmt_money(tot_pnl)}</td></tr>"
        )

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
    .meta {{ color: #666; font-size: 0.85rem; margin-top: 1rem; }}
    code {{ background: #f0f0f0; padding: 0.1rem 0.35rem; border-radius: 4px; }}
    select {{ padding: 0.5rem; font-size: 1rem; margin: 0.5rem 0; }}
    .date-header {{ display: flex; align-items: center; gap: 1rem; }}
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
  {side_summary}
  <table>
    <thead><tr><th>Symbol</th><th>Open</th><th>Open L</th><th>Open S</th><th>Closed</th><th>Cls L</th><th>Cls S</th><th>Wins</th><th>Losses</th><th>Flat</th><th>Σ realized</th></tr></thead>
    <tbody>{act_rows}</tbody>
  </table>
  <h2>Recent snapshots</h2>
  <table>
    <thead><tr><th>Time</th><th>Net Liq</th><th>Unrealized</th><th>Realized today</th><th>Daily (IB)</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p class="meta">DB: <code>{db_path}</code> · JSON:
  <a href="/api/latest">/api/latest</a> · <a href="/api/activity">/api/activity</a> · <a href="/api/dates">/api/dates</a> · <code>/api/snapshot/&lt;id&gt;</code>
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
    args = ap.parse_args()
    db_path = args.db.resolve()
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
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path or "/"
            qs = urllib.parse.parse_qs(parsed.query)

            # Support optional date filter (YYYY-MM-DD)
            selected_date = qs.get("date", [None])[0]
            if selected_date and not re.match(r"^\d{4}-\d{2}-\d{2}$", selected_date):
                selected_date = None

            if path == "/api/latest":
                snap = journal.get_latest_daily_snapshot()
                self._send_json(snap if snap else {})
                return
            if path == "/api/recent":
                try:
                    n = int(qs.get("n", ["100"])[0])
                except ValueError:
                    n = 100
                self._send_json({"snapshots": journal.get_recent_daily_snapshots(n)})
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
            m_snap = re.fullmatch(r"/api/snapshot/(\d+)", path)
            if m_snap:
                sid = int(m_snap.group(1))
                snap = journal.get_daily_snapshot_by_id(sid)
                self._send_json(snap if snap else {})
                return
            if path in ("/", "/index.html"):
                latest = journal.get_latest_daily_snapshot()
                recent = journal.get_recent_daily_snapshots(100)
                activity = journal.get_activity_by_symbol(selected_date)
                dates = journal.get_available_dates()
                day_exit = (
                    journal.get_journal_exit_day_aggregate(selected_date)
                    if selected_date
                    else None
                )
                self._send_html(
                    _html_shell(
                        latest,
                        recent,
                        activity,
                        dates,
                        str(db_path),
                        selected_date,
                        day_exit,
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
