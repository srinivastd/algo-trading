#!/usr/bin/env python3
"""
Step 2: Compare paper watch session (journal + market_data.json) to a ChromaDB backtest row.

Backtest ground truth: collection `backtest_results` (same as batch_backtest / query_chroma_recent_trades).
Paper: SQLite `trades`/`fills` + JSONL `market_data.json` (trade_execution_attempt/result, exits).

  cd pythonclient && ./py src/compare_paper_session_to_backtest.py \\
    --symbol QBTS --strategy groq \\
    --journal-db trade_journal.db \\
    --market-data market_data_logs/market_data.json \\
    --output logs/paper_vs_backtest_QBTS.md

  # Pin baseline.json `symbols.QBTS.source_run_id` (otherwise latest Chroma row for symbol wins):
  ./py src/compare_paper_session_to_backtest.py --symbol QBTS --strategy groq \\
    --chroma-run-id 'QBTS_2026-04-06 15:46:28.186352'

  # One watch session (journal run_id + UTC log date; filters market_data trade_ids):
  ./py src/compare_paper_session_to_backtest.py --symbol QBTS --strategy groq \\
    --run-id RUN_20260409_134608_5e775ec8 --date 2026-04-09 \\
    --chroma-run-id 'QBTS_2026-04-06 15:46:28.186352'
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb

_SCRIPT_DIR = Path(__file__).resolve().parent
_PYTHONCLIENT_ROOT = _SCRIPT_DIR.parent

from query_chroma_recent_trades import (  # noqa: E402
    _num,
    _ts_from_id,
    parse_trades,
)


def _merge_chroma_metas(paths: list[Path]) -> dict[str, tuple[str, dict[str, Any]]]:
    """id -> (chroma_dir, metadata)."""
    merged: dict[str, tuple[str, dict[str, Any]]] = {}
    for p in paths:
        p = Path(p).resolve()
        if not p.is_dir():
            continue
        try:
            coll = chromadb.PersistentClient(path=str(p)).get_collection("backtest_results")
            raw = coll.get(include=["metadatas"])
            for i, rid in enumerate(raw["ids"]):
                if rid not in merged:
                    merged[rid] = (str(p), dict(raw["metadatas"][i] or {}))
        except Exception:
            continue
    return merged


def _pick_chroma_row(
    merged: dict[str, tuple[str, dict[str, Any]]],
    *,
    symbol: str,
    strategy: str,
    chroma_run_id: str | None,
) -> tuple[str, str, dict[str, Any]] | None:
    sym_u = symbol.strip().upper()
    strat = strategy.strip()
    if chroma_run_id:
        rid = chroma_run_id.strip()
        if rid in merged:
            path, meta = merged[rid]
            return rid, path, meta
        return None
    best: tuple[str, str, dict[str, Any], datetime] | None = None
    for rid, (path, meta) in merged.items():
        if str(meta.get("symbol", "")).upper() != sym_u:
            continue
        if str(meta.get("strategy") or "legacy") != strat:
            continue
        ts = _ts_from_id(rid)
        if best is None or ts > best[3]:
            best = (rid, path, meta, ts)
    if best is None:
        return None
    return best[0], best[1], best[2]


def _load_baseline_symbol(path: Path, symbol: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    sym = symbol.strip().upper()
    block = (data.get("symbols") or {}).get(sym) or (data.get("symbols") or {}).get(symbol.strip())
    if not isinstance(block, dict):
        return None
    out = dict(block)
    out["_defaults"] = data.get("default_params") or {}
    out["_baseline_strategy"] = data.get("strategy")
    return out


def _load_journal_trades(
    db: Path,
    *,
    run_id: str | None,
    symbol: str | None,
    day: str | None,
) -> list[dict[str, Any]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        q = """
            SELECT trade_id, run_id, symbol, side, status, signal_value,
                   intended_entry_price, intended_shares, order_id,
                   entry_fill_price, entry_fill_time, exit_fill_price, exit_fill_time,
                   exit_reason, realized_pnl, error_message, slippage_bps,
                   created_at, updated_at, indicator_snapshot_json
            FROM trades
            WHERE 1=1
        """
        params: list[Any] = []
        if run_id:
            q += " AND run_id = ?"
            params.append(run_id)
        if symbol:
            q += " AND symbol = ?"
            params.append(symbol.strip().upper())
        if day:
            q += """ AND (
                substr(created_at,1,10) = ? OR substr(entry_fill_time,1,10) = ?
                OR substr(exit_fill_time,1,10) = ? OR substr(updated_at,1,10) = ?
            )"""
            params.extend([day, day, day, day])
        q += " ORDER BY created_at ASC"
        return [dict(r) for r in con.execute(q, params).fetchall()]
    finally:
        con.close()


def _exit_mix_bt(trades: list[dict[str, Any]]) -> Counter[str]:
    c: Counter[str] = Counter()
    for t in trades:
        r = t.get("exit_reason") or t.get("exitReason")
        if r:
            c[str(r)] += 1
    return c


def _exit_mix_paper(rows: list[dict[str, Any]]) -> Counter[str]:
    c: Counter[str] = Counter()
    for r in rows:
        if str(r.get("status", "")).lower() == "closed":
            er = r.get("exit_reason") or "unknown"
            c[str(er)] += 1
    return c


def _scan_market_data(
    path: Path,
    *,
    symbol: str | None,
    day: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    stats = {
        "lines_ok": 0,
        "lines_bad": 0,
        "trade_execution_attempt": 0,
        "trade_execution_result": 0,
        "result_status": Counter(),
        "position_exit_attempt": 0,
        "position_exit_result": 0,
        "position_bootstrap": 0,
        "position_monitoring_exit_non_hold": 0,
        "attempt_trade_ids": [],
        "result_by_trade_id": {},
    }
    if not path.is_file():
        stats["error"] = f"missing_file: {path}"
        return stats

    sym_f = symbol.strip().upper() if symbol else None
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                stats["lines_bad"] += 1
                continue
            stats["lines_ok"] += 1
            ts = str(o.get("timestamp") or "")
            if day and ts and ts[:10] != day:
                continue
            s = str(o.get("symbol") or "").upper()
            if sym_f and s and s != sym_f:
                continue
            tid_line = str(o.get("trade_id") or "")
            if run_id and tid_line and run_id not in tid_line:
                continue

            action = o.get("action")
            if action == "trade_execution_attempt":
                stats["trade_execution_attempt"] += 1
                tid = o.get("trade_id")
                if tid:
                    stats["attempt_trade_ids"].append(str(tid))
            elif action == "trade_execution_result":
                stats["trade_execution_result"] += 1
                tr = o.get("trade_result") or {}
                st = str(tr.get("status") or "?")
                stats["result_status"][st] += 1
                tid = o.get("trade_id")
                if tid:
                    stats["result_by_trade_id"][str(tid)] = tr
            elif action == "position_exit_attempt":
                stats["position_exit_attempt"] += 1
            elif action == "position_exit_result":
                stats["position_exit_result"] += 1
            elif action == "position_bootstrap":
                stats["position_bootstrap"] += 1
            elif action == "position_monitoring":
                pd = o.get("position_details") or {}
                er = str(pd.get("exit_reason") or "")
                if er and er != "hold":
                    stats["position_monitoring_exit_non_hold"] += 1

    return stats


def _fmt_counter(c: Counter[str]) -> str:
    if not c:
        return "*None*"
    return ", ".join(f"`{k}`: {v}" for k, v in c.most_common())


def _param_diff(chroma_meta: dict[str, Any], baseline: dict[str, Any] | None) -> list[str]:
    if not baseline:
        return []
    keys = [
        ("stop_loss_pct", "stop_loss_pct"),
        ("target_pct", "target_pct"),
        ("max_duration_hours", "max_duration_hours"),
        ("risk_per_trade", "risk_per_trade"),
        ("bar_size", "bar_size"),
        ("duration", "duration"),
        ("initial_capital", "initial_capital"),
    ]
    notes = []
    for ck, bk in keys:
        cv = chroma_meta.get(ck)
        bv = baseline.get(bk)
        if bv is None and baseline.get("_defaults"):
            bv = (baseline["_defaults"] or {}).get(bk)
        if cv is None and bv is None:
            continue
        try:
            if cv is not None and bv is not None and float(cv) == float(bv):
                continue
            if str(cv).strip() == str(bv).strip():
                continue
        except (TypeError, ValueError):
            if str(cv) == str(bv):
                continue
        notes.append(f"- **{ck}**: Chroma=`{cv}` baseline=`{bv}`")
    return notes


def build_report(
    *,
    symbol: str,
    strategy: str,
    chroma_id: str,
    chroma_path: str,
    chroma_meta: dict[str, Any],
    bt_trades: list[dict[str, Any]],
    paper_trades: list[dict[str, Any]],
    md_stats: dict[str, Any],
    baseline: dict[str, Any] | None,
    day: str | None,
    run_id: str | None,
) -> str:
    lines: list[str] = []
    lines.append(f"# Paper session vs backtest ({symbol.upper()} / {strategy})")
    lines.append("")
    if day:
        lines.append(f"Filter day (UTC prefix match on timestamps): **{day}**")
    if run_id:
        lines.append(f"Journal run_id filter: `{run_id}`")
    lines.append("")

    lines.append("## Chroma backtest row")
    lines.append("")
    lines.append(f"- **id**: `{chroma_id}`")
    lines.append(f"- **path**: `{chroma_path}`")
    ic = _num(chroma_meta.get("initial_capital"), 0)
    fc = _num(chroma_meta.get("final_capital"), 0)
    ret = ((fc - ic) / ic * 100.0) if ic > 0 else 0.0
    lines.append(
        f"- **return_pct**: {ret:.4f}% (initial={ic:,.2f} final={fc:,.2f}) "
        f"total_trades={chroma_meta.get('total_trades', len(bt_trades))} "
        f"success_rate={chroma_meta.get('success_rate', 'n/a')}"
    )
    lines.append(
        f"- **params**: bar_size={chroma_meta.get('bar_size')} duration={chroma_meta.get('duration')} "
        f"stop_loss_pct={chroma_meta.get('stop_loss_pct')} target_pct={chroma_meta.get('target_pct')} "
        f"max_duration_hours={chroma_meta.get('max_duration_hours')}"
    )
    lines.append("")
    lines.append("### Backtest exit_reason mix (parsed trades)")
    em = _exit_mix_bt(bt_trades)
    if not em and bt_trades:
        lines.append(
            "*Chroma `trades` payload has no `exit_reason` field (common for Groq-stored rows); "
            "exit mix unavailable without a richer export.*"
        )
    else:
        lines.append(_fmt_counter(em))
    lines.append("")

    if baseline:
        lines.append("## Baseline.json parity (symbol block vs Chroma)")
        lines.append("")
        diffs = _param_diff(chroma_meta, baseline)
        if diffs:
            lines.extend(diffs)
        else:
            lines.append("*No mismatches on common numeric/string params.*")
        lines.append("")

    lines.append("## Paper: SQLite `trades`")
    lines.append("")
    lines.append(f"- **rows**: {len(paper_trades)}")
    st = Counter(str(r.get("status") or "?") for r in paper_trades)
    lines.append(f"- **status**: {_fmt_counter(st)}")
    boot = sum(1 for r in paper_trades if str(r.get("trade_id", "")).startswith("BOOTSTRAP_"))
    lines.append(f"- **bootstrap trade_ids**: {boot}")
    lines.append("")
    lines.append("### Paper exit_reason (closed only)")
    lines.append(_fmt_counter(_exit_mix_paper(paper_trades)))
    lines.append("")

    lines.append("## Paper: market_data.json")
    lines.append("")
    if md_stats.get("error"):
        lines.append(f"- **error**: {md_stats['error']}")
    else:
        lines.append(f"- **lines parsed**: ok={md_stats['lines_ok']} bad={md_stats['lines_bad']}")
        lines.append(f"- **trade_execution_attempt**: {md_stats['trade_execution_attempt']}")
        lines.append(f"- **trade_execution_result**: {md_stats['trade_execution_result']}")
        lines.append(f"- **result status**: {_fmt_counter(md_stats['result_status'])}")
        lines.append(f"- **position_bootstrap**: {md_stats['position_bootstrap']}")
        lines.append(f"- **position_exit_attempt / result**: {md_stats['position_exit_attempt']} / {md_stats['position_exit_result']}")
        lines.append(
            f"- **position_monitoring** rows with exit_reason != hold: {md_stats['position_monitoring_exit_non_hold']}"
        )
    lines.append("")

    lines.append("## Gap hints (manual follow-up)")
    lines.append("")
    att = set(md_stats.get("attempt_trade_ids") or [])
    jids = {str(r["trade_id"]) for r in paper_trades}
    only_md = sorted(att - jids)
    only_j = sorted(jids - att)
    if only_md:
        lines.append(
            f"- trade_ids in **market_data attempts** but not in journal filter: {len(only_md)} "
            f"(sample: `{only_md[:5]}`). *Tighten with `--run-id` + `--date` if comparing one session.*"
        )
    if only_j and md_stats.get("trade_execution_attempt", 0) > 0:
        lines.append(
            f"- trade_ids in **journal** but no attempt in filtered market_data: {len(only_j)} "
            f"(sample: `{only_j[:5]}`)"
        )
    err_journal = [r for r in paper_trades if str(r.get("status", "")).lower() in ("error", "attempted")]
    if err_journal:
        lines.append(
            f"- journal rows not cleanly closed/submitted: {len(err_journal)} "
            "(check `status`, `error_message`)"
        )
    fails = md_stats.get("result_by_trade_id") or {}
    failed_tids = [tid for tid, tr in fails.items() if str(tr.get("status")).lower() != "success"]
    if failed_tids:
        lines.append(f"- execution results with status != success: {len(failed_tids)} (sample trade_ids: `{failed_tids[:5]}`)")
    if not bt_trades:
        lines.append("- **Chroma parsed 0 trades**; check metadata `trades` string / run id.")
    lines.append("")
    lines.append("---")
    lines.append("*Backtest is historical simulation; paper differs on fills, bars, and session. Use this report to find execution gaps, not to expect matching PnL.*")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compare paper journal + logs to Chroma backtest_results row")
    p.add_argument("--symbol", required=True, help="Symbol, e.g. QBTS")
    p.add_argument("--strategy", default="groq", help="Chroma metadata strategy slug (default groq)")
    p.add_argument("--chroma-run-id", default=None, help="Exact Chroma id; default = latest for symbol+strategy")
    p.add_argument(
        "--chroma-path",
        action="append",
        default=None,
        help="Chroma persistent dir (repeatable). Default: pythonclient/chroma_db, src/chroma_db, repo-root/chroma_db",
    )
    p.add_argument("--journal-db", type=Path, default=_PYTHONCLIENT_ROOT / "trade_journal.db")
    p.add_argument("--run-id", default=None, help="Filter journal trades.run_id")
    p.add_argument("--date", default=None, help="UTC date YYYY-MM-DD filter on journal + log timestamps")
    p.add_argument(
        "--market-data",
        type=Path,
        default=_PYTHONCLIENT_ROOT / "market_data_logs" / "market_data.json",
    )
    p.add_argument("--baseline", type=Path, default=_PYTHONCLIENT_ROOT / "baseline.json")
    p.add_argument("--no-baseline", action="store_true")
    p.add_argument("--output", type=Path, default=None, help="Write Markdown report to this path")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    sym = args.symbol.strip().upper()

    paths = [Path(p) for p in args.chroma_path] if args.chroma_path else []
    if not paths:
        paths = [
            _PYTHONCLIENT_ROOT / "chroma_db",
            _SCRIPT_DIR / "chroma_db",
            _PYTHONCLIENT_ROOT.parent / "chroma_db",
        ]
    merged = _merge_chroma_metas(paths)
    if not merged:
        print("No Chroma backtest_results found in:", ", ".join(str(p) for p in paths))
        return 1

    picked = _pick_chroma_row(
        merged, symbol=sym, strategy=args.strategy, chroma_run_id=args.chroma_run_id
    )
    if not picked:
        print(f"No Chroma row for symbol={sym!r} strategy={args.strategy!r} (and run id if set).")
        return 1
    chroma_id, chroma_path, chroma_meta = picked
    bt_trades = parse_trades(chroma_meta.get("trades"))

    baseline = None if args.no_baseline else _load_baseline_symbol(args.baseline, sym)

    paper_trades = _load_journal_trades(
        args.journal_db, run_id=args.run_id, symbol=sym, day=args.date
    )
    md_stats = _scan_market_data(
        args.market_data, symbol=sym, day=args.date, run_id=args.run_id
    )

    report = build_report(
        symbol=sym,
        strategy=args.strategy,
        chroma_id=chroma_id,
        chroma_path=chroma_path,
        chroma_meta=chroma_meta,
        bt_trades=bt_trades,
        paper_trades=paper_trades,
        md_stats=md_stats,
        baseline=baseline,
        day=args.date,
        run_id=args.run_id,
    )

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"Wrote {args.output.resolve()}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
