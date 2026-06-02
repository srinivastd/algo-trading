#!/usr/bin/env python3
"""
List journal trades whose entry ``decision_zone.decision_confidence`` exceeds a threshold,
with ADX / volume-chain / zone-age columns from ``indicator_snapshot_json`` at entry.

Watch-mode logging merges ``decision_zone`` into the snapshot (see ``main.watch_mode`` /
``GroqRecommendationsStrategy._decision_zone_payload``). Top-level ``adx`` may duplicate
``decision_zone.adx``; this script prefers ``decision_zone`` values when present.

Usage (from pythonclient/):

  PYTHONPATH=src ./py scripts/journal_high_confidence_metrics.py --min-confidence 0.7
  PYTHONPATH=src ./py scripts/journal_high_confidence_metrics.py --min-confidence 0.7 --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]


def _parse_snap(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if isinstance(raw, str) else {}
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
        if v != v:  # nan
            return None
        return v
    except (TypeError, ValueError):
        return None


def _confidence(snap: dict[str, Any]) -> Optional[float]:
    dz = snap.get("decision_zone")
    if not isinstance(dz, dict):
        return None
    return _f(dz.get("decision_confidence"))


def _adx_rsi(snap: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    dz = snap.get("decision_zone")
    adx_dz = rsi_dz = None
    if isinstance(dz, dict):
        adx_dz = _f(dz.get("adx"))
        rsi_dz = _f(dz.get("rsi"))
    adx_top = _f(snap.get("adx"))
    rsi_top = _f(snap.get("rsi"))
    return (adx_dz if adx_dz is not None else adx_top, rsi_dz if rsi_dz is not None else rsi_top)


def _zone_age_hours(snap: dict[str, Any]) -> Optional[float]:
    dz = snap.get("decision_zone")
    if not isinstance(dz, dict):
        return None
    return _f(dz.get("decision_zone_age_hours"))


def _volume_chain(snap: dict[str, Any]) -> dict[str, Optional[float]]:
    dz = snap.get("decision_zone")
    out = {
        "last_prev_ratio": None,
        "breakout_mult": None,
        "confirm_mult": None,
        "current_volume": None,
        "prev_volume": None,
    }
    if not isinstance(dz, dict):
        return out
    vc = dz.get("volume_chain")
    if not isinstance(vc, dict):
        return out
    out["last_prev_ratio"] = _f(vc.get("last_prev_ratio"))
    out["breakout_mult"] = _f(vc.get("breakout_mult"))
    out["confirm_mult"] = _f(vc.get("confirm_mult"))
    out["current_volume"] = _f(vc.get("current_volume"))
    out["prev_volume"] = _f(vc.get("prev_volume"))
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Trades with entry decision_confidence above threshold + ADX/volume/zone age."
    )
    p.add_argument("--journal-db", type=Path, default=_ROOT / "trade_journal.db")
    p.add_argument("--min-confidence", type=float, default=0.7)
    p.add_argument(
        "--status",
        choices=("all", "closed", "open", "submitted"),
        default="all",
        help="Filter trades.status (default: all)",
    )
    p.add_argument("--csv", type=Path, default=None, help="Optional CSV output path")
    p.add_argument(
        "--summary",
        action="store_true",
        help="Print median/mean ADX, zone age (h), vol last/prev ratio for filtered rows",
    )
    args = p.parse_args()
    db = args.journal_db.resolve()
    if not db.is_file():
        print(f"Database not found: {db}", file=sys.stderr)
        return 1

    q = """
        SELECT trade_id, symbol, status, side,
               entry_fill_time, exit_fill_time, realized_pnl, exit_reason,
               created_at, indicator_snapshot_json
        FROM trades
        ORDER BY COALESCE(NULLIF(trim(entry_fill_time), ''), created_at) ASC
    """
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(q))
    con.close()

    thr = float(args.min_confidence)
    picked: list[dict[str, Any]] = []
    for r in rows:
        snap = _parse_snap(r["indicator_snapshot_json"])
        conf = _confidence(snap)
        if conf is None or conf <= thr:
            continue
        st = (r["status"] or "").strip().lower()
        if args.status != "all" and st != args.status.lower():
            continue
        adx, rsi = _adx_rsi(snap)
        vc = _volume_chain(snap)
        dz = snap.get("decision_zone") if isinstance(snap.get("decision_zone"), dict) else {}
        picked.append({
            "trade_id": r["trade_id"],
            "symbol": r["symbol"],
            "status": r["status"],
            "side": r["side"],
            "entry_fill_time": r["entry_fill_time"],
            "exit_fill_time": r["exit_fill_time"],
            "realized_pnl": r["realized_pnl"],
            "exit_reason": (r["exit_reason"] or "")[:80],
            "decision_confidence": round(conf, 4),
            "adx": adx,
            "rsi": rsi,
            "zone_age_hours": _zone_age_hours(snap),
            "vol_last_prev_ratio": vc["last_prev_ratio"],
            "vol_breakout_mult": vc["breakout_mult"],
            "vol_confirm_mult": vc["confirm_mult"],
            "vol_current": vc["current_volume"],
            "vol_prev": vc["prev_volume"],
            "decision_zone_type": dz.get("decision_zone_type"),
            "decision_rule": dz.get("decision_rule"),
            "stock_profile_score": _f(snap.get("stock_profile_score")),
        })

    print(f"Journal: {db}")
    print(f"min_confidence > {thr}: {len(picked)} trade(s) (status filter={args.status})\n")

    if args.summary and picked:

        def _nums(key: str) -> list[float]:
            return [
                float(row[key])
                for row in picked
                if row.get(key) is not None and isinstance(row[key], (int, float))
            ]

        closed_n = sum(
            1
            for row in picked
            if str(row.get("status") or "").lower() == "closed"
            and row.get("realized_pnl") is not None
        )
        adxs = _nums("adx")
        ages = _nums("zone_age_hours")
        vr = _nums("vol_last_prev_ratio")

        def _med_mean(vals: list[float]) -> str:
            if not vals:
                return "n/a"
            med = statistics.median(vals)
            mean = statistics.mean(vals)
            return f"median={med:.3f} mean={mean:.3f} (n={len(vals)})"

        print("--- Summary (filtered set) ---")
        print(f"  closed with realized_pnl: {closed_n}")
        print(f"  ADX: {_med_mean(adxs)}")
        print(f"  zone_age_hours: {_med_mean(ages)}")
        print(f"  vol_last_prev_ratio: {_med_mean(vr)}")
        print()

    fields = [
        "trade_id", "symbol", "status", "side",
        "entry_fill_time", "exit_fill_time", "realized_pnl",
        "decision_confidence", "adx", "rsi", "zone_age_hours",
        "vol_last_prev_ratio", "vol_breakout_mult", "vol_confirm_mult",
        "vol_current", "vol_prev",
        "decision_zone_type", "decision_rule", "stock_profile_score", "exit_reason",
    ]

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in picked:
                w.writerow(row)
        print(f"Wrote {len(picked)} row(s) to {args.csv.resolve()}")

    for row in picked:
        parts = [
            f"{row['symbol']}",
            f"conf={row['decision_confidence']}",
            f"adx={row['adx']}",
            f"rsi={row['rsi']}",
            f"zone_age_h={row['zone_age_hours']}",
            f"vol_ratio={row['vol_last_prev_ratio']}",
            f"brk_mult={row['vol_breakout_mult']}",
            f"pnl={row['realized_pnl']}",
            f"{row['trade_id'][:36]}…",
        ]
        print("  " + "  ".join(str(x) for x in parts))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
