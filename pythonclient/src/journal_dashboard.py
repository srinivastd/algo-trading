#!/usr/bin/env python3
"""
HTTP view of journal DB: IB ``daily_snapshots`` (reqPnL) plus **activity by symbol**
(wins / losses / flat from closed ``trades`` rows).

Default: http://127.0.0.1:8765/

From ``pythonclient``::

    ./py src/journal_dashboard.py
    ./py src/journal_dashboard.py --db /path/to/trade_journal.db --port 8765

If port 8765 is busy, use ``--port 8766``.
"""
from __future__ import annotations

import argparse
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


def _html_shell(latest: dict | None, recent: list, activity: list[dict], db_path: str) -> str:
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
    tot_pnl = 0.0
    for a in activity:
        sym = a.get("symbol", "")
        o = int(a.get("open") or 0)
        c = int(a.get("closed") or 0)
        w = int(a.get("wins") or 0)
        l = int(a.get("losses") or 0)
        f = int(a.get("flat") or 0)
        pnl = float(a.get("total_realized_pnl") or 0.0)
        tot_open += o
        tot_closed += c
        tot_w += w
        tot_l += l
        tot_f += f
        tot_pnl += pnl
        pnl_cls = "pnl-pos" if pnl > 0 else ("pnl-neg" if pnl < 0 else "")
        act_rows += (
            f"<tr><td><strong>{sym}</strong></td>"
            f"<td class='num'>{o}</td><td class='num'>{c}</td>"
            f"<td class='num win'>{w}</td><td class='num loss'>{l}</td><td class='num'>{f}</td>"
            f"<td class='num {pnl_cls}'>{_fmt_money(pnl)}</td></tr>"
        )
    if not act_rows:
        act_rows = (
            "<tr><td colspan='7'>No trades in journal yet — closed trades get win/loss counts here.</td></tr>"
        )
    else:
        tp_cls = "pnl-pos" if tot_pnl > 0 else ("pnl-neg" if tot_pnl < 0 else "")
        act_rows += (
            f"<tr class='totals'><td><strong>All symbols</strong></td>"
            f"<td class='num'>{tot_open}</td><td class='num'>{tot_closed}</td>"
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
  </style>
</head>
<body>
  <h1>IBKR P&amp;L (from journal)</h1>
  <p>Latest snapshot: <strong>{ts}</strong>{sid_s}</p>
  <div class="cards">
    <div class="card"><div class="label">Net liquidation</div><div class="value">{nl}</div></div>
    <div class="card"><div class="label">Unrealized (IB)</div><div class="value">{u}</div></div>
    <div class="card"><div class="label">{r_lbl}</div><div class="value">{r}</div></div>
    <div class="card"><div class="label">{d_lbl}</div><div class="value">{d}</div></div>
  </div>
  {pnl_note}
  <h2>Activity by symbol</h2>
  <p class="meta">From <code>trades</code>: open = still open; closed = exited. Win/loss/flat use realized P&amp;L on closed rows.</p>
  <table>
    <thead><tr><th>Symbol</th><th>Open</th><th>Closed</th><th>Wins</th><th>Losses</th><th>Flat</th><th>Σ realized</th></tr></thead>
    <tbody>{act_rows}</tbody>
  </table>
  <h2>Recent snapshots</h2>
  <table>
    <thead><tr><th>Time</th><th>Net Liq</th><th>Unrealized</th><th>Realized today</th><th>Daily (IB)</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
  <p class="meta">DB: <code>{db_path}</code> · JSON:
  <a href="/api/latest">/api/latest</a> · <a href="/api/activity">/api/activity</a> · <code>/api/snapshot/&lt;id&gt;</code>
  · Auto-refresh every 30s.</p>
  <script>setTimeout(function(){{ location.reload(); }}, 30000);</script>
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
    ap.add_argument("--port", type=int, default=8765)
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
            if path == "/api/latest":
                snap = journal.get_latest_daily_snapshot()
                self._send_json(snap if snap else {})
                return
            if path == "/api/recent":
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    n = int(qs.get("n", ["100"])[0])
                except ValueError:
                    n = 100
                self._send_json({"snapshots": journal.get_recent_daily_snapshots(n)})
                return
            if path == "/api/activity":
                self._send_json({"by_symbol": journal.get_activity_by_symbol()})
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
                activity = journal.get_activity_by_symbol()
                self._send_html(_html_shell(latest, recent, activity, str(db_path)))
                return
            self.send_error(404, "Not found")

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Journal dashboard  http://{args.host}:{args.port}/  (db: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
