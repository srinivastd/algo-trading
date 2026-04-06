#!/usr/bin/env python3
"""
List backtest runs in ChromaDB ranked by return % (read-only; no sentence_transformers).

Run from pythonclient/ so ./chroma_db resolves:
  cd pythonclient && python3 src/query_chroma_best_returns.py
  python3 src/query_chroma_best_returns.py --days 365 --top 20
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

import chromadb


def _num(x, default: float = 0.0) -> float:
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank ChromaDB backtest_results by return %")
    parser.add_argument("--path", default="./chroma_db", help="Chroma persistent path")
    parser.add_argument("--days", type=int, default=90, help="Only runs newer than N days (0 = all)")
    parser.add_argument("--top", type=int, default=25, help="How many rows to print")
    parser.add_argument(
        "--strategy",
        default=None,
        metavar="SLUG",
        help="Filter by strategy metadata (e.g. groq, mean_reversion); omit for all",
    )
    args = parser.parse_args()

    client = chromadb.PersistentClient(path=args.path)
    coll = client.get_collection(name="backtest_results")
    raw = coll.get(include=["documents", "metadatas"])
    ids = raw["ids"]
    metas = raw["metadatas"]
    if not ids:
        print("No entries in backtest_results.")
        return

    rows = []
    for i, rid in enumerate(ids):
        m = metas[i] or {}
        ic = _num(m.get("initial_capital"), 0)
        fc = _num(m.get("final_capital"), 0)
        tp = _num(m.get("total_profit"), None)
        if tp is None and ic > 0:
            tp = fc - ic
        elif tp is None:
            tp = 0.0
        ret_pct = (fc - ic) / ic * 100.0 if ic > 0 else 0.0
        strat = m.get("strategy") or "legacy"
        rows.append(
            {
                "id": rid,
                "ts": _ts_from_id(rid),
                "symbol": m.get("symbol") or "?",
                "strategy": strat,
                "return_pct": ret_pct,
                "total_profit": tp,
                "total_trades": int(_num(m.get("total_trades"), 0)),
                "success_rate": _num(m.get("success_rate"), 0),
                "bar_size": m.get("bar_size"),
                "duration": m.get("duration"),
            }
        )

    if args.strategy:
        rows = [r for r in rows if r["strategy"] == args.strategy]

    if args.days and args.days > 0:
        cutoff = datetime.now() - timedelta(days=args.days)
        recent = [r for r in rows if r["ts"] >= cutoff]
        pool = recent if len(recent) >= 3 else rows
        label = f"last {args.days} days (fallback to all if <3 in window)"
    else:
        pool = rows
        label = "all time"

    pool.sort(key=lambda r: r["return_pct"], reverse=True)
    top = pool[: args.top]

    filt = f" | strategy={args.strategy}" if args.strategy else ""
    print(f"ChromaDB: backtest_results @ {args.path}")
    print(f"Total runs: {len(rows)} | Window: {label}{filt}")
    print("-" * 118)
    print(
        f"{'#':<3} {'Return%':>9} {'Profit $':>14} {'Trades':>7} {'Win%':>6} "
        f"{'Strategy':<16} {'Symbol':<7} {'Run time':<19} bar / duration"
    )
    print("-" * 118)
    for j, r in enumerate(top, 1):
        ts = r["ts"].strftime("%Y-%m-%d %H:%M") if r["ts"].year > 2000 else "?"
        cfg = f"{r.get('bar_size') or '-'} / {r.get('duration') or '-'}"
        print(
            f"{j:<3} {r['return_pct']:>9.2f} {r['total_profit']:>14,.2f} {r['total_trades']:>7} "
            f"{r['success_rate']:>6.1f} {str(r['strategy']):<16} {str(r['symbol']):<7} {ts:<19} {cfg}"
        )


if __name__ == "__main__":
    main()
