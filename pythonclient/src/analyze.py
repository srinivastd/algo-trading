#!/usr/bin/env python3
"""
Analyze Chroma backtest results: composite scoring, sensitivity analysis,
and generation of a next_experiment.json for the autoresearch loop.

Usage (from pythonclient/):
  ./py src/analyze.py --strategy groq --top 20
  ./py src/analyze.py --strategy groq --symbol QBTS --next-experiment
  ./py src/analyze.py --strategy groq --sensitivity
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chromadb


COMPOSITE_WEIGHTS = {
    "return_pct": 0.30,
    "profit_factor": 0.25,
    "win_rate": 0.20,
    "max_drawdown_inv": 0.15,
    "trade_count_norm": 0.10,
}

SWEEP_PARAM_KEYS = [
    "stop_loss_pct",
    "target_pct",
    "max_duration_hours",
    "risk_per_trade",
    "zone_lookback",
    "adx_min",
    "zone_freshness_hours",
    "volume_breakout_mult",
    "volume_confirm_mult",
    "position_size_cap",
]

PARAM_RANGES: Dict[str, Tuple[float, float, float]] = {
    "stop_loss_pct":        (0.25, 3.0,  0.25),
    "target_pct":           (2.0,  12.0, 1.0),
    "max_duration_hours":   (6,    168,  12),
    "risk_per_trade":       (500,  3000, 250),
    "zone_lookback":        (2,    8,    1),
    "adx_min":              (10,   30,   5),
    "zone_freshness_hours": (6,    48,   6),
    "volume_breakout_mult": (1.0,  1.5,  0.1),
    "volume_confirm_mult":  (1.0,  1.3,  0.05),
    "position_size_cap":    (0.5,  0.95, 0.15),
}


def _num(x: Any, default: float = 0.0) -> float:
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _ts_from_id(rid: str) -> datetime:
    try:
        part = rid.split("_", 1)[1]
        return datetime.fromisoformat(part.replace(" ", "T"))
    except Exception:
        return datetime.min


def _load_all_rows(chroma_paths: List[Path], strategy: str,
                   symbol_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    import numpy as np
    from pandas import Timestamp

    def _parse_trades(s: str) -> list:
        if not s:
            return []
        g = {"np": np, "Timestamp": Timestamp}
        try:
            v = eval(s, {"__builtins__": {}}, g)  # noqa: S307 – trusted local DB
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
        pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

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

        row: Dict[str, Any] = {
            "run_id": rid,
            "symbol": sym,
            "return_pct": round(ret, 4),
            "success_rate": _num(m.get("success_rate"), 0),
            "total_trades": int(_num(m.get("total_trades"), 0)),
            "profit_factor": round(pf, 4) if pf < 1e6 else 999.0,
            "max_drawdown_pct": round(max_dd, 4),
            "total_profit": _num(m.get("total_profit"), 0),
            "ts": _ts_from_id(rid).isoformat(),
        }
        for pk in SWEEP_PARAM_KEYS:
            row[pk] = _num(m.get(pk), 0) if pk != "zone_lookback" else int(_num(m.get(pk), 4))
        row["bar_size"] = m.get("bar_size", "")
        row["duration"] = m.get("duration", "")
        rows.append(row)
    return rows


def composite_score(row: Dict[str, Any],
                    ret_range: Tuple[float, float],
                    pf_range: Tuple[float, float],
                    dd_max: float,
                    trade_max: int) -> float:
    """0-1 composite score blending return, PF, win rate, inverse drawdown, trade volume."""

    def _norm(val: float, lo: float, hi: float) -> float:
        if hi <= lo:
            return 0.5
        return max(0.0, min(1.0, (val - lo) / (hi - lo)))

    s_ret = _norm(row["return_pct"], ret_range[0], ret_range[1])
    pf_capped = min(row["profit_factor"], 10.0)
    s_pf = _norm(pf_capped, pf_range[0], min(pf_range[1], 10.0))
    s_wr = row["success_rate"] / 100.0
    s_dd_inv = 1.0 - _norm(row["max_drawdown_pct"], 0, dd_max) if dd_max > 0 else 0.5
    s_tc = _norm(row["total_trades"], 0, trade_max) if trade_max > 0 else 0.5

    return (
        COMPOSITE_WEIGHTS["return_pct"] * s_ret
        + COMPOSITE_WEIGHTS["profit_factor"] * s_pf
        + COMPOSITE_WEIGHTS["win_rate"] * s_wr
        + COMPOSITE_WEIGHTS["max_drawdown_inv"] * s_dd_inv
        + COMPOSITE_WEIGHTS["trade_count_norm"] * s_tc
    )


def rank_with_composite(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    rets = [r["return_pct"] for r in rows]
    pfs = [r["profit_factor"] for r in rows]
    dds = [r["max_drawdown_pct"] for r in rows]
    tcs = [r["total_trades"] for r in rows]
    ret_range = (min(rets), max(rets))
    pf_range = (min(pfs), max(pfs))
    dd_max = max(dds) if dds else 1.0
    tc_max = max(tcs) if tcs else 1

    for r in rows:
        r["composite"] = round(composite_score(r, ret_range, pf_range, dd_max, tc_max), 4)
    rows.sort(key=lambda r: r["composite"], reverse=True)
    return rows


def sensitivity_report(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """For each sweepable param, compute mean composite per unique value."""
    report: Dict[str, List[Dict[str, Any]]] = {}
    for pk in SWEEP_PARAM_KEYS:
        groups: Dict[float, List[float]] = defaultdict(list)
        for r in rows:
            val = r.get(pk)
            if val is not None and val != 0:
                groups[float(val)].append(r.get("composite", 0))
        if len(groups) < 2:
            continue
        summary = []
        for v, scores in sorted(groups.items()):
            summary.append({
                "value": v,
                "count": len(scores),
                "mean_composite": round(statistics.mean(scores), 4),
                "best_composite": round(max(scores), 4),
            })
        report[pk] = summary
    return report


def propose_next_experiment(rows: List[Dict[str, Any]],
                            baseline_path: Path) -> Dict[str, Any]:
    """Pick the top-scoring run's params and perturb the weakest dimension."""
    if not rows:
        return {"error": "no rows to analyze"}

    ranked = rank_with_composite(rows)
    best = ranked[0]

    baseline: Dict[str, Any] = {}
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    sens = sensitivity_report(rows)

    best_params: Dict[str, Any] = {}
    for pk in SWEEP_PARAM_KEYS:
        best_params[pk] = best.get(pk, 0)

    weakest_param: Optional[str] = None
    weakest_spread = 0.0
    for pk, summary in sens.items():
        composites = [s["mean_composite"] for s in summary]
        if len(composites) >= 2:
            spread = max(composites) - min(composites)
            if spread > weakest_spread:
                weakest_spread = spread
                weakest_param = pk

    perturbations: Dict[str, List[float]] = {}
    if weakest_param and weakest_param in PARAM_RANGES:
        lo, hi, step = PARAM_RANGES[weakest_param]
        current = best_params.get(weakest_param, lo)
        candidates = []
        for delta in [-step, step, -2 * step, 2 * step]:
            candidate = round(current + delta, 6)
            if lo <= candidate <= hi and candidate != current:
                candidates.append(candidate)
        if candidates:
            perturbations[weakest_param] = candidates

    if not perturbations:
        for pk in SWEEP_PARAM_KEYS:
            if pk in PARAM_RANGES:
                lo, hi, step = PARAM_RANGES[pk]
                current = best_params.get(pk, lo)
                up = round(current + step, 6)
                down = round(current - step, 6)
                vals = [v for v in [down, up] if lo <= v <= hi and v != current]
                if vals:
                    perturbations[pk] = vals
                    break

    return {
        "generated_at": datetime.now().isoformat(),
        "best_run_id": best["run_id"],
        "best_composite": best["composite"],
        "best_params": best_params,
        "most_sensitive_param": weakest_param,
        "sensitivity_spread": round(weakest_spread, 4),
        "proposed_perturbations": perturbations,
        "bar_size": best.get("bar_size", ""),
        "duration": best.get("duration", ""),
    }


