#!/usr/bin/env python3
"""
Look up paper-trade journal runs and reconstruct the git commit + launch command.

Past runs without ``code_provenance`` infer commit from ``started_at`` via git log.

Usage (from pythonclient/):
  PYTHONPATH=src ./py src/reconstruct_paper_run.py --days 31 --top 10
  PYTHONPATH=src ./py src/reconstruct_paper_run.py --run-id RUN_20260513_133139_c2c76ffc
  PYTHONPATH=src ./py src/reconstruct_paper_run.py --days 31 --top 5 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from git_provenance import infer_git_provenance_before
from trade_journal import TradeJournal

DEFAULT_DB = Path(__file__).resolve().parent.parent / "trade_journal.db"

PAPER_LAUNCH_KEYS = (
    "bar_size",
    "duration",
    "zone_lookback",
    "zone_lookback_hours_requested",
    "risk_per_trade",
    "stop_loss_pct",
    "target_pct",
    "max_duration_hours",
    "adx_min",
    "min_entry_confidence",
    "max_entry_zone_age_hours",
    "check_interval",
    "daily_unrealized_profit_lock_pct",
)


def _parse_ts(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _provenance_for_run(run: dict[str, Any]) -> dict[str, Any]:
    stored = run.get("code_provenance")
    if isinstance(stored, dict) and stored.get("available"):
        return stored
    if run.get("git_commit"):
        return {
            "available": True,
            "commit": run["git_commit"],
            "commit_short": run.get("git_commit_short") or str(run["git_commit"])[:12],
            "branch": run.get("git_branch"),
            "dirty": bool(run.get("git_dirty")) if run.get("git_dirty") is not None else None,
            "inferred": False,
        }
    return infer_git_provenance_before(str(run.get("started_at") or ""))


def _run_pnl_stats(con, run_id: str) -> dict[str, Any]:
    row = con.execute(
        """
        SELECT
            COUNT(*) AS trades,
            SUM(CASE WHEN LOWER(status) = 'closed' THEN 1 ELSE 0 END) AS closed,
            SUM(CASE WHEN LOWER(status) = 'closed' THEN COALESCE(realized_pnl, 0) ELSE 0 END) AS realized_pnl,
            SUM(CASE WHEN LOWER(status) = 'open' THEN 1 ELSE 0 END) AS open_trades
        FROM trades WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return dict(row) if row else {"trades": 0, "closed": 0, "realized_pnl": 0.0, "open_trades": 0}


def _paper_watch_command(run: dict[str, Any]) -> str:
    params = run.get("params") or {}
    symbols = run.get("symbols") or []
    sym_str = " ".join(str(s).upper() for s in symbols) or "SYMBOL"
    strategy = run.get("strategy") or "groq"
    parts = [
        "PYTHONPATH=src ./py src/main.py",
        sym_str,
        f"--strategy {strategy}",
        "--paper",
        "--watch",
    ]
    if params.get("bar_size"):
        parts.append(f'--bar-size "{params["bar_size"]}"')
    if params.get("duration"):
        parts.append(f'--duration "{params["duration"]}"')
    if params.get("zone_lookback_hours_requested") is not None:
        parts.append(f'--zone-lookback-hours {params["zone_lookback_hours_requested"]}')
    elif params.get("zone_lookback") is not None:
        parts.append(f"--zone-lookback {int(params['zone_lookback'])}")
    for key, flag in (
        ("risk_per_trade", "--risk-per-trade"),
        ("stop_loss_pct", "--stop-loss"),
        ("target_pct", "--target"),
        ("max_duration_hours", "--max-duration"),
        ("adx_min", "--adx-min"),
        ("check_interval", "--check-interval"),
    ):
        if params.get(key) is not None:
            parts.append(f"{flag} {params[key]}")
    if params.get("min_entry_confidence") is not None:
        parts.append(f"--min-entry-confidence {params['min_entry_confidence']}")
    mza = params.get("max_entry_zone_age_hours")
    if mza is not None and float(mza) > 0:
        parts.append(f"--max-entry-zone-age-hours {mza}")
    elif mza is None and "max_entry_zone_age_hours" in params:
        parts.append("--max-entry-zone-age-hours 0")
    dpl = params.get("daily_unrealized_profit_lock_pct")
    if dpl is not None:
        parts.append(f"--daily-unrealized-profit-lock-pct {dpl}")
    return " \\\n  ".join(parts)


