#!/usr/bin/env python3
"""
Read-only local web UI for trade journal: holdings (from snapshots) and trades by day.

Trades are grouped by ``(run_id, symbol)`` and sorted by entry time. Heuristic links:
each **closed** row shows the most recent **open** row in the same chain whose entry is
before this exit; each **open** row shows the next **closed** row in the chain (if any).
This is best-effort when the DB has overlapping bootstrap + strategy rows.

Does not connect to IBKR and does not modify the database. Safe to run alongside
``main.py --watch`` — uses a separate process and port; SQLite WAL allows concurrent reads.

  cd pythonclient && PYTHONPATH=src ./py src/journal_dashboard.py
  # then open http://127.0.0.1:8765/

  ./py src/journal_dashboard.py --port 8766 --db ./trade_journal.db
  ./py src/journal_dashboard.py --commission-default pro-fixed   # Pro-tier estimates by default
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from datetime import date
from typing import Any

# IBKR published US stock pricing (see commissions-stocks link below).
# - **Lite**: $0 IB commission on US exchange-listed stocks/ETFs (regulatory fees may apply on sells).
# - **Pro fixed**: per-share + minimum + % of trade cap (approximation for dashboard only).
_IBKR_COMMISSIONS_STOCKS_URL = (
    "https://www.interactivebrokers.com/en/pricing/commissions-stocks.php"
)
_IBKR_PRO_FIXED_PER_SHARE = 0.005
_IBKR_PRO_FIXED_MIN_PER_ORDER = 1.0
_IBKR_PRO_FIXED_MAX_FRAC_NOTIONAL = 0.01


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    return con


def _latest_snapshot_for_date(con: sqlite3.Connection, day: str) -> sqlite3.Row | None:
    cur = con.execute(
        """
        SELECT id, date, time, net_liquidation, positions_json, unrealized_pnl, realized_pnl_today
        FROM daily_snapshots
        WHERE date = ?
        ORDER BY time DESC
        LIMIT 1
        """,
        (day,),
    )
    return cur.fetchone()


def _latest_snapshot_any(con: sqlite3.Connection) -> sqlite3.Row | None:
    cur = con.execute(
        """
        SELECT id, date, time, net_liquidation, positions_json, unrealized_pnl, realized_pnl_today
        FROM daily_snapshots
        ORDER BY time DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def _ibkr_pro_fixed_commission_one_order(shares: float | int | None, price: float | None) -> float | None:
    """Single US stock order, IBKR Pro **fixed** tier approximation (excludes reg/exchange fees)."""
    if shares is None or price is None:
        return None
    q = abs(float(shares))
    px = float(price)
    if q <= 0 or px <= 0:
        return None
    notional = q * px
    raw = _IBKR_PRO_FIXED_PER_SHARE * q
    leg = max(_IBKR_PRO_FIXED_MIN_PER_ORDER, raw)
    leg = min(leg, _IBKR_PRO_FIXED_MAX_FRAC_NOTIONAL * notional)
    return round(leg, 2)


