#!/usr/bin/env python3
"""
Batch-backtest many symbols via the existing main.py / Groq (or mean_reversion) flow,
rank results, and optionally sweep strategy parameters for autoresearch-style iteration.

Each run persists to ChromaDB `backtest_results` (same as a manual `main.py` run). This script
also writes CSV + JSON output for ranking and programmatic analysis.

Usage (from pythonclient/):
  cd pythonclient

  # 1. Batch backtest a watchlist
  ./.bt_run_venv/bin/python3 src/batch_backtest.py AAPL MSFT QBTS --strategy groq \\
    --duration "1 Y" --bar-size "4 hours" --risk-per-trade 1000 --max-duration 72 \\
    --stop-loss 0.75 --target 6.0 --initial-capital 50000

  # 2. Batch from file
  ./.bt_run_venv/bin/python3 src/batch_backtest.py -f watchlist.txt -o runs/batch.csv

  # 3. Rank all Chroma results (no new runs)
  ./.bt_run_venv/bin/python3 src/batch_backtest.py --rank --strategy groq --top 20

  # 4. Parameter sweep (autoresearch-style)
  ./.bt_run_venv/bin/python3 src/batch_backtest.py QBTS --sweep \\
    --sweep-stop-loss 0.5,0.75,1.0 --sweep-target 4.0,6.0,8.0 \\
    --duration "1 Y" --bar-size "4 hours"

  # 5. Verify Chroma schema
  ./.bt_run_venv/bin/python3 src/batch_backtest.py --verify-schema
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json as json_mod
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chromadb


def _ts_from_id(rid: str) -> datetime:
    try:
        part = rid.split("_", 1)[1]
        return datetime.fromisoformat(part.replace(" ", "T"))
    except Exception:
        return datetime.min


def _num(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def verify_chroma_schema(chroma_path: Path) -> None:
    """Print whether expected metadata keys exist (reads latest row if any)."""
    expected = {
        "symbol",
        "strategy",
        "success_rate",
        "total_trades",
        "initial_capital",
        "final_capital",
        "total_profit",
        "risk_per_trade",
        "stop_loss_pct",
        "target_pct",
        "max_duration_hours",
        "bar_size",
        "duration",
    }
    client = chromadb.PersistentClient(path=str(chroma_path))
    coll = client.get_collection(name="backtest_results")
    raw = coll.get(include=["metadatas"])
    if not raw["ids"]:
        print(f"No rows in {chroma_path} — run one backtest first.")
        return
    latest_i = max(range(len(raw["ids"])), key=lambda i: _ts_from_id(raw["ids"][i]))
    meta = raw["metadatas"][latest_i] or {}
    keys = set(meta.keys())
    print(f"Chroma path: {chroma_path}")
    print(f"Sample id: {raw['ids'][latest_i]}")
    print(f"Metadata keys ({len(keys)}): {sorted(keys)}")
    missing = expected - keys
    extra_ok = keys - expected - {"trades", "paper_trading", "volatility", "adx", "sma_20", "sma_50"}
    if missing:
        print(f"Missing vs checklist (may be older runs): {sorted(missing)}")
    else:
        print("Checklist fields present for Groq-style rows.")
    print(f"(Other keys are OK: indicators, trades string, etc. Count: {len(extra_ok)} feature-like keys.)")


def latest_row_for_symbol(
    chroma_path: Path, symbol: str, strategy: str
) -> Optional[Tuple[str, Dict[str, Any]]]:
    client = chromadb.PersistentClient(path=str(chroma_path))
    coll = client.get_collection(name="backtest_results")
    raw = coll.get(include=["metadatas"])
    sym_u = symbol.upper()
    best: Optional[Tuple[str, Dict[str, Any], datetime]] = None
    for i, rid in enumerate(raw["ids"]):
        m = raw["metadatas"][i] or {}
        if str(m.get("symbol", "")).upper() != sym_u:
            continue
        if str(m.get("strategy") or "legacy") != strategy:
            continue
        ts = _ts_from_id(rid)
        if best is None or ts > best[2]:
            best = (rid, m, ts)
    if best is None:
        return None
    return best[0], dict(best[1])


def row_dict_from_chroma(
    run_id: str, meta: Dict[str, Any], symbol: str, strategy: str, status: str, error: str
) -> Dict[str, Any]:
    ic = _num(meta.get("initial_capital"), 0)
    fc = _num(meta.get("final_capital"), 0)
    ret = ((fc - ic) / ic * 100.0) if ic > 0 else 0.0
    return {
        "run_id": run_id,
        "status": status,
        "error": error,
        "symbol": symbol.upper(),
        "strategy": strategy,
        "return_pct": round(ret, 6),
        "success_rate": meta.get("success_rate", ""),
        "total_trades": meta.get("total_trades", ""),
        "total_profit": meta.get("total_profit", ""),
        "initial_capital": meta.get("initial_capital", ""),
        "final_capital": meta.get("final_capital", ""),
        "risk_per_trade": meta.get("risk_per_trade", ""),
        "stop_loss_pct": meta.get("stop_loss_pct", ""),
        "target_pct": meta.get("target_pct", ""),
        "max_duration_hours": meta.get("max_duration_hours", ""),
        "bar_size": meta.get("bar_size", ""),
        "duration": meta.get("duration", ""),
    }


CSV_FIELDS = [
    "run_id",
    "status",
    "error",
    "symbol",
    "strategy",
    "return_pct",
    "success_rate",
    "total_trades",
    "total_profit",
    "initial_capital",
    "final_capital",
    "risk_per_trade",
    "stop_loss_pct",
    "target_pct",
    "max_duration_hours",
    "bar_size",
    "duration",
]


def load_symbols(args: argparse.Namespace) -> List[str]:
    out: List[str] = []
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
        for line in text.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s.split()[0])
    out.extend(args.symbols)
    # dedupe preserving order
    seen = set()
    uniq: List[str] = []
    for s in out:
        u = s.strip().upper()
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def build_main_argv(
    src_dir: Path,
    symbol: str,
    strategy: str,
    duration: str,
    bar_size: str,
    risk: float,
    max_duration: float,
    stop_loss: float,
    target: float,
    initial_capital: float,
    force_refresh: bool,
    client_id: int,
    debug: bool,
    zone_lookback: int = 4,
    zone_lookback_hours: Optional[float] = None,
    adx_min: float = 15.0,
    zone_freshness_hours: float = 12.0,
    volume_breakout_mult: float = 1.1,
    volume_confirm_mult: float = 1.05,
    position_size_cap: float = 0.95,
    historical_data_dir: Optional[str] = None,
    bt_start_day: Optional[str] = None,
    bt_end_day: Optional[str] = None,
) -> List[str]:
    exe = sys.executable
    main_py = src_dir / "main.py"
    cmd = [
        exe,
        str(main_py),
        symbol,
        "--backtest-only",
        "--strategy",
        strategy,
        "--duration",
        duration,
        "--bar-size",
        bar_size,
        "--risk-per-trade",
        str(risk),
        "--max-duration",
        str(max_duration),
        "--stop-loss",
        str(stop_loss),
        "--target",
        str(target),
        "--initial-capital",
        str(initial_capital),
        "--client-id",
        str(client_id),
    ]
    if zone_lookback_hours is not None:
        cmd.extend(["--zone-lookback-hours", str(zone_lookback_hours)])
    else:
        cmd.extend(["--zone-lookback", str(zone_lookback)])
    cmd.extend(
        [
        "--adx-min",
        str(adx_min),
        "--zone-freshness-hours",
        str(zone_freshness_hours),
        "--volume-breakout-mult",
        str(volume_breakout_mult),
        "--volume-confirm-mult",
        str(volume_confirm_mult),
        "--position-size-cap",
        str(position_size_cap),
        ]
    )
    if bt_start_day and bt_end_day:
        cmd.extend(["--bt-start-day", str(bt_start_day), "--bt-end-day", str(bt_end_day)])
    if force_refresh:
        cmd.append("--force-refresh")
    if debug:
        cmd.append("--debug")
    pc_root = src_dir.resolve().parent
    hdc = historical_data_dir if historical_data_dir is not None else str((pc_root / "historical_data").resolve())
    cmd.extend(["--historical-data-dir", hdc])
    return cmd


def rank_from_chroma(chroma_paths: List[Path], strategy: str, top: int, symbol_filter: Optional[str]) -> List[Dict[str, Any]]:
    """Load all Chroma rows, compute profit factor, and return ranked dicts."""
    import ast
    import numpy as np
    from pandas import Timestamp

    def _parse_trades(s: str) -> list:
        if not s:
            return []
        g = {"np": np, "Timestamp": Timestamp}
        try:
            v = eval(s, {"__builtins__": {}}, g)  # noqa: S307
            return v if isinstance(v, list) else []
        except Exception:
            return []

    merged: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for cp in chroma_paths:
        try:
            coll = chromadb.PersistentClient(path=str(cp)).get_collection("backtest_results")
            raw = coll.get(include=["metadatas"])
            for i, rid in enumerate(raw["ids"]):
                if rid not in merged:
                    merged[rid] = (str(cp), raw["metadatas"][i] or {})
        except Exception:
            pass

    rows: List[Dict[str, Any]] = []
    for rid, (path, m) in merged.items():
        strat = str(m.get("strategy") or "legacy")
        if strat != strategy:
            continue
        sym = str(m.get("symbol", "")).upper()
        if symbol_filter and sym != symbol_filter.upper():
            continue
        ic = _num(m.get("initial_capital"), 0)
        fc = _num(m.get("final_capital"), 0)
        ret = ((fc - ic) / ic * 100.0) if ic > 0 else 0.0

        trades_raw = m.get("trades", "")
        trades = _parse_trades(trades_raw) if isinstance(trades_raw, str) else []
        gross_win = sum(t.get("profit", 0) for t in trades if isinstance(t, dict) and t.get("profit", 0) > 0)
        gross_loss = abs(sum(t.get("profit", 0) for t in trades if isinstance(t, dict) and t.get("profit", 0) < 0))
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

        max_dd = 0.0
        if trades:
            equity = ic
            peak = ic
            for t in trades:
                if isinstance(t, dict):
                    equity += t.get("profit", 0)
                    peak = max(peak, equity)
                    dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
                    max_dd = max(max_dd, dd)

        rows.append({
            "run_id": rid,
            "chroma_path": path,
            "symbol": sym,
            "strategy": strat,
            "return_pct": round(ret, 4),
            "success_rate": _num(m.get("success_rate"), 0),
            "total_trades": int(_num(m.get("total_trades"), 0)),
            "profit_factor": round(pf, 4),
            "max_drawdown_pct": round(max_dd, 4),
            "total_profit": _num(m.get("total_profit"), 0),
            "risk_per_trade": _num(m.get("risk_per_trade"), 0),
            "stop_loss_pct": _num(m.get("stop_loss_pct"), 0),
            "target_pct": _num(m.get("target_pct"), 0),
            "max_duration_hours": _num(m.get("max_duration_hours"), 0),
            "bar_size": m.get("bar_size", ""),
            "duration": m.get("duration", ""),
            "adx": _num(m.get("adx"), 0),
            "rsi": _num(m.get("rsi"), 0),
            "volatility": _num(m.get("volatility"), 0),
            "ts": _ts_from_id(rid).isoformat(),
        })

    rows.sort(key=lambda r: r["return_pct"], reverse=True)
    return rows[:top]


def print_rank_table(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("No rows matched filters.")
        return
    header = f"{'#':>3}  {'Symbol':<8} {'Return%':>9} {'WinRate%':>9} {'PF':>7} {'MaxDD%':>8} {'Trades':>6}  {'SL%':>5} {'Tgt%':>5} {'MaxDur':>6} {'Bar':>8} {'Dur':>5}"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows, 1):
        pf_s = f"{r['profit_factor']:.2f}" if r["profit_factor"] < 1e6 else "inf"
        print(
            f"{i:3d}  {r['symbol']:<8} {r['return_pct']:>9.2f} {r['success_rate']:>9.1f} {pf_s:>7} "
            f"{r['max_drawdown_pct']:>8.2f} {r['total_trades']:>6}  "
            f"{r['stop_loss_pct']:>5.2f} {r['target_pct']:>5.1f} {r['max_duration_hours']:>6.0f} "
            f"{r['bar_size']:>8} {r['duration']:>5}"
        )


def run_single_backtest(
    src_dir: Path,
    symbol: str,
    strategy: str,
    duration: str,
    bar_size: str,
    risk: float,
    max_duration: float,
    stop_loss: float,
    target: float,
    initial_capital: float,
    force_refresh: bool,
    client_id: int,
    debug: bool,
    chroma_path: Path,
    continue_on_error: bool,
    zone_lookback: int = 4,
    zone_lookback_hours: Optional[float] = None,
    adx_min: float = 15.0,
    zone_freshness_hours: float = 12.0,
    volume_breakout_mult: float = 1.1,
    volume_confirm_mult: float = 1.05,
    position_size_cap: float = 0.95,
    historical_data_dir: Optional[str] = None,
    bt_start_day: Optional[str] = None,
    bt_end_day: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one main.py backtest, read Chroma, return a result dict."""
    cmd = build_main_argv(
        src_dir, symbol, strategy, duration, bar_size,
        risk, max_duration, stop_loss, target, initial_capital,
        force_refresh, client_id, debug,
        zone_lookback=zone_lookback,
        zone_lookback_hours=zone_lookback_hours,
        adx_min=adx_min,
        zone_freshness_hours=zone_freshness_hours,
        volume_breakout_mult=volume_breakout_mult,
        volume_confirm_mult=volume_confirm_mult,
        position_size_cap=position_size_cap,
        historical_data_dir=historical_data_dir,
        bt_start_day=bt_start_day,
        bt_end_day=bt_end_day,
    )
    label = f"{symbol} SL={stop_loss} T={target} maxDur={max_duration}"
    print(f"\n=== {label} ===", flush=True)
    proc = subprocess.run(cmd, cwd=str(src_dir), text=True)
    err = ""
    status = "ok" if proc.returncode == 0 else "error"
    if proc.returncode != 0:
        err = f"exit_code={proc.returncode}"
        print(f"ERROR {label}: {err}", flush=True)

    meta: Dict[str, Any] = {}
    run_id = ""
    if status == "ok":
        got = latest_row_for_symbol(chroma_path, symbol, strategy)
        if got:
            run_id, meta = got
        else:
            err = (err + "; ").strip() + "chroma_row_not_found"
            status = "error"

    row = row_dict_from_chroma(run_id, meta, symbol, strategy, status, err)
    row["profit_factor"] = ""
    row["max_drawdown_pct"] = ""
    return row


