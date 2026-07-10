#!/usr/bin/env python3
"""
Rank symbols by Jun22 Groq strategy PnL and extract IBKR screener traits.

Uses offline ``historical_data/*-1hour.csv`` caches (no IB connection required for ranking).
To expand the universe: prefetch bars first, then re-run this script.

From pythonclient/:

  PYTHONPATH=src ./py scripts/rank_symbols_jun22.py
  PYTHONPATH=src ./py scripts/rank_symbols_jun22.py --symbols QBTS,MSFT,META,TSLA,NVDA
  PYTHONPATH=src ./py scripts/rank_symbols_jun22.py --start 2026-05-08 --end 2026-07-08
  PYTHONPATH=src ./py scripts/rank_symbols_jun22.py --top 5 --write-screener

Outputs:
  logs/symbol_rank_jun22.json          — full ranking + traits
  logs/ibkr_screener_hints_jun22.md    — human screener checklist (--write-screener)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy  # noqa: E402

logging.disable(logging.CRITICAL)

HIST = _ROOT / "historical_data"
LOGS = _ROOT / "logs"

# Jun22 paper params (only SL/TGT/risk/zones — matches RUN_20260622)
JUN22_KW = dict(
    skip_chromadb=True,
    risk_per_trade=2000.0,
    max_duration_hours=12.0,
    paper_trading=True,
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
)

DEFAULT_UNIVERSE = [
    "QBTS", "AAPL", "MSFT", "TSLA", "PLTR", "NVDA", "AMD", "META", "GOOG", "AMZN", "INTC",
    "MU", "BABA", "JPM", "QQQ", "TQQQ", "SLV", "BMNR",
]


@dataclass
class SymbolTraits:
    symbol: str
    avg_price: float
    atr_pct: float          # mean 14-bar ATR / close * 100
    daily_vol_pct: float    # stdev of daily returns * 100
    avg_dollar_vol: float   # mean close * volume (1h bars → proxy; also daily below)
    avg_daily_shares: float
    avg_daily_dollar_vol: float
    range_60d_pct: float    # (max-min)/min over window * 100 if shorter
    trend_abs_pct: float    # |end/start - 1| * 100
    up_days_pct: float      # fraction of up days
    mean_abs_daily_pct: float
    beta_vs_qqq: float | None


@dataclass
class SymbolRank:
    symbol: str
    trades: int
    wins: int
    win_rate_pct: float
    pnl: float
    long_n: int
    long_pnl: float
    short_n: int
    short_pnl: float
    avg_pnl: float
    profit_factor: float | None
    traits: dict[str, Any]


def _find_cache(sym: str) -> Path | None:
    preferred = [
        HIST / f"{sym}-2023-07-09-2026-07-08-1hour.csv",
        HIST / f"{sym}-2023-07-07-2026-07-06-1hour.csv",
        HIST / f"{sym}-2023-07-08-2026-07-07-1hour.csv",
        HIST / f"{sym}-2023-06-10-2026-06-09-1hour.csv",
    ]
    for p in preferred:
        if p.exists():
            return p
    cands = sorted(HIST.glob(f"{sym}-*-1hour.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0] if cands else None


def load_ohlcv(sym: str) -> pd.DataFrame | None:
    path = _find_cache(sym)
    if path is None:
        return None
    df = pd.read_csv(path, index_col="date", parse_dates=False)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def to_et(et: Any) -> pd.Timestamp:
    et = pd.Timestamp(et)
    if et.tzinfo is None:
        return et.tz_localize("America/New_York")
    return et.tz_convert("America/New_York")


def compute_traits(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp,
                   qqq_daily: pd.Series | None) -> SymbolTraits:
    w = df[(df.index >= start) & (df.index <= end)].copy()
    if len(w) < 30:
        raise ValueError("too few bars")
    atr = (w["high"] - w["low"]).rolling(14).mean()
    atr_pct = float((atr / w["close"]).dropna().mean() * 100)
    daily = w["close"].resample("1D").last().dropna()
    dret = daily.pct_change().dropna()
    daily_vol = float(dret.std() * 100) if len(dret) else 0.0
    # daily volume = sum of 1h volumes
    dvol = w["volume"].resample("1D").sum().replace(0, np.nan).dropna()
    dclose = daily.reindex(dvol.index).ffill()
    avg_daily_shares = float(dvol.mean()) if len(dvol) else 0.0
    avg_daily_dollar = float((dclose * dvol).mean()) if len(dvol) else 0.0
    # 60 trading days of daily closes if available
    d60 = daily.tail(60)
    if len(d60) >= 5:
        range_pct = float((d60.max() / d60.min() - 1) * 100)
    else:
        range_pct = float((w["close"].max() / w["close"].min() - 1) * 100)
    trend_abs = float(abs(w["close"].iloc[-1] / w["close"].iloc[0] - 1) * 100)
    up_days = float((dret > 0).mean() * 100) if len(dret) else 50.0
    mean_abs = float(dret.abs().mean() * 100) if len(dret) else 0.0

    beta = None
    if qqq_daily is not None and len(dret) > 20:
        aligned = pd.concat([dret.rename("s"), qqq_daily.rename("q")], axis=1).dropna()
        if len(aligned) > 20 and aligned["q"].var() > 0:
            beta = float(aligned["s"].cov(aligned["q"]) / aligned["q"].var())

    return SymbolTraits(
        symbol="",
        avg_price=float(w["close"].mean()),
        atr_pct=atr_pct,
        daily_vol_pct=daily_vol,
        avg_dollar_vol=float((w["close"] * w["volume"]).mean()),
        avg_daily_shares=avg_daily_shares,
        avg_daily_dollar_vol=avg_daily_dollar,
        range_60d_pct=range_pct,
        trend_abs_pct=trend_abs,
        up_days_pct=up_days,
        mean_abs_daily_pct=mean_abs,
        beta_vs_qqq=beta,
    )


async def backtest_symbol(
    sym: str,
    df: pd.DataFrame,
    warm: pd.Timestamp,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> list[dict[str, Any]]:
    raw = df[(df.index >= warm) & (df.index <= end)].copy()
    if len(raw) < 80:
        return []
    strat = GroqRecommendationsStrategy(**JUN22_KW)
    ind = strat.calculate_indicators(raw)
    result = await strat.backtest(ind.copy(), symbol=sym)
    out = []
    for tr in result.get("trades", []):
        if tr.get("entry_date") is None or "profit" not in tr:
            continue
        et = to_et(tr["entry_date"])
        if not (start <= et <= end):
            continue
        out.append({
            "symbol": sym,
            "side": tr.get("type"),
            "entry": et.isoformat(),
            "exit": str(tr.get("exit_date")),
            "pnl": float(tr["profit"]),
            "exit_reason": tr.get("exit_reason"),
            "entry_price": float(tr.get("entry_price") or 0),
            "shares": tr.get("shares"),
        })
    return out


def summarize_trades(sym: str, trades: list[dict], traits: SymbolTraits) -> SymbolRank:
    pnls = [t["pnl"] for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    gw = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p < 0)
    pf = (gw / gl) if gl > 0 else (None if gw == 0 else float("inf"))
    tdict = asdict(traits)
    tdict["symbol"] = sym
    return SymbolRank(
        symbol=sym,
        trades=n,
        wins=wins,
        win_rate_pct=(wins / n * 100) if n else 0.0,
        pnl=sum(pnls) if pnls else 0.0,
        long_n=len(longs),
        long_pnl=sum(t["pnl"] for t in longs),
        short_n=len(shorts),
        short_pnl=sum(t["pnl"] for t in shorts),
        avg_pnl=(sum(pnls) / n) if n else 0.0,
        profit_factor=pf if pf != float("inf") else None,
        traits=tdict,
    )


def _pctile(series: list[float], q: float) -> float:
    if not series:
        return 0.0
    return float(np.percentile(series, q))


def build_screener_insights(ranks: list[SymbolRank], top_n: int = 3) -> dict[str, Any]:
    """Compare top PnL names vs bottom; emit IBKR-mappable filter bands."""
    with_trades = [r for r in ranks if r.trades > 0]
    if len(with_trades) < 2:
        return {"error": "need at least 2 symbols with trades"}

    ordered = sorted(with_trades, key=lambda r: r.pnl, reverse=True)
    top = ordered[: min(top_n, len(ordered))]
    bot = ordered[-min(top_n, len(ordered)) :]
    # avoid overlap if tiny universe
    top_syms = {r.symbol for r in top}
    bot = [r for r in bot if r.symbol not in top_syms] or bot

    def trait_list(group: list[SymbolRank], key: str) -> list[float]:
        vals = []
        for r in group:
            v = r.traits.get(key)
            if v is None:
                continue
            vals.append(float(v))
        return vals

    keys = [
        "avg_price", "atr_pct", "daily_vol_pct", "avg_daily_dollar_vol",
        "avg_daily_shares", "range_60d_pct", "trend_abs_pct", "mean_abs_daily_pct",
        "beta_vs_qqq", "up_days_pct",
    ]
    comparison = {}
    for k in keys:
        tv, bv = trait_list(top, k), trait_list(bot, k)
        comparison[k] = {
            "top_mean": float(np.mean(tv)) if tv else None,
            "top_min": float(min(tv)) if tv else None,
            "top_max": float(max(tv)) if tv else None,
            "bottom_mean": float(np.mean(bv)) if bv else None,
            "bottom_min": float(min(bv)) if bv else None,
            "bottom_max": float(max(bv)) if bv else None,
        }

    # Recommended bands = envelope of top winners (inclusive), with liquidity floor
    # from the *least liquid* top name so QBTS still fits.
    top_dollar = trait_list(top, "avg_daily_dollar_vol")
    top_atr = trait_list(top, "atr_pct")
    top_dvol = trait_list(top, "daily_vol_pct")
    top_price = trait_list(top, "avg_price")
    top_range = trait_list(top, "range_60d_pct")
    top_beta = trait_list(top, "beta_vs_qqq")
    top_shares = trait_list(top, "avg_daily_shares")

    # Two clusters observed historically: speculative high-vol (QBTS) vs liquid mega-cap (MSFT/META)
    speculative = [r for r in top if r.traits.get("atr_pct", 0) >= 2.0]
    megacap = [r for r in top if r.traits.get("atr_pct", 0) < 2.0]

    def cluster_band(group: list[SymbolRank], label: str) -> dict[str, Any] | None:
        if not group:
            return None
        return {
            "label": label,
            "symbols": [r.symbol for r in group],
            "price_min": round(min(trait_list(group, "avg_price")), 2),
            "price_max": round(max(trait_list(group, "avg_price")), 2),
            "atr_pct_min": round(min(trait_list(group, "atr_pct")), 2),
            "atr_pct_max": round(max(trait_list(group, "atr_pct")), 2),
            "daily_vol_pct_min": round(min(trait_list(group, "daily_vol_pct")), 2),
            "daily_vol_pct_max": round(max(trait_list(group, "daily_vol_pct")), 2),
            "avg_daily_dollar_vol_min": round(min(trait_list(group, "avg_daily_dollar_vol")), 0),
            "avg_daily_shares_min": round(min(trait_list(group, "avg_daily_shares")), 0),
            "range_60d_pct_min": round(min(trait_list(group, "range_60d_pct")), 1),
            "beta_vs_qqq_min": round(min(trait_list(group, "beta_vs_qqq")), 2) if trait_list(group, "beta_vs_qqq") else None,
            "beta_vs_qqq_max": round(max(trait_list(group, "beta_vs_qqq")), 2) if trait_list(group, "beta_vs_qqq") else None,
        }

    # Union band that keeps all top winners (for a single IBKR scan pass)
    union = {
        "price_min": round(min(top_price), 2) if top_price else 15.0,
        "price_max": None,  # don't cap — MSFT/META are expensive
        "avg_daily_dollar_vol_min": round(min(top_dollar) * 0.8, 0) if top_dollar else 50_000_000,
        "avg_daily_shares_min": round(min(top_shares) * 0.8, 0) if top_shares else 1_000_000,
        "atr_pct_min": round(min(top_atr) * 0.85, 2) if top_atr else 0.7,
        "atr_pct_max": round(max(top_atr) * 1.25, 2) if top_atr else 5.0,
        "daily_vol_pct_min": round(min(top_dvol) * 0.85, 2) if top_dvol else 1.5,
        "daily_vol_pct_max": round(max(top_dvol) * 1.25, 2) if top_dvol else 12.0,
        "range_60d_pct_min": round(min(top_range) * 0.75, 1) if top_range else 10.0,
        "beta_vs_qqq_min": round(min(top_beta) - 0.2, 2) if top_beta else None,
        "beta_vs_qqq_max": round(max(top_beta) + 0.3, 2) if top_beta else None,
        "shortable": True,
        "exclude_etfs_optional": True,
    }

    ibkr_checklist = [
        {
            "field": "Price",
            "ibkr_scanner": "priceAbove / priceBelow",
            "suggest": f">= ${union['price_min']:.0f} (no hard upper cap; mega-caps OK)",
        },
        {
            "field": "Average Volume (shares)",
            "ibkr_scanner": "avgVolumeAbove",
            "suggest": f">= {union['avg_daily_shares_min']:,.0f} shares/day",
        },
        {
            "field": "Avg Dollar Volume",
            "ibkr_scanner": "proxy: price × avgVolume, or marketCap + volume",
            "suggest": f">= ${union['avg_daily_dollar_vol_min']:,.0f}/day "
                       f"(keeps QBTS-class liquidity floor from top winners)",
        },
        {
            "field": "Volatility / ATR%",
            "ibkr_scanner": "no direct ATR%; use 'Volatility' or historical volatility ~ "
                            f"{union['daily_vol_pct_min']:.1f}–{union['daily_vol_pct_max']:.1f}% daily",
            "suggest": f"Intrabar ATR% ~{union['atr_pct_min']:.1f}–{union['atr_pct_max']:.1f}% "
                       f"(covers both MSFT~1% and QBTS~3.5%)",
        },
        {
            "field": "60d range / momentum width",
            "ibkr_scanner": "52-week / period high-low range, or % change filters",
            "suggest": f"60d high-low range >= ~{union['range_60d_pct_min']:.0f}% "
                       f"(need tradable swings for zone breakouts)",
        },
        {
            "field": "Beta (vs QQQ/SPY)",
            "ibkr_scanner": "betaAbove / betaBelow if available in Fundamental Explorer",
            "suggest": (
                f"beta ~{union['beta_vs_qqq_min']}–{union['beta_vs_qqq_max']} vs QQQ"
                if union["beta_vs_qqq_min"] is not None
                else "prefer market-sensitive names (not deep defensives)"
            ),
        },
        {
            "field": "Shortable",
            "ibkr_scanner": "shortable shares / locate",
            "suggest": "Must be shortable — 2m edge was mostly SHORT PnL",
        },
        {
            "field": "Instrument type",
            "ibkr_scanner": "STK, US, SMART",
            "suggest": "US common stock; optionally run a 2nd scan for high-beta speculative names",
        },
    ]

    return {
        "top_symbols": [r.symbol for r in top],
        "bottom_symbols": [r.symbol for r in bot],
        "top_pnl": {r.symbol: r.pnl for r in top},
        "bottom_pnl": {r.symbol: r.pnl for r in bot},
        "trait_comparison": comparison,
        "clusters": {
            "speculative_high_vol": cluster_band(speculative, "QBTS-like high ATR"),
            "liquid_megacap": cluster_band(megacap, "MSFT/META-like liquid"),
        },
        "union_band_all_top_winners": union,
        "ibkr_scanner_checklist": ibkr_checklist,
        "notes": [
            "Top winners are NOT one profile: QBTS is high-vol/low-price; MSFT/META are liquid mega-caps.",
            "Run TWO IBKR scans (speculative + mega-cap), then union and rank with this script.",
            "Primary rank = strategy PnL on Jun22 params; traits only build the candidate pool.",
            "Re-rank monthly; 2m sample is small — prefer names with >=5 trades and PF>1.",
        ],
    }


def write_screener_md(insights: dict[str, Any], path: Path) -> None:
    lines = [
        "# IBKR screener hints (from Jun22 strategy ranking)",
        "",
        f"**Top PnL names:** {', '.join(insights.get('top_symbols', []))}",
        f"**Bottom PnL names:** {', '.join(insights.get('bottom_symbols', []))}",
        "",
        "## How to use",
        "1. Run **two** IBKR scans (clusters below) → candidate tickers.",
        "2. Prefetch 1h bars: `PYTHONPATH=src ./py scripts/prefetch_ib_bars_cache.py TICKERS --duration \"3 Y\" --bar-size \"1 hour\"`",
        "3. Re-rank: `PYTHONPATH=src ./py scripts/rank_symbols_jun22.py --symbols ...`",
        "4. Keep top PnL names with enough trades on the paper watchlist.",
        "",
        "## Cluster A — speculative / high-vol (QBTS-like)",
    ]
    spec = (insights.get("clusters") or {}).get("speculative_high_vol")
    mega = (insights.get("clusters") or {}).get("liquid_megacap")
    union = insights.get("union_band_all_top_winners") or {}

    def _cluster_block(c: dict | None) -> list[str]:
        if not c:
            return ["_(no names in this cluster in current top set)_", ""]
        return [
            f"- Examples: {', '.join(c.get('symbols') or [])}",
            f"- Price: ${c.get('price_min')} – ${c.get('price_max')}",
            f"- ATR% (1h): {c.get('atr_pct_min')} – {c.get('atr_pct_max')}",
            f"- Daily vol%: {c.get('daily_vol_pct_min')} – {c.get('daily_vol_pct_max')}",
            f"- Min avg daily $ volume: ${c.get('avg_daily_dollar_vol_min'):,.0f}"
            if c.get("avg_daily_dollar_vol_min") else "- Min avg daily $ volume: n/a",
            f"- Min avg daily shares: {c.get('avg_daily_shares_min'):,.0f}"
            if c.get("avg_daily_shares_min") else "- Min avg daily shares: n/a",
            f"- 60d range% >= {c.get('range_60d_pct_min')}",
            f"- Beta vs QQQ: {c.get('beta_vs_qqq_min')} – {c.get('beta_vs_qqq_max')}",
            "",
        ]

    lines += _cluster_block(spec)
    lines += ["## Cluster B — liquid mega-cap (MSFT/META-like)", ""]
    lines += _cluster_block(mega)
    lines += [
        "## Union band (single scan that keeps all top winners)",
        "",
        f"- Price >= ${union.get('price_min')}",
        f"- Avg daily $ volume >= ${union.get('avg_daily_dollar_vol_min'):,.0f}"
        if union.get("avg_daily_dollar_vol_min") else "- Avg daily $ volume: n/a",
        f"- Avg daily shares >= {union.get('avg_daily_shares_min'):,.0f}"
        if union.get("avg_daily_shares_min") else "- Avg daily shares: n/a",
        f"- ATR% ~ {union.get('atr_pct_min')} – {union.get('atr_pct_max')}",
        f"- Daily vol% ~ {union.get('daily_vol_pct_min')} – {union.get('daily_vol_pct_max')}",
        f"- 60d range% >= {union.get('range_60d_pct_min')}",
        f"- Shortable: yes",
        "",
        "## IBKR field mapping",
        "",
    ]
    for row in insights.get("ibkr_scanner_checklist") or []:
        lines.append(f"- **{row['field']}** (`{row['ibkr_scanner']}`): {row['suggest']}")
    lines += ["", "## Notes", ""]
    for n in insights.get("notes") or []:
        lines.append(f"- {n}")
    lines.append("")
    path.write_text("\n".join(lines))


async def run(args: argparse.Namespace) -> int:
    start = pd.Timestamp(args.start, tz="America/New_York")
    end = pd.Timestamp(args.end, tz="America/New_York") + pd.Timedelta(hours=23, minutes=59)
    warm = start - pd.Timedelta(days=args.warm_days)

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.all_cached:
        symbols = sorted({
            p.name.split("-")[0].upper()
            for p in HIST.glob("*-1hour.csv")
            if p.name[0].isalpha() and p.name[0].isupper()
        })
    else:
        symbols = list(DEFAULT_UNIVERSE)

    qqq = load_ohlcv("QQQ")
    qqq_daily = None
    if qqq is not None:
        # Use warm→end so beta has enough overlap even if QQQ cache is shorter than the window.
        qd = qqq[(qqq.index >= warm) & (qqq.index <= end)]["close"].resample("1D").last().dropna()
        qqq_daily = qd.pct_change().dropna()
        if len(qqq_daily) < 20:
            qd = qqq["close"].resample("1D").last().dropna()
            qqq_daily = qd.pct_change().dropna()

    ranks: list[SymbolRank] = []
    all_trades: list[dict] = []
    missing: list[str] = []

    print(f"Window {start.date()} → {end.date()}  warm_days={args.warm_days}")
    print(f"Params: Jun22 paper (risk2k SL1.1 TGT4.8 1h zl20 fresh8h longs+shorts)")
    print(f"Symbols: {symbols}\n")

    for sym in symbols:
        df = load_ohlcv(sym)
        if df is None:
            missing.append(sym)
            print(f"SKIP {sym}: no 1h cache")
            continue
        try:
            traits = compute_traits(df, start, end, qqq_daily)
        except ValueError as e:
            missing.append(f"{sym}({e})")
            print(f"SKIP {sym}: {e}")
            continue
        trades = await backtest_symbol(sym, df, warm, start, end)
        rank = summarize_trades(sym, trades, traits)
        ranks.append(rank)
        all_trades.extend(trades)
        print(
            f"{sym:5s} n={rank.trades:3d} WR={rank.win_rate_pct:5.1f}%  "
            f"PnL=${rank.pnl:+10,.2f}  L={rank.long_n}/{rank.long_pnl:+,.0f}  "
            f"S={rank.short_n}/{rank.short_pnl:+,.0f}  "
            f"ATR%={traits.atr_pct:.2f}  dVol%={traits.daily_vol_pct:.2f}  "
            f"$vol/d=${traits.avg_daily_dollar_vol/1e6:.0f}M  "
            f"betaQ={traits.beta_vs_qqq if traits.beta_vs_qqq is not None else float('nan'):.2f}"
        )

    ranks_sorted = sorted(ranks, key=lambda r: r.pnl, reverse=True)
    insights = build_screener_insights(ranks_sorted, top_n=args.top)

    print("\n=== RANKED BY PnL ===")
    for i, r in enumerate(ranks_sorted, 1):
        print(
            f"{i:2d}. {r.symbol:5s}  PnL=${r.pnl:+10,.2f}  n={r.trades:3d}  "
            f"WR={r.win_rate_pct:5.1f}%  PF={r.profit_factor if r.profit_factor is not None else float('nan'):.2f}"
        )

    print("\n=== SCREENER CLUSTERS ===")
    for name, c in (insights.get("clusters") or {}).items():
        if not c:
            print(f"{name}: (empty)")
            continue
        print(f"{name}: {c['symbols']}")
        print(
            f"  price ${c['price_min']}–${c['price_max']}  "
            f"ATR% {c['atr_pct_min']}–{c['atr_pct_max']}  "
            f"min $/day ${c['avg_daily_dollar_vol_min']:,.0f}"
        )

    print("\n=== IBKR CHECKLIST (union band) ===")
    for row in insights.get("ibkr_scanner_checklist") or []:
        print(f"- {row['field']}: {row['suggest']}")

    LOGS.mkdir(parents=True, exist_ok=True)
    out_json = LOGS / "symbol_rank_jun22.json"
    payload = {
        "window": {"start": str(start.date()), "end": str(end.date()), "warm_days": args.warm_days},
        "params": JUN22_KW,
        "missing": missing,
        "ranking": [
            {
                **{k: getattr(r, k) for k in (
                    "symbol", "trades", "wins", "win_rate_pct", "pnl",
                    "long_n", "long_pnl", "short_n", "short_pnl", "avg_pnl", "profit_factor",
                )},
                "traits": r.traits,
            }
            for r in ranks_sorted
        ],
        "screener_insights": insights,
        "trades": sorted(all_trades, key=lambda t: t["entry"]),
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {out_json}")

    if args.write_screener:
        md = LOGS / "ibkr_screener_hints_jun22.md"
        write_screener_md(insights, md)
        print(f"Wrote {md}")

    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Rank symbols by Jun22 strategy PnL + IBKR screener traits")
    p.add_argument("--symbols", help="Comma-separated tickers (default: built-in universe with caches)")
    p.add_argument("--all-cached", action="store_true", help="Use every SYMBOL-*-1hour.csv in historical_data/")
    p.add_argument("--start", default="2026-05-08", help="Window start YYYY-MM-DD (ET)")
    p.add_argument("--end", default="2026-07-08", help="Window end YYYY-MM-DD (ET)")
    p.add_argument("--warm-days", type=int, default=60, help="Bars before start for indicator warm-up")
    p.add_argument("--top", type=int, default=3, help="Top/bottom N for screener trait comparison")
    p.add_argument("--write-screener", action="store_true", help="Write logs/ibkr_screener_hints_jun22.md")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
