#!/usr/bin/env python3
"""
Autoresearch orchestrator: closed-loop parameter optimisation for algo-trading.

Flow per iteration:
  1. Load baseline.json (or next_experiment.json per symbol if resuming).
  2. Build a parameter sweep around the current best params.
  3. Run batch_backtest.py --sweep for ALL symbols at once.
  4. Run analyze.py per symbol: composite scoring → baseline update → next_experiment.
  5. Repeat for --iterations rounds or until --max-hours elapses.

Usage (from pythonclient/):
  ./py autoresearch.py QBTS TSLA NVDA --iterations 3
  ./py autoresearch.py QBTS TSLA NVDA PLTR SLV FCX RTX MU --iterations 5 --max-hours 6
  ./py autoresearch.py QBTS --resume --iterations 2
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Use centralized daily logging
from src.logging_config import setup_daily_logging

logger = setup_daily_logging(
    log_name="autoresearch",
    level=logging.INFO,
    console=True,
    backup_count=30,
)

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
BASELINE_PATH = ROOT / "baseline.json"
NEXT_EXP_DIR = ROOT / "runs" / "next_experiments"
LOG_PATH = ROOT / "logs" / "autoresearch_log.jsonl"


def _load_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _save_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")


def _append_log(entry: Dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _run(cmd: List[str], label: str) -> int:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  cmd: {' '.join(cmd[:8])} ...")
    print(f"{'='*60}\n", flush=True)
    proc = subprocess.run(cmd, cwd=str(SRC))
    return proc.returncode


def _next_exp_path(symbol: str) -> Path:
    return NEXT_EXP_DIR / f"{symbol.upper()}_next_experiment.json"


def build_sweep_cmd(
    symbols: List[str],
    strategy: str,
    baseline: Dict[str, Any],
    experiments: Dict[str, Dict[str, Any]],
    iteration: int,
) -> List[str]:
    """Build a batch_backtest.py --sweep command for all symbols.

    Uses the best-known params from baseline, then adds sweep values
    from experiments (per-symbol proposals merged into one grid).
    """
    default_params = baseline.get("default_params", {})

    # For multi-symbol runs we use the default_params as the base
    # and merge proposed perturbations from any symbol's experiment
    params = dict(default_params)

    # Collect proposed perturbations across all symbols
    all_perturbations: Dict[str, set] = {}
    for sym in symbols:
        exp = experiments.get(sym, {})
        for pk, vals in exp.get("proposed_perturbations", {}).items():
            if pk not in all_perturbations:
                all_perturbations[pk] = set()
            for v in vals:
                all_perturbations[pk].add(v)
        # Also pull best_params from the experiment if available
        for pk, v in exp.get("best_params", {}).items():
            if pk in all_perturbations:
                all_perturbations[pk].add(v)

    exe = sys.executable
    cmd = [
        exe, str(SRC / "batch_backtest.py"),
    ] + [s.upper() for s in symbols] + [
        "--sweep",
        "--strategy", strategy,
        "--duration", params.get("duration", "1 Y"),
        "--bar-size", params.get("bar_size", "4 hours"),
        "--initial-capital", str(params.get("initial_capital", 50000)),
        "--continue-on-error",
        "--json",
        "-o", f"../runs/sweep_iter{iteration}.csv",
    ]

    param_to_base_flag = {
        "stop_loss_pct": "--stop-loss",
        "target_pct": "--target",
        "max_duration_hours": "--max-duration",
        "risk_per_trade": "--risk-per-trade",
        "zone_lookback": "--zone-lookback",
        "adx_min": "--adx-min",
        "zone_freshness_hours": "--zone-freshness-hours",
        "volume_breakout_mult": "--volume-breakout-mult",
        "volume_confirm_mult": "--volume-confirm-mult",
        "position_size_cap": "--position-size-cap",
    }
    param_to_sweep_flag = {
        "stop_loss_pct": "--sweep-stop-loss",
        "target_pct": "--sweep-target",
        "max_duration_hours": "--sweep-max-duration",
        "risk_per_trade": "--sweep-risk",
        "zone_lookback": "--sweep-zone-lookback",
        "adx_min": "--sweep-adx-min",
        "zone_freshness_hours": "--sweep-zone-freshness",
        "volume_breakout_mult": "--sweep-vol-breakout",
        "volume_confirm_mult": "--sweep-vol-confirm",
    }

    for pk, flag in param_to_base_flag.items():
        cmd.extend([flag, str(params.get(pk, 0))])

    MAX_VALS_PER_PARAM = 3

    if all_perturbations:
        for pk, vals in all_perturbations.items():
            if pk in param_to_sweep_flag:
                current = params.get(pk)
                all_vals = sorted(set(list(vals) + ([current] if current is not None else [])))
                if len(all_vals) > MAX_VALS_PER_PARAM:
                    if current is not None and current in all_vals:
                        idx = all_vals.index(current)
                        half = MAX_VALS_PER_PARAM // 2
                        start = max(0, idx - half)
                        end = start + MAX_VALS_PER_PARAM
                        if end > len(all_vals):
                            end = len(all_vals)
                            start = max(0, end - MAX_VALS_PER_PARAM)
                        all_vals = all_vals[start:end]
                    else:
                        all_vals = all_vals[:MAX_VALS_PER_PARAM]
                cmd.extend([param_to_sweep_flag[pk], ",".join(str(v) for v in all_vals)])
    else:
        # Initial sweep: 3 SL × 3 Tgt = 9 combos per symbol
        sl = params.get("stop_loss_pct", 0.75)
        tgt = params.get("target_pct", 6.0)

        sl_vals = [round(sl - 0.25, 2), sl, round(sl + 0.25, 2)]
        tgt_vals = [round(tgt - 1.0, 1), tgt, round(tgt + 1.0, 1)]

        cmd.extend(["--sweep-stop-loss", ",".join(str(v) for v in sl_vals if v > 0)])
        cmd.extend(["--sweep-target", ",".join(str(v) for v in tgt_vals if v > 0)])

    return cmd


def run_analyze(strategy: str, symbol: str, output_path: Optional[str] = None) -> int:
    exe = sys.executable
    exp_path = _next_exp_path(symbol)
    cmd = [
        exe, str(SRC / "analyze.py"),
        "--strategy", strategy,
        "--symbol", symbol,
        "--update-baseline",
        "--next-experiment",
        "--sensitivity",
        "--json",
        "-o", str(exp_path),
    ]
    return _run(cmd, f"Analyze {symbol}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Closed-loop autoresearch: sweep → analyze → update baseline → repeat"
    )
    p.add_argument("symbols", nargs="+", help="Symbols to optimise")
    p.add_argument("--strategy", default="groq", choices=("groq", "mean_reversion"))
    p.add_argument("--iterations", type=int, default=3, help="Number of sweep→analyze rounds")
    p.add_argument("--max-hours", type=float, default=8.0, help="Hard time cap in hours")
    p.add_argument("--resume", action="store_true",
                   help="Start from per-symbol next_experiment.json instead of baseline")
    p.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    args = p.parse_args()

    symbols = [s.upper() for s in args.symbols]
    start_time = time.time()
    deadline = start_time + args.max_hours * 3600

    print(f"Autoresearch: {', '.join(symbols)} | strategy={args.strategy} | "
          f"iterations={args.iterations} | max_hours={args.max_hours}")
    print(f"Started at {datetime.now().isoformat()}")
    print(f"Symbols: {len(symbols)} | Initial grid: 3 SL × 3 Tgt = 9 combos/symbol "
          f"= {9 * len(symbols)} runs (iter 1); subsequent iters adapt via sensitivity\n")

    NEXT_EXP_DIR.mkdir(parents=True, exist_ok=True)

    for iteration in range(1, args.iterations + 1):
        if time.time() > deadline:
            print(f"\nTime cap reached ({args.max_hours}h). Stopping.")
            break

        iter_start = time.time()
        print(f"\n{'#'*60}")
        print(f"  ITERATION {iteration}/{args.iterations}")
        print(f"  Elapsed: {(iter_start - start_time) / 60:.1f} min")
        print(f"{'#'*60}")

        baseline = _load_json(BASELINE_PATH)

        experiments: Dict[str, Dict[str, Any]] = {}
        if args.resume or iteration > 1:
            for sym in symbols:
                exp = _load_json(_next_exp_path(sym))
                if exp and exp.get("proposed_perturbations"):
                    experiments[sym] = exp

        sweep_cmd = build_sweep_cmd(
            symbols, args.strategy, baseline, experiments, iteration
        )

        _append_log({
            "iteration": iteration,
            "phase": "sweep_start",
            "symbols": symbols,
            "cmd": " ".join(sweep_cmd[:12]),
            "ts": datetime.now().isoformat(),
        })

        if args.dry_run:
            print(f"\n[DRY RUN] Would execute:\n  {' '.join(sweep_cmd)}\n")
        else:
            rc = _run(sweep_cmd, f"Sweep iteration {iteration} — {len(symbols)} symbols")
            _append_log({
                "iteration": iteration,
                "phase": "sweep_done",
                "exit_code": rc,
                "ts": datetime.now().isoformat(),
            })
            if rc != 0:
                print(f"Sweep returned exit {rc} (some symbols may have failed, continuing).")

        # Analyze each symbol individually
        print(f"\n--- Analyzing {len(symbols)} symbols ---")
        for sym in symbols:
            if time.time() > deadline:
                print(f"Time cap reached during analysis. Stopping.")
                break

            if args.dry_run:
                print(f"[DRY RUN] Would analyze {sym}")
                continue

            rc = run_analyze(args.strategy, sym)
            _append_log({
                "iteration": iteration,
                "phase": "analyze_done",
                "symbol": sym,
                "exit_code": rc,
                "ts": datetime.now().isoformat(),
            })

        # Print iteration summary
        new_baseline = _load_json(BASELINE_PATH)
        print(f"\n{'='*60}")
        print(f"  ITERATION {iteration} SUMMARY")
        print(f"{'='*60}")
        print(f"  {'Symbol':<8} {'Composite':>10} {'Return%':>10} {'WinRate%':>10} {'PF':>8} {'Trades':>7}")
        print(f"  {'-'*53}")
        for sym in symbols:
            sb = new_baseline.get("symbols", {}).get(sym, {})
            comp = sb.get("best_composite", "N/A")
            ret = sb.get("best_return_pct", "N/A")
            wr = sb.get("best_win_rate", "N/A")
            pf = sb.get("best_profit_factor", "N/A")
            tc = sb.get("best_total_trades", "N/A")
            comp_s = f"{comp:.4f}" if isinstance(comp, (int, float)) else str(comp)
            ret_s = f"{ret:.2f}" if isinstance(ret, (int, float)) else str(ret)
            wr_s = f"{wr:.1f}" if isinstance(wr, (int, float)) else str(wr)
            pf_s = f"{pf:.2f}" if isinstance(pf, (int, float)) else str(pf)
            tc_s = str(tc)
            print(f"  {sym:<8} {comp_s:>10} {ret_s:>10} {wr_s:>10} {pf_s:>8} {tc_s:>7}")

            exp = _load_json(_next_exp_path(sym))
            if exp.get("most_sensitive_param"):
                print(f"           → next: perturb {exp['most_sensitive_param']} "
                      f"{exp.get('proposed_perturbations', {}).get(exp['most_sensitive_param'], [])}")

        _append_log({
            "iteration": iteration,
            "phase": "iter_summary",
            "elapsed_min": round((time.time() - iter_start) / 60, 1),
            "ts": datetime.now().isoformat(),
        })

    elapsed = (time.time() - start_time) / 60
    print(f"\n{'#'*60}")
    print(f"  AUTORESEARCH COMPLETE")
    print(f"  {args.iterations} iterations × {len(symbols)} symbols in {elapsed:.1f} minutes")
    print(f"  Baseline: {BASELINE_PATH}")
    print(f"  Logs:     {LOG_PATH}")
    print(f"{'#'*60}")


if __name__ == "__main__":
    main()