def main() -> None:
    p = argparse.ArgumentParser(
        description="Batch backtest, rank, or sweep params via main.py; Chroma + CSV + JSON"
    )
    p.add_argument("symbols", nargs="*", help="Ticker symbols")
    p.add_argument("-f", "--file", help="File with one symbol per line (# comments ok)")
    p.add_argument(
        "-o", "--output", default="batch_backtest_results.csv",
        help="Append CSV path (relative to cwd)",
    )
    p.add_argument(
        "--chroma-dir", default="chroma_db",
        help="Chroma directory relative to src/",
    )
    p.add_argument("--strategy", default="groq", choices=("groq", "mean_reversion", "legacy"))
    p.add_argument("--duration", default="1 M")
    p.add_argument("--bar-size", default="15 mins")
    p.add_argument("--risk-per-trade", type=float, default=1000.0)
    p.add_argument("--max-duration", type=float, default=12.0)
    p.add_argument("--stop-loss", type=float, default=1.0)
    p.add_argument("--target", type=float, default=4.0)
    p.add_argument("--initial-capital", type=float, default=50000.0)
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--client-id", type=int, default=1)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--zone-lookback", type=int, default=4)
    p.add_argument(
        "--zone-lookback-hours",
        type=float,
        default=None,
        metavar="H",
        help="If set, pass --zone-lookback-hours to main (groq clock-normalized LB); excludes --zone-lookback.",
    )
    p.add_argument("--adx-min", type=float, default=15.0)
    p.add_argument("--zone-freshness-hours", type=float, default=12.0)
    p.add_argument("--volume-breakout-mult", type=float, default=1.1)
    p.add_argument("--volume-confirm-mult", type=float, default=1.05)
    p.add_argument("--position-size-cap", type=float, default=0.95)
    p.add_argument("--continue-on-error", action="store_true", help="Keep batch after a failed symbol")
    p.add_argument(
        "--historical-data-dir",
        default=None,
        help="Historical CSV cache directory (default: pythonclient/historical_data)",
    )
    p.add_argument("--verify-schema", action="store_true", help="Print Chroma metadata checklist and exit")
    p.add_argument("--json", action="store_true", help="Also write a .json sidecar with full results")

    rank_g = p.add_argument_group("Ranking (read-only, no new runs)")
    rank_g.add_argument("--rank", action="store_true", help="Rank existing Chroma results and exit")
    rank_g.add_argument("--top", type=int, default=30, help="How many top results to show")
    rank_g.add_argument("--rank-symbol", default=None, help="Filter rank to one symbol")

    sweep_g = p.add_argument_group("Parameter sweep (autoresearch-style)")
    sweep_g.add_argument("--sweep", action="store_true", help="Run parameter grid sweep")
    sweep_g.add_argument("--sweep-stop-loss", default=None, help="Comma-separated stop loss values")
    sweep_g.add_argument("--sweep-target", default=None, help="Comma-separated target pct values")
    sweep_g.add_argument("--sweep-max-duration", default=None, help="Comma-separated max duration values")
    sweep_g.add_argument("--sweep-risk", default=None, help="Comma-separated risk per trade values")
    sweep_g.add_argument("--sweep-zone-lookback", default=None, help="Comma-separated zone lookback values")
    sweep_g.add_argument("--sweep-adx-min", default=None, help="Comma-separated ADX min values")
    sweep_g.add_argument("--sweep-zone-freshness", default=None, help="Comma-separated zone freshness hours")
    sweep_g.add_argument("--sweep-vol-breakout", default=None, help="Comma-separated volume breakout multipliers")
    sweep_g.add_argument("--sweep-vol-confirm", default=None, help="Comma-separated volume confirm multipliers")

    args = p.parse_args()

    src_dir = Path(__file__).resolve().parent
    chroma_path = (src_dir / args.chroma_dir).resolve()
    hist_root = (
        Path(args.historical_data_dir).resolve()
        if args.historical_data_dir
        else (src_dir.parent / "historical_data").resolve()
    )
    hist_root.mkdir(parents=True, exist_ok=True)

    if args.verify_schema:
        verify_chroma_schema(chroma_path)
        return

    if args.rank:
        paths = [chroma_path, (src_dir / "chroma_db").resolve(), (src_dir.parent / "chroma_db").resolve()]
        paths = list({str(p): p for p in paths}.values())
        rows = rank_from_chroma(paths, args.strategy, args.top, args.rank_symbol)
        print_rank_table(rows)
        if args.json:
            out = Path(args.output).with_suffix(".rank.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json_mod.dumps(rows, indent=2, default=str), encoding="utf-8")
            print(f"\nJSON written to {out.resolve()}")
        return

    symbols = load_symbols(args)
    if not symbols:
        p.error("Provide symbols or -f/--file")

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_path.exists() or output_path.stat().st_size == 0

    all_rows: List[Dict[str, Any]] = []

    if args.sweep:
        sl_vals = [float(x) for x in args.sweep_stop_loss.split(",")] if args.sweep_stop_loss else [args.stop_loss]
        tgt_vals = [float(x) for x in args.sweep_target.split(",")] if args.sweep_target else [args.target]
        dur_vals = [float(x) for x in args.sweep_max_duration.split(",")] if args.sweep_max_duration else [args.max_duration]
        risk_vals = [float(x) for x in args.sweep_risk.split(",")] if args.sweep_risk else [args.risk_per_trade]
        zlb_vals = [int(x) for x in args.sweep_zone_lookback.split(",")] if args.sweep_zone_lookback else [args.zone_lookback]
        adx_vals = [float(x) for x in args.sweep_adx_min.split(",")] if args.sweep_adx_min else [args.adx_min]
        zfh_vals = [float(x) for x in args.sweep_zone_freshness.split(",")] if args.sweep_zone_freshness else [args.zone_freshness_hours]
        vb_vals = [float(x) for x in args.sweep_vol_breakout.split(",")] if args.sweep_vol_breakout else [args.volume_breakout_mult]
        vc_vals = [float(x) for x in args.sweep_vol_confirm.split(",")] if args.sweep_vol_confirm else [args.volume_confirm_mult]
        grid = list(itertools.product(
            symbols, sl_vals, tgt_vals, dur_vals, risk_vals,
            zlb_vals, adx_vals, zfh_vals, vb_vals, vc_vals,
        ))
        print(f"Sweep: {len(grid)} combinations")
        for sym, sl, tgt, md, rk, zlb, adxm, zfh, vb, vc in grid:
            row = run_single_backtest(
                src_dir, sym, args.strategy, args.duration, args.bar_size,
                rk, md, sl, tgt, args.initial_capital,
                args.force_refresh, args.client_id, args.debug,
                chroma_path, args.continue_on_error,
                zone_lookback=zlb,
                zone_lookback_hours=args.zone_lookback_hours,
                adx_min=adxm,
                zone_freshness_hours=zfh,
                volume_breakout_mult=vb,
                volume_confirm_mult=vc,
                position_size_cap=args.position_size_cap,
                historical_data_dir=str(hist_root),
            )
            all_rows.append(row)
            with output_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if write_header:
                    w.writeheader()
                    write_header = False
                w.writerow(row)
            if row["status"] != "ok" and not args.continue_on_error:
                break
    else:
        for symbol in symbols:
            row = run_single_backtest(
                src_dir, symbol, args.strategy, args.duration, args.bar_size,
                args.risk_per_trade, args.max_duration, args.stop_loss, args.target,
                args.initial_capital, args.force_refresh, args.client_id, args.debug,
                chroma_path, args.continue_on_error,
                zone_lookback=args.zone_lookback,
                zone_lookback_hours=args.zone_lookback_hours,
                adx_min=args.adx_min,
                zone_freshness_hours=args.zone_freshness_hours,
                volume_breakout_mult=args.volume_breakout_mult,
                volume_confirm_mult=args.volume_confirm_mult,
                position_size_cap=args.position_size_cap,
                historical_data_dir=str(hist_root),
            )
            all_rows.append(row)
            with output_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if write_header:
                    w.writeheader()
                    write_header = False
                w.writerow(row)
            if row["status"] != "ok" and not args.continue_on_error:
                break

    print(f"\nWrote {len(all_rows)} row(s) to {output_path.resolve()}")

    if args.json:
        json_path = output_path.with_suffix(".json")
        json_path.write_text(json_mod.dumps(all_rows, indent=2, default=str), encoding="utf-8")
        print(f"JSON written to {json_path.resolve()}")

    if all_rows:
        ok_rows = [r for r in all_rows if r["status"] == "ok"]
        if ok_rows:
            best = max(ok_rows, key=lambda r: float(r.get("return_pct", 0)))
            print(f"\nBest: {best['symbol']} → {best['return_pct']}% return "
                  f"(SL={best.get('stop_loss_pct')} Tgt={best.get('target_pct')} MaxDur={best.get('max_duration_hours')})")


if __name__ == "__main__":
    main()
