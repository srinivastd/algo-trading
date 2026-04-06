#!/usr/bin/env python3
"""
Recent backtest rows from ChromaDB with full per-trade detail (metadata `trades`).

  cd pythonclient && python3 src/query_chroma_recent_trades.py --recent 5
  # Default merges ./chroma_db and ./src/chroma_db (backtests often write to one or the other by cwd).
  python3 src/query_chroma_recent_trades.py --recent 3 --strategy groq --symbol qbts
  python3 src/query_chroma_recent_trades.py --path ./chroma_db --recent 5   # single DB only
  python3 src/query_chroma_recent_trades.py --all-dbs --recent 5   # also merge repo-root ../chroma_db
"""
from __future__ import annotations

import argparse
import ast
import json
from datetime import datetime
from typing import Any, List

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


def _trades_repr_eval(s: str) -> Any:
    """Parse trades saved via str(list_of_dicts) with pandas/numpy repr (trusted local DB only)."""
    import numpy as np
    from pandas import Timestamp

    g = {"np": np, "Timestamp": Timestamp}
    return eval(s, {"__builtins__": {}}, g)  # noqa: S307


def _trade_to_jsonable(t: Any) -> dict:
    if not isinstance(t, dict):
        return {"value": str(t)}
    out: dict[str, Any] = {}
    for k, v in t.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif hasattr(v, "item"):  # numpy scalar
            try:
                out[k] = v.item()
            except Exception:
                out[k] = float(v)
        else:
            out[k] = v
    return out


def parse_trades(raw: Any) -> List[dict]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [_trade_to_jsonable(t) if isinstance(t, dict) else {"value": t} for t in raw]
    if not isinstance(raw, str):
        return [{"value": raw}]
    s = raw.strip()
    if not s:
        return []
    try:
        v = ast.literal_eval(s)
        if isinstance(v, list):
            return [_trade_to_jsonable(t) if isinstance(t, dict) else {"value": t} for t in v]
        return [{"value": v}]
    except (SyntaxError, ValueError, TypeError):
        pass
    try:
        v = _trades_repr_eval(s)
        if isinstance(v, list):
            return [_trade_to_jsonable(t) if isinstance(t, dict) else {"value": t} for t in v]
        return [{"value": str(v)}]
    except Exception:
        return [{"parse_error": True, "raw": s[:2000]}]


def load_collection(path: str):
    client = chromadb.PersistentClient(path=path)
    return client.get_collection(name="backtest_results")


def main() -> None:
    p = argparse.ArgumentParser(description="Recent Chroma backtests with all transactions")
    p.add_argument(
        "--path",
        default=None,
        metavar="DIR",
        help="Single Chroma directory (cwd-relative). If omitted, merges ./chroma_db and ./src/chroma_db.",
    )
    p.add_argument(
        "--all-dbs",
        action="store_true",
        help="Also merge ../chroma_db (repo root) with the default or --path set.",
    )
    p.add_argument("--recent", type=int, default=10, help="How many most recent runs to show")
    p.add_argument("--strategy", default=None, help="Filter by strategy slug (e.g. groq)")
    p.add_argument("--symbol", default=None, help="Filter by symbol (e.g. qbts)")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = p.parse_args()

    if args.path:
        paths = [args.path]
    else:
        paths = ["./chroma_db", "./src/chroma_db"]
    if args.all_dbs:
        paths = list(dict.fromkeys([*paths, "../chroma_db"]))

    merged: dict[str, tuple[str, dict, str]] = {}  # id -> (path, meta, doc)

    for path in paths:
        try:
            coll = load_collection(path)
            raw = coll.get(include=["documents", "metadatas"])
            ids = raw["ids"]
            for i, rid in enumerate(ids):
                if rid not in merged:
                    merged[rid] = (path, raw["metadatas"][i] or {}, raw["documents"][i] or "")
        except Exception as e:
            print(f"(skip {path}: {e})")

    if not merged:
        print("No backtest_results found in any path.")
        return

    items = []
    for rid, (path, meta, doc) in merged.items():
        ic = _num(meta.get("initial_capital"), 0)
        fc = _num(meta.get("final_capital"), 0)
        ret = ((fc - ic) / ic * 100.0) if ic > 0 else 0.0
        items.append(
            {
                "id": rid,
                "chroma_path": path,
                "ts": _ts_from_id(rid),
                "document": doc,
                "meta": meta,
                "return_pct": ret,
            }
        )

    if args.strategy:
        want = args.strategy.strip().lower()
        items = [
            x
            for x in items
            if str(x["meta"].get("strategy") or "legacy").strip().lower() == want
        ]
    if args.symbol:
        sym = args.symbol.upper()
        items = [x for x in items if str(x["meta"].get("symbol") or "").upper() == sym]

    items.sort(key=lambda x: x["ts"], reverse=True)
    items = items[: args.recent]

    if args.json:
        out = []
        for it in items:
            trades = parse_trades(it["meta"].get("trades"))
            m = dict(it["meta"])
            m["trades_parsed"] = trades
            out.append(
                {
                    "id": it["id"],
                    "chroma_path": it["chroma_path"],
                    "timestamp": it["ts"].isoformat() if it["ts"].year > 2000 else None,
                    "return_pct": it["return_pct"],
                    "document": it["document"],
                    "metadata": m,
                }
            )
        print(json.dumps(out, indent=2, default=str))
        return

    print(f"Showing {len(items)} most recent backtest(s) (after filters)\n")
    for idx, it in enumerate(items, 1):
        m = it["meta"]
        trades = parse_trades(m.get("trades"))
        sym = m.get("symbol", "?")
        strat = m.get("strategy", "legacy")
        ic = _num(m.get("initial_capital"), 0)
        fc = _num(m.get("final_capital"), 0)
        tp = _num(m.get("total_profit"), fc - ic)
        sr = _num(m.get("success_rate"), 0)
        ntr = int(_num(m.get("total_trades"), len(trades)))

        print("=" * 100)
        print(f"[{idx}] {it['id']}")
        print(f"    Chroma: {it['chroma_path']}  |  strategy={strat}  |  symbol={sym}")
        print(f"    Run time (from id): {it['ts'] if it['ts'].year > 2000 else '?'}")
        print(f"    initial_capital={ic:,.2f}  final_capital={fc:,.2f}  return={it['return_pct']:.2f}%")
        print(f"    total_profit={tp:,.2f}  total_trades={ntr}  success_rate={sr:.1f}%")
        print(f"    bar_size={m.get('bar_size')}  duration={m.get('duration')}")
        print(f"    doc: {it['document'][:200]}{'...' if len(it['document']) > 200 else ''}")
        print(f"\n    --- Transactions ({len(trades)}) ---")
        for ti, tr in enumerate(trades, 1):
            line = json.dumps(tr, default=str)
            if len(line) > 160:
                print(f"    {ti:4d}  {line[:157]}...")
            else:
                print(f"    {ti:4d}  {line}")
        print()


if __name__ == "__main__":
    main()
