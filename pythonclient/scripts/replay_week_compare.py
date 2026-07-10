#!/usr/bin/env python3
"""
Offline long-only week replay for a given worktree checkout.
Uses May-13 / Jun-22 paper params (stop 1.1%, risk $2k, 1h, zone_lookback 20).

Usage (from a worktree's pythonclient/):
  PYTHONPATH=src ./py /path/to/replay_week_compare.py --label may13 --commit 3368c18
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import pandas as pd

logging.disable(logging.CRITICAL)

from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy

HIST = Path(__file__).resolve().parents[1] / "historical_data"
# Prefer Jul-8 3Y cache; fall back to newest *1hour* covering the window.
SYMS = ["QBTS", "AAPL", "TSLA", "PLTR", "NVDA", "AMD", "META", "GOOG", "INTC", "MU"]
PREFERRED = "-2023-07-09-2026-07-08-1hour.csv"


class LongOnly(GroqRecommendationsStrategy):
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        sigs = super().generate_signals(df)
        sigs.loc[sigs["signal"] < 0, "signal"] = 0
        return sigs


def load_symbol(sym: str) -> pd.DataFrame | None:
    preferred = HIST / f"{sym}{PREFERRED}"
    path = preferred if preferred.exists() else None
    if path is None:
        cands = sorted(HIST.glob(f"{sym}-*-1hour.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        path = cands[0] if cands else None
    if path is None:
        return None
    df = pd.read_csv(path, index_col="date", parse_dates=False)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    return df[["open", "high", "low", "close", "volume"]].sort_index()


async def run(label: str, commit: str, start: str, end: str) -> dict:
    start_ts = pd.Timestamp(f"{start} 00:00:00", tz="America/New_York")
    end_ts = pd.Timestamp(f"{end} 23:59:59", tz="America/New_York")
    warm_from = start_ts - pd.Timedelta(days=60)

    # Match May 13 / Jun 22 journal params (no vol/ADX gate — that didn't exist yet).
    strat = LongOnly(
        skip_chromadb=True,
        risk_per_trade=2000.0,
        max_duration_hours=12.0,
        stop_loss_pct=1.1,
        target_pct=4.8,
        bar_size="1 hour",
        duration="3 Y",
        zone_lookback=20,
        adx_min=28.5,
        long_min_rsi=18.0,
        zone_freshness_hours=8.0,
        volume_breakout_mult=1.36,
        volume_confirm_mult=1.19,
        position_size_cap=0.95,
        min_hold_hours=2.0,
        short_realtime_filters_enabled=False,
        paper_trading=True,
    )

    all_trades: list[dict] = []
    loaded = []
    for sym in SYMS:
        raw = load_symbol(sym)
        if raw is None:
            print(f"SKIP {sym}: no cache", flush=True)
            continue
        raw = raw[(raw.index >= warm_from) & (raw.index <= end_ts)].copy()
        if len(raw) < 100:
            print(f"SKIP {sym}: too few bars ({len(raw)})", flush=True)
            continue
        print(f"BT {sym}: {raw.index.min().date()}→{raw.index.max().date()} n={len(raw)}", flush=True)
        ind = strat.calculate_indicators(raw)
        result = await strat.backtest(ind.copy(), symbol=sym)
        loaded.append(sym)
        for tr in result.get("trades", []):
            et = tr.get("entry_date")
            if et is None or "profit" not in tr:
                continue
            et = pd.Timestamp(et)
            if et.tzinfo is None:
                et = et.tz_localize("America/New_York")
            else:
                et = et.tz_convert("America/New_York")
            if not (start_ts <= et <= end_ts):
                continue
            if tr.get("type") != "LONG":
                continue
            all_trades.append(
                {
                    "symbol": sym,
                    "entry": et.isoformat(),
                    "exit": str(tr.get("exit_date")),
                    "pnl": float(tr["profit"]),
                    "exit_reason": tr.get("exit_reason"),
                    "entry_price": float(tr.get("entry_price") or 0),
                    "shares": tr.get("shares"),
                }
            )

    pnls = [t["pnl"] for t in all_trades]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    out = {
        "label": label,
        "commit": commit,
        "window": {"start": start, "end": end},
        "symbols": loaded,
        "params": {
            "risk_per_trade": 2000.0,
            "stop_loss_pct": 1.1,
            "target_pct": 4.8,
            "max_duration_hours": 12.0,
            "bar_size": "1 hour",
            "zone_lookback": 20,
            "adx_min": 28.5,
            "zone_freshness_hours": 8.0,
            "long_only": True,
            "entry_volume_adx_gate": False,
        },
        "trades": n,
        "wins": wins,
        "win_rate_pct": (wins / n * 100.0) if n else 0.0,
        "pnl": sum(pnls) if pnls else 0.0,
        "avg_pnl": (sum(pnls) / n) if n else 0.0,
        "trade_detail": sorted(all_trades, key=lambda t: t["entry"]),
    }
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--start", default="2026-07-01")
    p.add_argument("--end", default="2026-07-08")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    result = asyncio.run(run(args.label, args.commit, args.start, args.end))
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text)
        print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