def _fills_agg(con: sqlite3.Connection, trade_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Per trade_id: summed fill commissions and qty by entry vs exit (is_entry 1/0)."""
    if not trade_ids:
        return {}
    uniq: list[str] = list(dict.fromkeys(trade_ids))
    ph = ",".join("?" * len(uniq))
    cur = con.execute(
        f"""
        SELECT trade_id, is_entry,
               SUM(qty) AS sqty,
               SUM(COALESCE(commission, 0)) AS scomm
        FROM fills
        WHERE trade_id IN ({ph})
        GROUP BY trade_id, is_entry
        """,
        uniq,
    )
    out: dict[str, dict[str, Any]] = {}
    for row in cur:
        tid = str(row["trade_id"])
        d = out.setdefault(
            tid,
            {"entry_qty": None, "exit_qty": None, "fill_comm": 0.0},
        )
        d["fill_comm"] += float(row["scomm"] or 0.0)
        q = float(row["sqty"] or 0.0)
        if row["is_entry"]:
            d["entry_qty"] = (d["entry_qty"] or 0.0) + q
        else:
            d["exit_qty"] = (d["exit_qty"] or 0.0) + q
    return out


def _estimate_row_commission_ibkr(
    t: dict,
    *,
    model: str,
    fill_meta: dict[str, Any] | None,
) -> tuple[float | None, int]:
    """
    Commission for one journal row. ``BF_*`` backfill rows → no estimate (log replay, not IB).

    - **lite**: $0 IB commission per share on US listed (IBKR Lite); still returns leg count.
    - **pro-fixed**: Pro fixed tier; uses fill qty from ``fills`` when present else ``intended_shares``.
    """
    tid = str(t.get("trade_id") or "")
    if tid.startswith("BF_"):
        return None, 0

    st = str(t.get("status") or "").lower()
    sh = t.get("intended_shares")
    entry_px = t.get("entry_fill_price")
    exit_px = t.get("exit_fill_price")
    fm = fill_meta or {}
    fill_comm = float(fm.get("fill_comm") or 0.0)
    if fill_comm > 0:
        n = 0
        if entry_px is not None:
            n += 1
        if st == "closed" and exit_px is not None:
            n += 1
        return round(fill_comm, 2), max(n, 1)

    def _qty_for_leg(exit_leg: bool) -> float | int | None:
        key = "exit_qty" if exit_leg else "entry_qty"
        q = fm.get(key)
        if q is not None and float(q) > 0:
            return q
        return sh

    q_entry = _qty_for_leg(False)
    q_exit = _qty_for_leg(True) if st == "closed" else None

    if model == "lite":
        n = 0
        if q_entry is not None and entry_px is not None:
            if abs(float(q_entry)) > 0 and float(entry_px) > 0:
                n += 1
        if st == "closed" and q_exit is not None and exit_px is not None:
            if abs(float(q_exit)) > 0 and float(exit_px) > 0:
                n += 1
        elif st == "closed" and exit_px is not None and q_entry is not None:
            if abs(float(q_entry)) > 0 and float(exit_px) > 0:
                n += 1
        if n == 0:
            return None, 0
        return 0.0, n

    total = 0.0
    n = 0
    e = _ibkr_pro_fixed_commission_one_order(q_entry, entry_px)
    if e is not None:
        total += e
        n += 1
    if st == "closed":
        qx = q_exit if q_exit is not None else q_entry
        x = _ibkr_pro_fixed_commission_one_order(qx, exit_px)
        if x is not None:
            total += x
            n += 1
    if n == 0:
        return None, 0
    return round(total, 2), n


def _comm_cell_html(t: dict, *, model: str, fill_meta: dict[str, Any] | None) -> str:
    tid = str(t.get("trade_id") or "")
    if tid.startswith("BF_"):
        return '<td class="num comm muted" title="Backfill row (BF_*); no IB commission estimate">—</td>'

    est, _n = _estimate_row_commission_ibkr(t, model=model, fill_meta=fill_meta)
    stored = t.get("commission")
    if est is None:
        est_part = "—"
    elif model == "lite" and est == 0.0:
        est_part = "0.00"
    else:
        est_part = f"~{est:.2f}"

    if model == "lite":
        tip = (
            f"IBKR Lite: $0 commission on US exchange-listed stocks/ETFs per IBKR; "
            f"regulatory fees on some sells not modeled. {_IBKR_COMMISSIONS_STOCKS_URL}"
        )
    else:
        tip = (
            f"IBKR Pro fixed-tier estimate: ${_IBKR_PRO_FIXED_PER_SHARE}/share, "
            f"${_IBKR_PRO_FIXED_MIN_PER_ORDER:g} min/order, "
            f"{100 * _IBKR_PRO_FIXED_MAX_FRAC_NOTIONAL:.0f}% notional cap. "
            f"Uses fills qty when logged. {_IBKR_COMMISSIONS_STOCKS_URL}"
        )
    if stored is not None:
        try:
            sv = float(stored)
        except (TypeError, ValueError):
            sv = None
        if sv is not None:
            return (
                f'<td class="num comm" title="{_html_escape(tip)}">{est_part}'
                f'<br/><span class="muted" style="font-size:0.75rem">stored {sv:.2f}</span></td>'
            )
    return f'<td class="num comm" title="{_html_escape(tip)}">{est_part}</td>'


def _trades_for_day(con: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    cur = con.execute(
        """
        SELECT trade_id, run_id, symbol, side, status,
               intended_entry_price, intended_shares,
               entry_fill_price, substr(entry_fill_time,1,19) AS entry_t,
               exit_fill_price, substr(exit_fill_time,1,19) AS exit_t,
               exit_reason, realized_pnl, commission, error_message,
               substr(created_at,1,19) AS created_t
        FROM trades
        WHERE substr(created_at,1,10) = ?
           OR substr(COALESCE(entry_fill_time,''),1,10) = ?
           OR substr(COALESCE(exit_fill_time,''),1,10) = ?
        ORDER BY COALESCE(entry_fill_time, created_at)
        """,
        (day, day, day),
    )
    return cur.fetchall()


def _row_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def _day_total_est_commission(
    rows: list[sqlite3.Row],
    fills_map: dict[str, dict[str, Any]],
    model: str,
) -> float:
    """Sum per-row commission (same as Comm ~). Excludes ``BF_*`` backfill rows."""
    s = 0.0
    for r in rows:
        tid = str(r["trade_id"] or "")
        if tid.startswith("BF_"):
            continue
        d = _row_dict(r)
        est, _n = _estimate_row_commission_ibkr(d, model=model, fill_meta=fills_map.get(tid))
        if est is not None:
            s += est
    return round(s, 2)


def _day_closed_pnl_summary(rows: list[sqlite3.Row]) -> tuple[float, int, int, int, int]:
    """Sum realized PnL and win/loss/flat counts for **closed** rows in the day filter."""
    total = 0.0
    wins = losses = flats = 0
    counted = 0
    for r in rows:
        if str(r["status"] or "").lower() != "closed":
            continue
        p = r["realized_pnl"]
        if p is None:
            continue
        v = float(p)
        total += v
        counted += 1
        if v > 0:
            wins += 1
        elif v < 0:
            losses += 1
        else:
            flats += 1
    return total, wins, losses, flats, counted


def _wl_cell(t: dict) -> str:
    """Win / Loss / Flat for closed rows with realized_pnl; em dash otherwise."""
    st = str(t.get("status") or "").lower()
    if st != "closed":
        return '<td class="wl"><span class="wl-na">—</span></td>'
    pnl = t.get("realized_pnl")
    if pnl is None:
        return '<td class="wl"><span class="wl-na">—</span></td>'
    v = float(pnl)
    if v > 0:
        return '<td class="wl"><span class="wl-win">Win</span></td>'
    if v < 0:
        return '<td class="wl"><span class="wl-loss">Loss</span></td>'
    return '<td class="wl"><span class="wl-flat">Flat</span></td>'


def _sort_ts(r: dict) -> str:
    return str(r.get("entry_t") or r.get("created_t") or "")


def _group_trades_by_run_symbol(rows: list[sqlite3.Row]) -> list[tuple[tuple[str, str], list[dict]]]:
    """Group into (run_id, symbol) chains, each sorted chronologically."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        d = _row_dict(r)
        run = str(d.get("run_id") or "").strip() or "(no run_id)"
        sym = str(d.get("symbol") or "").strip().upper() or "(no symbol)"
        buckets.setdefault((run, sym), []).append(d)
    out: list[tuple[tuple[str, str], list[dict]]] = []
    for key, lst in buckets.items():
        lst.sort(key=_sort_ts)
        out.append((key, lst))
    out.sort(key=lambda kv: _sort_ts(kv[1][0]) if kv[1] else "")
    return out


def _correlate_chain(chain: list[dict]) -> None:
    """
    Heuristic open→closed links within one (run_id, symbol) chain (time-ordered).

    - For each **closed** row: `prior_open_id` = most recent **open** row before it
      whose entry time is strictly before this row's exit time (string compare works on ISO).
    - For each **open** row: `next_close_id` = first **closed** row after it whose exit
      time is after this row's entry (or created) time.
    Mutates dicts in place with keys prior_open_id, next_close_id (or None).
    """
    n = len(chain)
    for i, r in enumerate(chain):
        r["prior_open_id"] = None
        r["next_close_id"] = None
    for i, r in enumerate(chain):
        if str(r.get("status") or "").lower() != "closed":
            continue
        exit_t = str(r.get("exit_t") or "")
        if not exit_t:
            continue
        for j in range(i - 1, -1, -1):
            p = chain[j]
            if str(p.get("status") or "").lower() != "open":
                continue
            ent = str(p.get("entry_t") or p.get("created_t") or "")
            if ent and ent < exit_t:
                r["prior_open_id"] = p.get("trade_id")
                break
    for i, r in enumerate(chain):
        if str(r.get("status") or "").lower() != "open":
            continue
        entry_t = str(r.get("entry_t") or r.get("created_t") or "")
        if not entry_t:
            continue
        for j in range(i + 1, n):
            c = chain[j]
            if str(c.get("status") or "").lower() != "closed":
                continue
            ex = str(c.get("exit_t") or "")
            if ex and ex > entry_t:
                r["next_close_id"] = c.get("trade_id")
                break


def _page(db: Path, day: str, commission_model: str) -> str:
    snap_note = ""
    holdings_rows = ""
    snap_meta = ""

    try:
        con = _connect(db)
    except sqlite3.OperationalError as e:
        return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Journal</title></head>
        <body><h1>Could not open database (read-only)</h1><pre>{_html_escape(str(e))}</pre>
        <p>Path: {_html_escape(str(db))}</p></body></html>"""

    trades: list[sqlite3.Row] = []
    fills_map: dict[str, dict[str, Any]] = {}
    try:
        trades = _trades_for_day(con, day)
        tids = [str(r["trade_id"]) for r in trades if r["trade_id"] is not None]
        fills_map = _fills_agg(con, tids)
        snap = _latest_snapshot_for_date(con, day)
        if snap is None:
            snap = _latest_snapshot_any(con)
            if snap is not None:
                snap_note = (
                    f"<p class=\"muted\">No snapshot row for <strong>{_html_escape(day)}</strong>; "
                    f"showing latest snapshot from <strong>{_html_escape(snap['date'])}</strong>.</p>"
                )
        if snap is None:
            snap_meta = "<p class=\"muted\">No rows in <code>daily_snapshots</code> yet.</p>"
        else:
            snap_meta = (
                f"<p><strong>Snapshot time:</strong> {_html_escape(snap['time'])} "
                f"(id={snap['id']}) &nbsp; NL: {snap['net_liquidation']!s} &nbsp; "
                f"Unreal: {snap['unrealized_pnl']!s} &nbsp; Realized today: {snap['realized_pnl_today']!s}</p>"
            )
            try:
                positions = json.loads(snap["positions_json"] or "[]")
            except json.JSONDecodeError:
                positions = []
            if not positions:
                holdings_rows = "<tr><td colspan=\"4\" class=\"muted\">No open positions in this snapshot.</td></tr>"
            else:
                for p in positions:
                    sym = _html_escape(str(p.get("symbol", "")))
                    qty = p.get("qty", "")
                    ac = p.get("avgCost", "")
                    st = _html_escape(str(p.get("secType", "")))
                    holdings_rows += f"<tr><td>{sym}</td><td>{st}</td><td class=\"num\">{qty}</td><td class=\"num\">{ac}</td></tr>"
    finally:
        con.close()

    pnl_total, pnl_wins, pnl_losses, pnl_flats, pnl_closed_ct = _day_closed_pnl_summary(trades)
    day_total_comm = _day_total_est_commission(trades, fills_map, commission_model)
    net_after_comm = round(pnl_total - day_total_comm, 2)
    if commission_model == "lite":
        comm_block = (
            f"<strong>Total IB commission (IBKR Lite — US listed stocks/ETFs):</strong> "
            f"<span class=\"mono\">{day_total_comm:,.2f}</span>"
            f"<span class=\"muted\"> · <code>BF_*</code> backfill excluded · "
            f"<a href=\"{_IBKR_COMMISSIONS_STOCKS_URL}\" target=\"_blank\" rel=\"noopener\">IBKR schedule</a> "
            f"($0 IB stock/ETF commission on Lite; small regulatory fees on some sells not modeled).</span>"
        )
    else:
        comm_block = (
            f"<strong>Total est. commission (IBKR Pro fixed tier):</strong> "
            f"<span class=\"mono\">~{day_total_comm:,.2f}</span>"
            f"<span class=\"muted\"> — sum of <strong>Comm ~</strong>; uses <code>fills</code> qty when logged. "
            f"<code>BF_*</code> excluded. </span>"
            f"<a href=\"{_IBKR_COMMISSIONS_STOCKS_URL}\" target=\"_blank\" rel=\"noopener\">IBKR schedule</a>."
        )
    if pnl_closed_ct:
        net_line = (
            f"<br/><strong>Approx. net after est. commission:</strong> "
            f"<span class=\"{'pnl-pos' if net_after_comm >= 0 else 'pnl-neg'}\">{net_after_comm:,.2f}</span>"
            f"<span class=\"muted\"> (realized P&amp;L minus total est. comm; open legs included in comm only).</span>"
        )
        pnl_summary_html = (
            f"<section class=\"daypnl\"><h2>Day realized P&amp;L</h2>"
            f"<p><strong>Total (closed legs, this day filter):</strong> "
            f"<span class=\"{'pnl-pos' if pnl_total >= 0 else 'pnl-neg'}\">{pnl_total:,.2f}</span>"
            f" &nbsp;<span class=\"muted\">{pnl_closed_ct} closed with PnL · "
            f"<span class=\"wl-win\">{pnl_wins} win{'s' if pnl_wins != 1 else ''}</span> · "
            f"<span class=\"wl-loss\">{pnl_losses} loss{'es' if pnl_losses != 1 else ''}</span> · "
            f"<span class=\"wl-flat\">{pnl_flats} flat</span></span>"
            f"<br/>{comm_block}{net_line}</p></section>"
        )
    else:
        pnl_summary_html = (
            "<section class=\"daypnl\"><h2>Day realized P&amp;L</h2>"
            "<p class=\"muted\">No closed <code>trades</code> rows with <code>realized_pnl</code> "
            "for this calendar day.</p>"
            f"<p>{comm_block}</p></section>"
        )

    chains = _group_trades_by_run_symbol(trades)
    for _key, chain in chains:
        _correlate_chain(chain)

    chain_blocks = ""
    if not chains:
        chain_blocks = "<p class=\"muted\">No trades for this day.</p>"
    for (run, sym), chain in chains:
        n_open = sum(1 for r in chain if str(r.get("status") or "").lower() == "open")
        n_closed = sum(1 for r in chain if str(r.get("status") or "").lower() == "closed")
        pnl_sum = sum(float(r["realized_pnl"] or 0) for r in chain if str(r.get("status") or "").lower() == "closed")
        chain_est_comm = 0.0
        for r in chain:
            if str(r.get("status") or "").lower() != "closed":
                continue
            tid_c = str(r.get("trade_id") or "")
            est, nlegs = _estimate_row_commission_ibkr(
                r, model=commission_model, fill_meta=fills_map.get(tid_c)
            )
            if est is not None and nlegs >= 2:
                chain_est_comm += est
        chain_est_comm = round(chain_est_comm, 2)
        chain_comm_disp = (
            f"{chain_est_comm:.2f}" if commission_model == "lite" else f"~{chain_est_comm:.2f}"
        )
        inner = ""
        for t in chain:
            pnl = t["realized_pnl"]
            pnl_s = f"{pnl:.2f}" if pnl is not None else ""
            err = t["error_message"] or ""
            err_cell = f'<span class="err">{_html_escape(err[:200])}</span>' if err else ""
            tid = str(t["trade_id"])
            tid_show = tid if len(tid) <= 48 else tid[:45] + "…"
            if tid.startswith("BF_"):
                leg = "BF"
            elif tid.startswith("BOOTSTRAP_"):
                leg = "BOOT"
            else:
                leg = "TRADE"
            st = str(t.get("status") or "").lower()
            prior_cell = "—"
            if st == "closed" and t.get("prior_open_id"):
                po = str(t["prior_open_id"])
                prior_cell = f'<span class="mono" title="{_html_escape(po)}">{_html_escape(po if len(po) <= 36 else po[:33] + "…")}</span>'
            next_cell = "—"
            if st == "open" and t.get("next_close_id"):
                nc = str(t["next_close_id"])
                next_cell = f'<span class="mono" title="{_html_escape(nc)}">{_html_escape(nc if len(nc) <= 36 else nc[:33] + "…")}</span>'
            wl = _wl_cell(t)
            ccell = _comm_cell_html(
                t,
                model=commission_model,
                fill_meta=fills_map.get(tid),
            )
            inner += (
                f"<tr><td class=\"tag\">{_html_escape(leg)}</td>"
                f"<td class=\"mono\" title=\"{_html_escape(tid)}\">{_html_escape(tid_show)}</td>"
                f"<td>{_html_escape(str(t['side']))}</td>"
                f"<td>{_html_escape(str(t['status']))}</td>"
                f"<td class=\"num\">{t['intended_shares']}</td>"
                f"<td class=\"num\">{t['entry_fill_price']!s}</td><td>{_html_escape(str(t['entry_t'] or ''))}</td>"
                f"<td>{_html_escape(str(t['exit_t'] or ''))}</td><td>{_html_escape(str(t['exit_reason'] or ''))}</td>"
                f"<td class=\"num\">{pnl_s}</td>"
                f"{ccell}"
                f"{wl}"
                f"<td>{prior_cell}</td><td>{next_cell}</td><td>{err_cell}</td></tr>"
            )
        chain_blocks += (
            f"<section class=\"chain\"><h3>{_html_escape(sym)} <span class=\"muted\">·</span> "
            f"<span class=\"mono\">{_html_escape(run)}</span></h3>"
            f"<p class=\"muted\">{len(chain)} row(s): {n_open} open, {n_closed} closed &nbsp;·&nbsp; "
            f"Realized PnL (closed): <strong>{pnl_sum:.2f}</strong>"
            f" &nbsp;·&nbsp; Comm (closed RT): <strong>{chain_comm_disp}</strong></p>"
            f"<table><thead><tr><th>Kind</th><th>Trade id</th><th>Side</th><th>Status</th>"
            f"<th class=\"num\">Shares</th><th class=\"num\">Entry fill</th><th>Entry</th><th>Exit</th>"
            f"<th>Reason</th><th class=\"num\">PnL</th>"
            f"<th class=\"num\" title=\"Lite=$0 IB (US listed); Pro fixed=~ per schedule; stored=journal\">Comm</th>"
            f"<th class=\"wl\">W/L</th>"
            f"<th title=\"For closed rows: latest prior OPEN in this chain with entry before this exit.\">Prior open →</th>"
            f"<th title=\"For open rows: next CLOSED in this chain after this entry.\">→ Next close</th>"
            f"<th>Error</th></tr></thead><tbody>{inner}</tbody></table></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Journal — {_html_escape(day)}</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 1.25rem; max-width: 1200px; background: #0f1419; color: #e6edf3; }}
    h1 {{ font-size: 1.25rem; }}
    .muted {{ color: #8b949e; }}
    .mono {{ font-family: ui-monospace, monospace; font-size: 0.75rem; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 0.75rem; font-size: 0.875rem; }}
    th, td {{ border: 1px solid #30363d; padding: 0.4rem 0.5rem; text-align: left; }}
    th {{ background: #161b22; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .err {{ color: #f85149; }}
    .tag {{ font-size: 0.7rem; font-weight: 600; color: #58a6ff; }}
    section.daypnl {{ margin-top: 1.25rem; }}
    section.daypnl h2 {{ font-size: 1.05rem; margin-bottom: 0.25rem; }}
    .pnl-pos {{ color: #3fb950; }}
    .pnl-neg {{ color: #f85149; }}
    th.wl, td.wl {{ white-space: nowrap; }}
    .wl-win {{ color: #3fb950; font-weight: 600; }}
    .wl-loss {{ color: #f85149; font-weight: 600; }}
    .wl-flat {{ color: #d29922; font-weight: 600; }}
    .wl-na {{ color: #8b949e; }}
    section.chain {{ margin-top: 1.5rem; padding-top: 0.5rem; border-top: 1px solid #30363d; }}
    section.chain h3 {{ margin: 0.35rem 0 0.25rem; font-size: 1rem; }}
    input[type="date"] {{ background: #21262d; color: #e6edf3; border: 1px solid #30363d; padding: 0.35rem; }}
    select {{ background: #21262d; color: #e6edf3; border: 1px solid #30363d; padding: 0.35rem; margin: 0 0.5rem; border-radius: 4px; }}
    button {{ background: #238636; color: #fff; border: none; padding: 0.4rem 0.9rem; border-radius: 6px; cursor: pointer; }}
    code {{ background: #161b22; padding: 0.1rem 0.35rem; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Trade journal</h1>
  <p class="muted">Read-only · DB: <code>{_html_escape(str(db))}</code></p>
  <form method="get" action="/">
    <label>Day (journal dates / snapshot <code>date</code>): </label>
    <input type="date" name="date" value="{_html_escape(day)}"/>
    <label style="margin-left:1rem">Commission model: </label>
    <select name="comm_model">
      <option value="lite" {"selected" if commission_model == "lite" else ""}>IBKR Lite ($0 US stock/ETF IB fee)</option>
      <option value="pro-fixed" {"selected" if commission_model == "pro-fixed" else ""}>IBKR Pro fixed ($0.005/sh est.)</option>
    </select>
    <button type="submit">Load</button>
  </form>
  {snap_note}
  <h2>Holdings (latest snapshot)</h2>
  {snap_meta}
  <table>
    <thead><tr><th>Symbol</th><th>Type</th><th class="num">Qty</th><th class="num">Avg cost</th></tr></thead>
    <tbody>{holdings_rows}</tbody>
  </table>
  {pnl_summary_html}
  <h2>Trades &amp; open → closed (by run + symbol)</h2>
  <p class="muted">Rows where <code>created_at</code>, <code>entry_fill_time</code>, or <code>exit_fill_time</code> starts with the selected date. Grouped by <code>run_id</code> + symbol, time-ordered. <strong>Prior open →</strong> on a <em>closed</em> leg points at the latest still-open row before this exit. <strong>→ Next close</strong> on an <em>open</em> leg points at the next closed row in the chain (if any).</p>
  <p class="muted">Commission column follows the model selected above. Default <strong>IBKR Lite</strong> matches IBKR’s $0 commission on US exchange-listed stocks/ETFs (see <a href="{_IBKR_COMMISSIONS_STOCKS_URL}" target="_blank" rel="noopener">commissions — stocks</a>). <strong>Pro fixed</strong> uses IB’s published fixed tier ($0.005/share, $1 min/order, 1% cap) with quantities from <code>fills</code> when available—otherwise <code>intended_shares</code> can inflate estimates. <code>BF_*</code> rows are excluded from totals.</p>
  {chain_blocks}
</body>
</html>"""


def make_handler(db_path: Path, default_commission_model: str = "lite"):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/", ""):
                self.send_error(404, "Not found")
                return
            qs = urllib.parse.parse_qs(parsed.query or "")
            raw = (qs.get("date") or [None])[0]
            if raw and len(raw) == 10:
                day = raw
            else:
                day = date.today().isoformat()
            raw_cm = (qs.get("comm_model") or [None])[0]
            if raw_cm in ("lite", "pro-fixed"):
                comm_model = raw_cm
            else:
                comm_model = default_commission_model
            body = _page(db_path, day, comm_model).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return H


def main() -> None:
    ap = argparse.ArgumentParser(description="Local read-only journal dashboard")
    ap.add_argument(
        "--db",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "trade_journal.db",
        help="Path to trade_journal.db",
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--commission-default",
        choices=("lite", "pro-fixed"),
        default="lite",
        help="Default commission model (form can override). lite=IBKR Lite $0 US stock/ETF IB fee.",
    )
    args = ap.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"Database not found: {args.db}")
    httpd = HTTPServer(
        (args.host, args.port),
        make_handler(args.db.resolve(), args.commission_default),
    )
    print(f"Journal dashboard: http://{args.host}:{args.port}/")
    print("Ctrl+C to stop. Read-only; safe alongside watch mode.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
