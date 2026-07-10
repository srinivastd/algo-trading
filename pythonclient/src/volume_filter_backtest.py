#!/usr/bin/env python3
"""
Multi-symbol 1Y backtest comparing the baseline GroqRecommendations entries against
two volume-based entry filters:

  baseline : every entry the strategy takes
  (a)      : vol_ratio >= VOL_FLOOR  (default 1.0)
  (b)      : vol_ratio >= VOL_STRONG (default 2.0)  AND  adx >= ADX_STRONG (default 30)

The filters are applied *post-hoc* to the baseline trade set: each surviving trade keeps
its own simulated PnL, and we re-sum PnL / win-rate / trade-count. This is the standard
fast A/B for an entry filter (a filter only removes entries, never adds them). The one
caveat is capital compounding — per-trade share sizing in the baseline depends on capital
at entry time, so the variant totals hold sizing fixed at the baseline path rather than
re-simulating. If a filter looks promising here, the follow-up is a full re-sim with the
gate inside signal generation.

Data is read from the offline historical_data cache (3Y 1-hour CSVs), sliced to the most
recent ~1 year, so no IB Gateway connection is required.

Usage (from pythonclient/):
  PYTHONPATH=src ./py src/volume_filter_backtest.py
  PYTHONPATH=src ./py src/volume_filter_backtest.py --years 1 --vol-floor 1.0 --vol-strong 2.0 --adx-strong 30
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy

logging.getLogger().setLevel(logging.WARNING)  # quiet the strategy's INFO chatter

PC_ROOT = Path(__file__).resolve().parent.parent
HIST_DIR = PC_ROOT / "historical_data"

# Full watchlist: every symbol that has the consistent 3Y 1-hour cache.
WATCHLIST = ["AAPL", "AMD", "AMZN", "GOOG", "INTC", "META", "MSFT", "NVDA", "PLTR", "QBTS", "TSLA"]
HIST_SUFFIX = "-2023-06-10-2026-06-09-1hour.csv"

HOLD_OUT = "QBTS"  # reported separately (inspired the rule)


def _load_symbol(symbol: str, years: float) -> pd.DataFrame | None:
    path = HIST_DIR / f"{symbol}{HIST_SUFFIX}"
    if not path.exists():
        return None
    df = pd.read_csv(path, index_col="date", parse_dates=False)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    cutoff = df.index.max() - pd.Timedelta(days=round(365 * years))
    return df[df.index >= cutoff].copy()


@dataclass
class VariantStats:
    name: str
    trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    pnls: list[float] = field(default_factory=list)

    def add(self, profit: float) -> None:
        self.trades += 1
        self.pnl += profit
        if profit > 0:
            self.wins += 1
        self.pnls.append(profit)

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    @property
    def avg(self) -> float:
        return (self.pnl / self.trades) if self.trades else 0.0


def _new_variants() -> dict[str, VariantStats]:
    return {
        "baseline": VariantStats("baseline"),
        "a_vol_floor": VariantStats("(a) vol>=floor"),
        "b_vol_adx": VariantStats("(b) post-hoc vol&adx"),
        "b_true_sim": VariantStats("(b) true-sim gate (compounded)"),
    }


async def _backtest_symbol(
    symbol: str,
    df: pd.DataFrame,
    *,
    vol_floor: float,
    vol_strong: float,
    adx_strong: float,
) -> dict[str, VariantStats]:
    strat = GroqRecommendationsStrategy(skip_chromadb=True)
    # generate_signals/backtest require indicators to be precomputed (it does not call
    # calculate_indicators itself). Compute once, feed to backtest, and reuse the same
    # frame for entry-bar vol_ratio/adx lookup.
    ind = strat.calculate_indicators(df.copy())
    result = await strat.backtest(ind.copy(), symbol=symbol)
    trades = result.get("trades", [])

    # True compounding re-sim with the gate wired into generate_signals (validates wiring +
    # corrects for capital-path effects the post-hoc filter ignores).
    gated = GroqRecommendationsStrategy(
        skip_chromadb=True,
        entry_volume_adx_gate=True,
        entry_min_volume_ratio=vol_strong,
        entry_min_adx=adx_strong,
    )
    gated_ind = gated.calculate_indicators(df.copy())
    gated_result = await gated.backtest(gated_ind.copy(), symbol=symbol)

    variants = _new_variants()
    for tr in gated_result.get("trades", []):
        if "profit" in tr:
            variants["b_true_sim"].add(float(tr["profit"]))
    for tr in trades:
        if "profit" not in tr:
            continue  # never closed (shouldn't happen — series_end closes all)
        profit = float(tr["profit"])
        entry_time = tr["entry_date"]
        try:
            row = ind.loc[entry_time]
            vol_ratio = float(row["volume_ratio"])
            adx = float(row["adx"])
        except (KeyError, TypeError, ValueError):
            vol_ratio = float("nan")
            adx = float("nan")

        variants["baseline"].add(profit)
        if not math.isnan(vol_ratio) and vol_ratio >= vol_floor:
            variants["a_vol_floor"].add(profit)
        if (
            not math.isnan(vol_ratio)
            and not math.isnan(adx)
            and vol_ratio >= vol_strong
            and adx >= adx_strong
        ):
            variants["b_vol_adx"].add(profit)
    return variants


def _merge(into: dict[str, VariantStats], src: dict[str, VariantStats]) -> None:
    for key, vs in src.items():
        tgt = into[key]
        tgt.trades += vs.trades
        tgt.wins += vs.wins
        tgt.pnl += vs.pnl
        tgt.pnls.extend(vs.pnls)


def _print_table(title: str, variants: dict[str, VariantStats]) -> None:
    print(f"\n{title}")
    print("-" * 78)
    print(f"{'variant':<34}{'trades':>8}{'win_rate':>11}{'total_pnl':>14}{'avg_pnl':>11}")
    base = variants["baseline"]
    for key in ("baseline", "a_vol_floor", "b_vol_adx", "b_true_sim"):
        vs = variants[key]
        delta = ""
        if key != "baseline" and base.pnl != 0:
            delta = f"  ({(vs.pnl - base.pnl)/abs(base.pnl)*100:+.0f}% vs base)"
        print(
            f"{vs.name:<34}{vs.trades:>8}{vs.win_rate:>10.1f}%"
            f"{vs.pnl:>14,.0f}{vs.avg:>11,.0f}{delta}"
        )


def main() -> None:
    p = argparse.ArgumentParser(description="Multi-symbol volume-filter backtest comparison")
    p.add_argument("--years", type=float, default=1.0, help="Lookback window in years (default 1)")
    p.add_argument("--vol-floor", type=float, default=1.0, help="Variant (a) min volume_ratio")
    p.add_argument("--vol-strong", type=float, default=2.0, help="Variant (b) min volume_ratio")
    p.add_argument("--adx-strong", type=float, default=25.0, help="Variant (b) min adx")
    p.add_argument("--symbols", nargs="*", default=WATCHLIST, help="Override watchlist")
    args = p.parse_args()

    overall = _new_variants()
    ex_holdout = _new_variants()
    holdout: dict[str, VariantStats] | None = None
    per_symbol: list[tuple[str, dict[str, VariantStats], str]] = []

    for symbol in args.symbols:
        df = _load_symbol(symbol, args.years)
        if df is None or df.empty:
            print(f"SKIP {symbol}: no cached data")
            continue
        span = f"{df.index.min().date()} -> {df.index.max().date()} ({len(df)} bars)"
        variants = asyncio.run(
            _backtest_symbol(
                symbol,
                df,
                vol_floor=args.vol_floor,
                vol_strong=args.vol_strong,
                adx_strong=args.adx_strong,
            )
        )
        per_symbol.append((symbol, variants, span))
        _merge(overall, variants)
        if symbol == HOLD_OUT:
            holdout = variants
        else:
            _merge(ex_holdout, variants)

    print("=" * 78)
    print(
        f"Volume-filter backtest  |  window={args.years}Y  |  "
        f"(a) vol>={args.vol_floor}  |  (b) vol>={args.vol_strong} & adx>={args.adx_strong}"
    )
    print(f"Symbols: {', '.join(s for s, _, _ in per_symbol)}")
    print("=" * 78)

    _print_table("AGGREGATE — full watchlist", overall)
    _print_table(f"AGGREGATE — ex-{HOLD_OUT} (out-of-sample)", ex_holdout)
    if holdout is not None:
        _print_table(f"{HOLD_OUT} only (hold-out / rule inspiration)", holdout)

    print("\n\nPER-SYMBOL baseline / (b) true-sim gate  —  trades, win%, total PnL")
    print("-" * 78)
    print(f"{'symbol':<8}{'baseline':>24}{'(b) true-sim gate':>26}")
    for symbol, v, span in per_symbol:
        def cell(vs: VariantStats) -> str:
            return f"{vs.trades}t {vs.win_rate:.0f}% ${vs.pnl:,.0f}"
        print(
            f"{symbol:<8}{cell(v['baseline']):>24}{cell(v['b_true_sim']):>26}"
        )
        print(f"   {span}")


if __name__ == "__main__":
    main()