def update_baseline_if_improved(rows: List[Dict[str, Any]], baseline_path: Path) -> bool:
    """Compare the top-scored run against each symbol's entry in baseline.json.
    
    Returns True if baseline was updated.
    """
    if not rows or not baseline_path.exists():
        return False

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    symbols_section = baseline.setdefault("symbols", {})
    default_params = baseline.get("default_params", {})
    updated = False

    by_symbol: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_symbol[r["symbol"]].append(r)

    for sym, sym_rows in by_symbol.items():
        best = max(sym_rows, key=lambda r: r.get("composite", 0))
        existing = symbols_section.get(sym, {})
        old_composite = existing.get("best_composite", -1)

        if best.get("composite", 0) > old_composite:
            entry: Dict[str, Any] = {}
            for pk in SWEEP_PARAM_KEYS:
                entry[pk] = best.get(pk, default_params.get(pk, 0))
            entry["bar_size"] = best.get("bar_size", default_params.get("bar_size", ""))
            entry["duration"] = best.get("duration", default_params.get("duration", ""))
            entry["initial_capital"] = default_params.get("initial_capital", 50000)
            entry["best_return_pct"] = best["return_pct"]
            entry["best_profit_factor"] = best["profit_factor"]
            entry["best_max_drawdown_pct"] = best["max_drawdown_pct"]
            entry["best_win_rate"] = best["success_rate"]
            entry["best_total_trades"] = best["total_trades"]
            entry["best_composite"] = best["composite"]
            entry["source_run_id"] = best["run_id"]
            entry["updated"] = datetime.now().isoformat()
            symbols_section[sym] = entry
            updated = True
            print(f"  Baseline improved for {sym}: composite {old_composite:.4f} → {best['composite']:.4f}")

    if updated:
        baseline["version"] = baseline.get("version", 0) + 1
        baseline["updated"] = datetime.now().strftime("%Y-%m-%d")
        baseline_path.write_text(json.dumps(baseline, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"  baseline.json updated (version {baseline['version']})")
    else:
        print("  No improvement over current baseline.")
    return updated


def print_composite_table(rows: List[Dict[str, Any]], top: int) -> None:
    if not rows:
        print("No rows matched filters.")
        return
    header = (
        f"{'#':>3}  {'Symbol':<8} {'Composite':>9} {'Return%':>9} {'WinRate%':>9} "
        f"{'PF':>7} {'MaxDD%':>8} {'Trades':>6}  "
        f"{'SL%':>5} {'Tgt%':>5} {'MaxDur':>6} {'ZLB':>3} {'ADXm':>5} {'ZFH':>5}"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows[:top], 1):
        pf_s = f"{r['profit_factor']:.2f}" if r["profit_factor"] < 999 else "inf"
        print(
            f"{i:3d}  {r['symbol']:<8} {r['composite']:>9.4f} {r['return_pct']:>9.2f} "
            f"{r['success_rate']:>9.1f} {pf_s:>7} {r['max_drawdown_pct']:>8.2f} "
            f"{r['total_trades']:>6}  "
            f"{r.get('stop_loss_pct', 0):>5.2f} {r.get('target_pct', 0):>5.1f} "
            f"{r.get('max_duration_hours', 0):>6.0f} "
            f"{r.get('zone_lookback', 4):>3} {r.get('adx_min', 15):>5.1f} "
            f"{r.get('zone_freshness_hours', 12):>5.1f}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Analyze backtest results from ChromaDB")
    p.add_argument("--strategy", default="groq", choices=("groq", "mean_reversion", "legacy"))
    p.add_argument("--symbol", default=None, help="Filter to one symbol")
    p.add_argument("--top", type=int, default=30, help="How many top results to show")
    p.add_argument("--chroma-dir", default="chroma_db", help="Chroma directory relative to src/")
    p.add_argument("--sensitivity", action="store_true", help="Print sensitivity report")
    p.add_argument("--next-experiment", action="store_true", help="Generate next_experiment.json")
    p.add_argument("--update-baseline", action="store_true", help="Auto-update baseline.json if sweep improved")
    p.add_argument("--baseline", default="../baseline.json", help="Path to baseline.json (relative to src/)")
    p.add_argument("--json", action="store_true", help="Write full ranked results as JSON")
    p.add_argument("-o", "--output", default=None, help="Output path for JSON files")

    args = p.parse_args()
    src_dir = Path(__file__).resolve().parent
    chroma_path = (src_dir / args.chroma_dir).resolve()
    paths = list({str(p): p for p in [
        chroma_path,
        (src_dir / "chroma_db").resolve(),
        (src_dir.parent / "chroma_db").resolve(),
    ]}.values())

    rows = _load_all_rows(paths, args.strategy, args.symbol)
    if not rows:
        print(f"No backtest results found for strategy={args.strategy}" +
              (f" symbol={args.symbol}" if args.symbol else ""))
        return

    ranked = rank_with_composite(rows)
    print_composite_table(ranked, args.top)

    if args.sensitivity:
        print("\n=== Sensitivity Report ===")
        sens = sensitivity_report(ranked)
        if not sens:
            print("Not enough sweep variation to compute sensitivity.")
        else:
            for pk, summary in sens.items():
                print(f"\n  {pk}:")
                for s in summary:
                    print(f"    value={s['value']:>8.3f}  n={s['count']:>3}  "
                          f"mean_composite={s['mean_composite']:.4f}  best={s['best_composite']:.4f}")

    baseline_path = (src_dir / args.baseline).resolve()

    if args.update_baseline:
        print("\n=== Baseline Update Check ===")
        update_baseline_if_improved(ranked, baseline_path)

    if args.next_experiment:
        proposal = propose_next_experiment(ranked, baseline_path)
        out_path = Path(args.output) if args.output else src_dir.parent / "next_experiment.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(proposal, indent=2, default=str), encoding="utf-8")
        print(f"\nNext experiment written to {out_path.resolve()}")
        print(f"  Most sensitive param: {proposal.get('most_sensitive_param')}")
        print(f"  Proposed perturbations: {proposal.get('proposed_perturbations')}")

    if args.json:
        out_path = Path(args.output) if args.output else src_dir.parent / "analyze_results.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(ranked, indent=2, default=str), encoding="utf-8")
        print(f"\nFull ranked JSON written to {out_path.resolve()}")


if __name__ == "__main__":
    main()