def _fetch_runs(journal: TradeJournal, days: int) -> list[dict[str, Any]]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with journal._conn() as con:
        rows = con.execute(
            """
            SELECT run_id, strategy, symbols_json, params_json, started_at, ended_at,
                   git_commit, git_commit_short, git_branch, git_dirty
            FROM runs
            WHERE started_at >= ?
            ORDER BY started_at DESC
            """,
            (cutoff,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            run = dict(row)
            try:
                run["symbols"] = json.loads(run.pop("symbols_json") or "[]")
            except json.JSONDecodeError:
                run["symbols"] = []
            try:
                run["params"] = json.loads(run.pop("params_json") or "{}")
            except json.JSONDecodeError:
                run["params"] = {}
            run["code_provenance"] = run["params"].get("code_provenance")
            stats = _run_pnl_stats(con, run["run_id"])
            run.update(stats)
            out.append(run)
    return out


def format_run(run: dict[str, Any], *, verbose: bool = False) -> str:
    prov = _provenance_for_run(run)
    commit = prov.get("commit") or "unknown"
    short = prov.get("commit_short") or str(commit)[:12]
    inferred = prov.get("inferred")
    params = run.get("params") or {}

    lines = [
        f"Run ID:     {run['run_id']}",
        f"Started:    {run.get('started_at')}  ended={run.get('ended_at')}",
        f"Strategy:   {run.get('strategy')}  symbols={run.get('symbols')}",
        f"Realized:   ${float(run.get('realized_pnl') or 0):,.2f}  "
        f"({int(run.get('closed') or 0)} closed / {int(run.get('trades') or 0)} total trades, "
        f"{int(run.get('open_trades') or 0)} still open)",
        f"Git:        {short}  branch={prov.get('branch') or run.get('git_branch')}  "
        f"dirty={prov.get('dirty') if prov.get('dirty') is not None else run.get('git_dirty')}"
        + ("  (inferred from started_at)" if inferred else ""),
    ]
    if prov.get("subject"):
        lines.append(f"Subject:    {prov['subject']}")
    lines.append(f"Checkout:   git checkout {short}")
    if commit != "unknown" and len(str(commit)) >= 40:
        lines.append(f"Full SHA:   {commit}")

    psummary = {k: params.get(k) for k in PAPER_LAUNCH_KEYS if k in params}
    if psummary:
        lines.append("Params:     " + json.dumps(psummary, default=str))

    lines.append("Relaunch:\n  " + _paper_watch_command(run))
    if verbose:
        lines.append("Provenance:\n" + json.dumps(prov, indent=2, default=str))
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description="Reconstruct git + paper launch from trade journal runs")
    p.add_argument("--journal-db", default=str(DEFAULT_DB))
    p.add_argument("--days", type=int, default=31)
    p.add_argument("--top", type=int, default=10, help="Top N runs by realized PnL")
    p.add_argument("--run-id", dest="run_id", help="Exact journal run_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    journal = TradeJournal(Path(args.journal_db))

    if args.run_id:
        run = journal.get_run(args.run_id)
        if not run:
            print(f"Unknown run_id: {args.run_id}", file=sys.stderr)
            sys.exit(1)
        with journal._conn() as con:
            run.update(_run_pnl_stats(con, run["run_id"]))
        selected = [run]
    else:
        runs = _fetch_runs(journal, args.days)
        runs.sort(key=lambda r: float(r.get("realized_pnl") or 0), reverse=True)
        selected = runs[: max(1, args.top)]
        if not selected:
            print(f"No paper runs in the last {args.days} days.", file=sys.stderr)
            sys.exit(1)

    if args.json:
        payload = []
        for run in selected:
            prov = _provenance_for_run(run)
            payload.append(
                {
                    "run_id": run["run_id"],
                    "started_at": run.get("started_at"),
                    "realized_pnl": float(run.get("realized_pnl") or 0),
                    "trades": int(run.get("trades") or 0),
                    "closed": int(run.get("closed") or 0),
                    "code_provenance": prov,
                    "git_checkout": prov.get("commit_short") or prov.get("commit"),
                    "paper_watch_command": _paper_watch_command(run),
                    "params": run.get("params"),
                }
            )
        print(json.dumps(payload if len(payload) > 1 else payload[0], indent=2, default=str))
        return

    for i, run in enumerate(selected):
        if i:
            print("\n" + "=" * 72 + "\n")
        print(format_run(run, verbose=args.verbose))


if __name__ == "__main__":
    main()
