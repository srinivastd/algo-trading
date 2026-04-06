#!/usr/bin/env python3
"""
Autoresearch orchestrator: closed-loop parameter optimisation for algo-trading.

Flow per iteration:
  1. Load baseline.json (or next_experiment.json if it exists).
  2. Build a small parameter sweep around the current best params.
  3. Run batch_backtest.py --sweep with those params.
  4. Run analyze.py --update-baseline --next-experiment --sensitivity.
  5. If composite score improved → keep new baseline, propose next sweep.
     If not improved → widen the search or mark convergence.
  6. Repeat for --iterations rounds or until --max-hours elapses.

Usage (from pythonclient/):
  ./py autoresearch.py --symbol QBTS --iterations 5
  ./py autoresearch.py --symbol QBTS --iterations 10 --strategy groq --max-hours 4
  ./py autoresearch.py --symbol QBTS --resume  # pick up from next_experiment.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
BASELINE_PATH = ROOT / "baseline.json"
NEXT_EXP_PATH = ROOT / "next_experiment.json"
LOG_PATH = ROOT / "runs" / "autoresearch_log.jsonl"


def _load_json(path: Path) -> Dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _append_log(entry: Dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _run(cmd: List[str], label: str) -> int:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  cmd: {' '.join(cmd[:6])} ...")
    print(f"{'='*60}\n")
    proc = subprocess.run(cmd, cwd=str(SRC))
    return proc.returncode


def build_sweep_args_from_experiment(experiment: Dict[str, Any],
                                     symbol: str,
                                     strategy: str) -> List[str]:
    """Turn a next_experiment.json into batch_backtest.py --sweep CLI args."""
    best = experiment.get("best_params", {})
    perturbations = experiment.get("proposed_perturbations", {})

    exe = sys.executable
    cmd = [
        exe, str(SRC / "batch_backtest.py"),
        symbol,
        "--sweep",
        "--strategy", strategy,
        "--duration", experiment.get("duration", "1 Y"),
        "--bar-size", experiment.get("bar_size", "4 hours"),
        "--continue-on-error",
        "--json",
    ]

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

    for pk, flag in param_to_base_flag.items():
        if pk in best:
            cmd.extend([flag, str(best[pk])])

    for pk, candidates in perturbations.items():
        if pk in param_to_sweep_flag:
            current = best.get(pk)
            all_vals = sorted(set([current] + candidates)) if current is not None else sorted(set(candidates))
            cmd.extend([param_to_sweep_flag[pk], ",".join(str(v) for v in all_vals)])

    return cmd


def build_initial_sweep_args(symbol: str, strategy: str,
                              baseline: Dict[str, Any]) -> List[str]:
    """Create a first sweep from baseline.json defaults."""
    params = baseline.get("symbols", {}).get(symbol, baseline.get("default_params", {}))

    exe = sys.executable
    cmd = [
        exe, str(SRC / "batch_backtest.py"),
        symbol,
        "--sweep",
        "--strategy", strategy,
        "--duration", params.get("duration", "1 Y"),
        "--bar-size", params.get("bar_size", "4 hours"),
        "--initial-capital", str(params.get("initial_capital", 50000)),
        "--continue-on-error",
        "--json",
    ]

    sl = params.get("stop_loss_pct", 0.75)
    tgt = params.get("target_pct", 6.0)
    md = params.get("max_duration_hours", 72)

    cmd.extend(["--stop-loss", str(sl)])
    cmd.extend(["--target", str(tgt)])
    cmd.extend(["--max-duration", str(md)])
    cmd.extend(["--risk-per-trade", str(params.get("risk_per_trade", 1000))])
    cmd.extend(["--zone-lookback", str(params.get("zone_lookback", 4))])
    cmd.extend(["--adx-min", str(params.get("adx_min", 15.0))])
    cmd.extend(["--zone-freshness-hours", str(params.get("zone_freshness_hours", 12.0))])
    cmd.extend(["--volume-breakout-mult", str(params.get("volume_breakout_mult", 1.1))])
    cmd.extend(["--volume-confirm-mult", str(params.get("volume_confirm_mult", 1.05))])
    cmd.extend(["--position-size-cap", str(params.get("position_size_cap", 0.95))])

    sl_vals = [round(sl - 0.25, 2), sl, round(sl + 0.25, 2)]
    tgt_vals = [round(tgt - 1.0, 1), tgt, round(tgt + 1.0, 1)]
    md_vals = [max(6, md - 12), md, md + 12]
    cmd.extend(["--sweep-stop-loss", ",".join(str(v) for v in sl_vals if v > 0)])
    cmd.extend(["--sweep-target", ",".join(str(v) for v in tgt_vals if v > 0)])
    cmd.extend(["--sweep-max-duration", ",".join(str(v) for v in md_vals)])

    return cmd


def run_analyze(strategy: str, symbol: str) -> int:
    exe = sys.executable
    cmd = [
        exe, str(SRC / "analyze.py"),
        "--strategy", strategy,
        "--symbol", symbol,
        "--update-baseline",
        "--next-experiment",
        "--sensitivity",
        "--json",
    ]
    return _run(cmd, f"Analyze results for {symbol}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Closed-loop autoresearch: sweep → analyze → update baseline → repeat"
    )
    p.add_argument("--symbol", required=True, help="Symbol to optimise")
    p.add_argument("--strategy", default="groq", choices=("groq", "mean_reversion"))
    p.add_argument("--iterations", type=int, default=3, help="Number of sweep→analyze rounds")
    p.add_argument("--max-hours", type=float, default=8.0, help="Hard time cap in hours")
    p.add_argument("--resume", action="store_true",
                   help="Start from next_experiment.json instead of baseline")
    p.add_argument("--dry-run", action="store_true", help="Print commands without executing")
    args = p.parse_args()

    start_time = time.time()
    deadline = start_time + args.max_hours * 3600

    print(f"Autoresearch: {args.symbol} | strategy={args.strategy} | "
          f"iterations={args.iterations} | max_hours={args.max_hours}")
    print(f"Started at {datetime.now().isoformat()}\n")

    for iteration in range(1, args.iterations + 1):
        if time.time() > deadline:
            print(f"\nTime cap reached ({args.max_hours}h). Stopping.")
            break

        print(f"\n{'#'*60}")
        print(f"  ITERATION {iteration}/{args.iterations}")
        print(f"{'#'*60}")

        experiment = _load_json(NEXT_EXP_PATH) if (args.resume or iteration > 1) else {}
        baseline = _load_json(BASELINE_PATH)

        if experiment and experiment.get("proposed_perturbations"):
            sweep_cmd = build_sweep_args_from_experiment(
                experiment, args.symbol, args.strategy
            )
            label = f"Sweep iteration {iteration} (from next_experiment.json)"
        else:
            sweep_cmd = build_initial_sweep_args(
                args.symbol, args.strategy, baseline
            )
            label = f"Sweep iteration {iteration} (from baseline.json)"

        _append_log({
            "iteration": iteration,
            "phase": "sweep_start",
            "symbol": args.symbol,
            "cmd": " ".join(sweep_cmd[:10]),
            "ts": datetime.now().isoformat(),
        })

        if args.dry_run:
            print(f"[DRY RUN] Would execute:\n  {' '.join(sweep_cmd)}\n")
        else:
            rc = _run(sweep_cmd, label)
            _append_log({
                "iteration": iteration,
                "phase": "sweep_done",
                "exit_code": rc,
                "ts": datetime.now().isoformat(),
            })
            if rc != 0:
                print(f"Sweep failed (exit {rc}). Check IBKR connection.")

        if not args.dry_run:
            rc = run_analyze(args.strategy, args.symbol)
            _append_log({
                "iteration": iteration,
                "phase": "analyze_done",
                "exit_code": rc,
                "ts": datetime.now().isoformat(),
            })

        new_experiment = _load_json(NEXT_EXP_PATH)
        new_baseline = _load_json(BASELINE_PATH)
        sym_baseline = new_baseline.get("symbols", {}).get(args.symbol, {})

        print(f"\n--- Iteration {iteration} summary ---")
        print(f"  Baseline composite: {sym_baseline.get('best_composite', 'N/A')}")
        print(f"  Next most-sensitive param: {new_experiment.get('most_sensitive_param', 'N/A')}")
        print(f"  Perturbations: {new_experiment.get('proposed_perturbations', {})}")

        _append_log({
            "iteration": iteration,
            "phase": "summary",
            "best_composite": sym_baseline.get("best_composite"),
            "best_return": sym_baseline.get("best_return_pct"),
            "sensitive_param": new_experiment.get("most_sensitive_param"),
            "ts": datetime.now().isoformat(),
        })

    elapsed = (time.time() - start_time) / 60
    print(f"\nAutoresearch complete. {args.iterations} iterations in {elapsed:.1f} minutes.")
    print(f"  Baseline: {BASELINE_PATH}")
    print(f"  Next experiment: {NEXT_EXP_PATH}")
    print(f"  Log: {LOG_PATH}")


if __name__ == "__main__":
    main()
