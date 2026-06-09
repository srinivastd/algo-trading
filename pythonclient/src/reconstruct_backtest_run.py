#!/usr/bin/env python3
"""
Look up a Chroma backtest run and emit git commit + replay command.

Chroma does not store git SHA — we infer the last commit before the run timestamp.

Usage (from pythonclient/):
  PYTHONPATH=src ./py src/reconstruct_backtest_run.py --days 31 --top 5
  PYTHONPATH=src ./py src/reconstruct_backtest_run.py --id "QBTS_2026-05-12 22:17:11.925045"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import chromadb

REPO_ROOT = Path(__file__).resolve().parents[2]
CHROMA = Path(__file__).resolve().parent / "chroma_db"

REPLAY_KEYS = (
    "symbol",
    "strategy",
    "duration",
    "bar_size",
    "zone_lookback",
    "zone_lookback_hours_requested",
    "risk_per_trade",
    "stop_loss_pct",
    "target_pct",
    "max_duration_hours",
    "adx_min",
    "zone_freshness_hours",
    "volume_breakout_mult",
    "volume_confirm_mult",
    "position_size_cap",
    "min_hold_hours",
    "long_min_rsi",
    "short_realtime_filters_enabled",
)


def _ts_from_id(rid: str) -> datetime:
    try:
        return datetime.fromisoformat(rid.split("_", 1)[1].replace(" ", "T"))
    except Exception:
        return datetime.min


def _git_commit_before(ts: datetime) -> str:
    """Last repo commit at or before run time (best-effort code version)."""
    until = ts.strftime("%Y-%m-%d %H:%M:%S %z").strip()
    if not until.endswith(("+", "-")):
        # Assume US/Pacific if tz missing (matches journal/backtest timestamps).
        until += " -0700"
    cmd = [
        "git", "log", "-1", "--format=%H %h %ad %s",
        f"--until={until}",
        "--date=iso-local",
    ]
    try:
        out = subprocess.check_output(cmd, cwd=str(REPO_ROOT), text=True, stderr=subprocess.DEVNULL).strip()
        return out or "(no commit found before run time)"
    except subprocess.CalledProcessError:
        return "(git lookup failed)"


def _replay_command(meta: dict) -> str:
    sym = meta.get("symbol", "SYMBOL")
    parts = [
        "PYTHONPATH=src ./py src/main.py",
        str(sym),
        "--backtest-only",
        "--strategy",
        str(meta.get("strategy") or "groq"),
        "--duration",
        str(meta.get("duration") or "1 Y"),
        "--bar-size",
        f'"{meta.get("bar_size") or "1 hour"}"',
        "--risk-per-trade",
        str(meta.get("risk_per_trade") or 2000),
        "--stop-loss",
        str(meta.get("stop_loss_pct") or 1.1),
        "--target",
        str(meta.get("target_pct") or 4.8),
        "--max-duration",
        str(meta.get("max_duration_hours") or 12),
        "--adx-min",
        str(meta.get("adx_min") or 28.5),
        "--zone-lookback",
        str(meta.get("zone_lookback") or 7),
        "--zone-freshness-hours",
        str(meta.get("zone_freshness_hours") or 8.0),
        "--volume-breakout-mult",
        str(meta.get("volume_breakout_mult") or 1.36),
        "--volume-confirm-mult",
        str(meta.get("volume_confirm_mult") or 1.19),
        "--position-size-cap",
        str(meta.get("position_size_cap") or 0.95),
        "--min-hold-hours",
        str(meta.get("min_hold_hours") or 2.0),
        "--no-journal",
    ]
    zlh = meta.get("zone_lookback_hours_requested")
    if zlh is not None and float(zlh) > 0:
        parts.extend(["--zone-lookback-hours", str(float(zlh))])
    if meta.get("long_min_rsi") is not None:
        parts.extend(["--long-min-rsi", str(meta.get("long_min_rsi"))])
    srf = meta.get("short_realtime_filters_enabled")
    if srf in (1, True, "1", "true", "True"):
        parts.append("--short-realtime-filters")
    return " \\\n  ".join(parts)


def _profit(meta: dict) -> float:
    ic = float(meta.get("initial_capital") or 0)
    fc = float(meta.get("final_capital") or 0)
    if ic > 0:
        return fc - ic
    return float(meta.get("total_profit") or 0)


def _return_pct(meta: dict) -> float:
    ic = float(meta.get("initial_capital") or 0)
    fc = float(meta.get("final_capital") or 0)
    return ((fc - ic) / ic * 100.0) if ic > 0 else 0.0


def load_runs(chroma_path: Path) -> list[tuple[str, dict, datetime]]:
    client = chromadb.PersistentClient(path=str(chroma_path))
    coll = client.get_collection("backtest_results")
    raw = coll.get(include=["metadatas"])
    rows: list[tuple[str, dict, datetime]] = []
    for i, rid in enumerate(raw["ids"]):
        m = dict(raw["metadatas"][i] or {})
        rows.append((rid, m, _ts_from_id(rid)))
    return rows


def format_run(rid: str, meta: dict, ts: datetime, *, verbose: bool = False) -> str:
    profit = _profit(meta)
    ret = _return_pct(meta)
    commit = _git_commit_before(ts)
    lines = [
        f"ID:       {rid}",
        f"Run time: {ts.isoformat(sep=' ', timespec='seconds')}",
        f"Symbol:   {meta.get('symbol')}  strategy={meta.get('strategy')}",
        f"Result:   ${profit:,.2f} profit  ({ret:.2f}% return)  trades={meta.get('total_trades')}  win_rate={meta.get('success_rate')}%",
        f"Params:   bar={meta.get('bar_size')}  dur={meta.get('duration')}  zl={meta.get('zone_lookback')}  "
        f"zlh_req={meta.get('zone_lookback_hours_requested')}  risk={meta.get('risk_per_trade')}  "
        f"sl={meta.get('stop_loss_pct')}  tgt={meta.get('target_pct')}  adx_min={meta.get('adx_min')}",
        f"Git:      {commit}",
        f"Checkout: git checkout $(git log -1 --format=%h --until='{ts.strftime('%Y-%m-%d %H:%M:%S')} -0700')",
        f"Strategy: git log -1 --format=%h --until='{ts.strftime('%Y-%m-%d %H:%M:%S')} -0700' -- pythonclient/src/strategies/groq_recommendations_strategy.py",
    ]
    export = meta.get("confidence_trades_export")
    if export:
        lines.append(f"Trades:   {export}")
    lines.append("Replay:\n  " + _replay_command(meta))
    if verbose:
        subset = {k: meta.get(k) for k in REPLAY_KEYS if k in meta}
        lines.append("Metadata:\n" + json.dumps(subset, indent=2, default=str))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Reconstruct git version + replay cmd from Chroma backtest")
    p.add_argument("--chroma-dir", default=str(CHROMA))
    p.add_argument("--days", type=int, default=31, help="Lookback window when ranking by profit")
    p.add_argument("--top", type=int, default=1, help="Show top N runs by profit in window")
    p.add_argument("--id", dest="run_id", help="Exact Chroma run id")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    rows = load_runs(Path(args.chroma_dir))
    if args.run_id:
        match = [(rid, m, ts) for rid, m, ts in rows if rid == args.run_id]
        if not match:
            print(f"No run with id: {args.run_id}", file=sys.stderr)
            sys.exit(1)
        selected = match
    else:
        cutoff = datetime.now() - timedelta(days=args.days)
        recent = [(rid, m, ts) for rid, m, ts in rows if ts >= cutoff]
        recent.sort(key=lambda x: _profit(x[1]), reverse=True)
        selected = recent[: max(1, args.top)]
        if not selected:
            print(f"No runs in the last {args.days} days.", file=sys.stderr)
            sys.exit(1)

    if args.json:
        payload = []
        for rid, m, ts in selected:
            payload.append({
                "id": rid,
                "run_time": ts.isoformat(),
                "profit": _profit(m),
                "return_pct": _return_pct(m),
                "git_commit_line": _git_commit_before(ts),
                "replay_command": _replay_command(m),
                "params": {k: m.get(k) for k in REPLAY_KEYS if k in m},
                "trades_export": m.get("confidence_trades_export"),
            })
        print(json.dumps(payload if len(payload) > 1 else payload[0], indent=2))
        return

    for i, (rid, m, ts) in enumerate(selected):
        if i:
            print("\n" + "=" * 72 + "\n")
        print(format_run(rid, m, ts, verbose=args.verbose))


if __name__ == "__main__":
    main()
