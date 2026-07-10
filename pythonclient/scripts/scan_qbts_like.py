#!/usr/bin/env python3
"""
QBTS-like IBKR screener / scanner.

Finds US stocks with characteristics similar to the high-vol cluster that printed
in the Jun22 strategy ranking:

  ATR% (from daily bars)  ~ 3–4%   (default band 2.5–5.0)
  Daily return vol        ~ 7–9%   (default band 5.5–12.0)
  Avg dollar volume       ≳ $500M–$1B/day  (default min $500M)

Pipeline:
  1) IBKR market scanner (MOST_ACTIVE / HOT_BY_VOLUME / TOP_PERC_GAIN|LOSS) → candidates
  2) Pull ~60 trading days of daily bars per candidate
  3) Compute ATR%, daily vol%, avg $/day volume, 60d range
  4) Keep names inside the QBTS-like bands; write ranked CSV/JSON

From pythonclient/:

  # Live IBKR scan (paper gateway :4002)
  PYTHONPATH=src ./py scripts/scan_qbts_like.py

  # Tighter / looser bands
  PYTHONPATH=src ./py scripts/scan_qbts_like.py --atr-min 3 --atr-max 4 --dvol-min 7 --dvol-max 9 \\
      --min-dollar-vol 5e8 --max-dollar-vol 2e9

  # Seed list only (no scanner), still needs IB for daily bars unless --offline-cache
  PYTHONPATH=src ./py scripts/scan_qbts_like.py --seeds-file watchlist.txt --no-ib-scan

  # Offline: score symbols that already have 1h/daily caches (no IB)
  PYTHONPATH=src ./py scripts/scan_qbts_like.py --offline-cache

Requires TWS / IB Gateway for live scan (default paper port 4002).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import pytz
from ib_insync import ScannerSubscription, Stock

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ibkr.connection import IBKRConnection  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("scan_qbts_like")

HIST = _ROOT / "historical_data"
LOGS = _ROOT / "logs"

# Extra seeds often near QBTS-like vol/liquidity (merged with IB scanner hits)
DEFAULT_SEEDS = [
    "QBTS", "IONQ", "RGTI", "QUBT", "SOUN", "BBAI", "AI", "PATH", "UPST", "AFRM",
    "MARA", "RIOT", "COIN", "HOOD", "SOFI", "PLTR", "SMCI", "ARM", "MSTR", "OKLO",
    "SMR", "ASTS", "RKLB", "JOBY", "LCID", "RIVN", "NIO", "XPEV", "LI", "SNAP",
    "PINS", "ROKU", "DKNG", "PENN", "CVNA", "CRSP", "BEAM", "NTLA",
    "SNOW", "DDOG", "NET", "CRWD", "PANW", "ZS", "MDB", "SHOP", "SQ", "PYPL",
    "U", "RBLX", "TTD", "ABNB", "DASH", "UBER", "LYFT", "SE", "MELI",
]

# Common ETFs / leveraged products to drop unless --include-etfs
ETF_BLOCKLIST = {
    "QQQ", "SPY", "IWM", "DIA", "TQQQ", "SQQQ", "UPRO", "SPXU", "SOXL", "SOXS",
    "TNA", "TZA", "FAS", "FAZ", "QLD", "QID", "SSO", "SDS", "SLV", "GLD", "USO",
    "XLE", "XLF", "XLK", "SMH", "ARKK", "ARKG", "ARKW", "VXX", "UVXY", "SVXY",
}


@dataclass
class ScanHit:
    symbol: str
    last: float
    atr_pct: float
    daily_vol_pct: float
    avg_daily_dollar_vol: float
    avg_daily_shares: float
    range_60d_pct: float
    mean_abs_daily_pct: float
    n_days: int
    source: str  # scanner / seed / cache
    pass_filter: bool
    reject_reason: str = ""


def _finite(x: float, default: float = float("nan")) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _atr_pct_simple(ohlc: pd.DataFrame, window: int = 14) -> float:
    """Mean (bar range / close) over ``window`` — matches rank_symbols_jun22 1h ATR%."""
    rng = (ohlc["high"] - ohlc["low"]).abs() / ohlc["close"].replace(0, np.nan)
    return float(rng.rolling(window).mean().dropna().mean() * 100)


def compute_metrics(
    daily: pd.DataFrame | None,
    hourly: pd.DataFrame | None = None,
) -> dict[str, float] | None:
    """
    Metrics aligned with the Jun22 ranking screener notes:

      atr_pct      — prefer **1h** mean range/close (QBTS was ~3.5% on 1h);
                     fall back to daily range/close if no hourly
      daily_vol%   — stdev of **daily** close-to-close returns
      $/day volume — mean(daily close * daily volume)
    """
    if daily is None or len(daily) < 20:
        # synthesize daily from hourly if needed
        if hourly is None or len(hourly) < 50:
            return None
        daily = pd.DataFrame(
            {
                "open": hourly["open"].resample("1D").first(),
                "high": hourly["high"].resample("1D").max(),
                "low": hourly["low"].resample("1D").min(),
                "close": hourly["close"].resample("1D").last(),
                "volume": hourly["volume"].resample("1D").sum(),
            }
        ).dropna(subset=["close"])
        daily = daily[daily["volume"] > 0]

    d = daily[["open", "high", "low", "close", "volume"]].dropna().sort_index()
    if len(d) < 20:
        return None

    if hourly is not None and len(hourly) >= 50:
        h = hourly[["open", "high", "low", "close"]].dropna().sort_index()
        # last ~60 trading days of 1h bars
        cutoff = d.index.max() - pd.Timedelta(days=70)
        h = h[h.index >= cutoff]
        atr_pct = _atr_pct_simple(h) if len(h) >= 30 else _atr_pct_simple(d)
        atr_source = "1h"
    else:
        atr_pct = _atr_pct_simple(d)
        atr_source = "1d"

    rets = d["close"].pct_change().dropna()
    daily_vol = float(rets.std() * 100) if len(rets) else float("nan")
    mean_abs = float(rets.abs().mean() * 100) if len(rets) else float("nan")

    dollar = (d["close"] * d["volume"]).replace(0, np.nan).dropna()
    avg_dollar = float(dollar.mean()) if len(dollar) else 0.0
    avg_shares = float(d["volume"].replace(0, np.nan).dropna().mean()) if len(d) else 0.0

    tail = d["close"].tail(min(60, len(d)))
    range_pct = float((tail.max() / tail.min() - 1) * 100) if len(tail) >= 5 else float("nan")

    return {
        "last": float(d["close"].iloc[-1]),
        "atr_pct": atr_pct,
        "atr_source": 1.0 if atr_source == "1h" else 0.0,  # JSON-friendly flag
        "daily_vol_pct": daily_vol,
        "avg_daily_dollar_vol": avg_dollar,
        "avg_daily_shares": avg_shares,
        "range_60d_pct": range_pct,
        "mean_abs_daily_pct": mean_abs,
        "n_days": float(len(d)),
    }


def load_1h_cache(sym: str) -> pd.DataFrame | None:
    cands = sorted(HIST.glob(f"{sym}-*-1hour.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not cands:
        return None
    df = pd.read_csv(cands[0], index_col="date", parse_dates=False)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")
    return df[["open", "high", "low", "close", "volume"]].sort_index()


def daily_from_1h(ohlcv: pd.DataFrame) -> pd.DataFrame:
    daily = pd.DataFrame(
        {
            "open": ohlcv["open"].resample("1D").first(),
            "high": ohlcv["high"].resample("1D").max(),
            "low": ohlcv["low"].resample("1D").min(),
            "close": ohlcv["close"].resample("1D").last(),
            "volume": ohlcv["volume"].resample("1D").sum(),
        }
    ).dropna(subset=["close"])
    return daily[daily["volume"] > 0].tail(90)


def passes_qbts_band(
    m: dict[str, float],
    *,
    atr_min: float,
    atr_max: float,
    dvol_min: float,
    dvol_max: float,
    min_dollar: float,
    max_dollar: float | None,
    price_min: float,
    price_max: float | None,
    range_min: float,
    symbol: str = "",
    include_etfs: bool = False,
) -> tuple[bool, str]:
    if not include_etfs and symbol.upper() in ETF_BLOCKLIST:
        return False, "etf_blocklist"
    last = m["last"]
    if last < price_min:
        return False, f"price {last:.2f} < {price_min}"
    if price_max is not None and last > price_max:
        return False, f"price {last:.2f} > {price_max}"
    if m["atr_pct"] < atr_min or m["atr_pct"] > atr_max:
        return False, f"atr% {m['atr_pct']:.2f} outside [{atr_min},{atr_max}]"
    if m["daily_vol_pct"] < dvol_min or m["daily_vol_pct"] > dvol_max:
        return False, f"dvol% {m['daily_vol_pct']:.2f} outside [{dvol_min},{dvol_max}]"
    if m["avg_daily_dollar_vol"] < min_dollar:
        return False, f"$vol/d {m['avg_daily_dollar_vol']:.0f} < {min_dollar:.0f}"
    if max_dollar is not None and m["avg_daily_dollar_vol"] > max_dollar:
        return False, f"$vol/d {m['avg_daily_dollar_vol']:.0f} > {max_dollar:.0f}"
    if m["range_60d_pct"] < range_min:
        return False, f"60d range {m['range_60d_pct']:.1f}% < {range_min}"
    return True, ""


async def ib_scanner_symbols(ib, scan_codes: list[str], rows: int, above_price: float, above_volume: int) -> list[str]:
    """Run one or more IBKR scanner codes; return unique tickers."""
    found: list[str] = []
    seen: set[str] = set()
    for code in scan_codes:
        sub = ScannerSubscription(
            instrument="STK",
            locationCode="STK.US.MAJOR",
            scanCode=code,
            numberOfRows=rows,
            abovePrice=above_price,
            aboveVolume=above_volume,
            stockTypeFilter="CORP",
        )
        # Prefer common stocks; TagValue filters vary by TWS version — fail soft.
        try:
            logger.info("IB scanner %s (rows=%s abovePrice=%s aboveVolume=%s)", code, rows, above_price, above_volume)
            results = await ib.reqScannerDataAsync(sub)
        except Exception as e:
            logger.error("Scanner %s failed: %s", code, e)
            continue

        for item in results or []:
            c = getattr(item, "contractDetails", None)
            contract = getattr(c, "contract", None) if c is not None else getattr(item, "contract", None)
            sym = getattr(contract, "symbol", None) if contract is not None else None
            sec = getattr(contract, "secType", "STK") if contract is not None else "STK"
            if not sym or sec != "STK":
                continue
            sym = str(sym).upper()
            if sym in seen:
                continue
            seen.add(sym)
            found.append(sym)
        await asyncio.sleep(1.0)  # pacing between scan codes
    return found


async def fetch_hourly_bars(ib, symbol: str, days: int = 15) -> pd.DataFrame | None:
    """Short 1h history for ATR% (matches rank_symbols_jun22 definition)."""
    contract = Stock(symbol, "SMART", "USD")
    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            return None
        contract = qualified[0]
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{max(days, 5)} D",
            barSizeSetting="1 hour",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            return None
        df = pd.DataFrame(
            {
                "date": [b.date for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            }
        )
        df["date"] = pd.to_datetime(df["date"])
        if getattr(df["date"].dt, "tz", None) is None:
            df["date"] = df["date"].dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
        else:
            df["date"] = df["date"].dt.tz_convert("America/New_York")
        return df.dropna(subset=["date"]).set_index("date").sort_index()
    except Exception as e:
        logger.debug("Hourly bars failed for %s: %s", symbol, e)
        return None


async def fetch_daily_bars(ib, symbol: str, days: int = 90) -> pd.DataFrame | None:
    """Fetch daily bars from IB (useRTH=True)."""
    contract = Stock(symbol, "SMART", "USD")
    try:
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified:
            logger.warning("Could not qualify %s", symbol)
            return None
        contract = qualified[0]
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{max(days, 30)} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        if not bars:
            return None
        df = pd.DataFrame(
            {
                "date": [b.date for b in bars],
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            }
        )
        df["date"] = pd.to_datetime(df["date"])
        if getattr(df["date"].dt, "tz", None) is None:
            # IB sometimes returns date-only
            df["date"] = df["date"].dt.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
        else:
            df["date"] = df["date"].dt.tz_convert("America/New_York")
        df = df.dropna(subset=["date"]).set_index("date").sort_index()
        return df
    except Exception as e:
        logger.warning("Daily bars failed for %s: %s", symbol, e)
        return None


def load_seeds(path: str | None, extra: Iterable[str]) -> list[str]:
    seeds: list[str] = []
    seen: set[str] = set()
    for s in list(extra) + list(DEFAULT_SEEDS):
        u = s.strip().upper()
        if u and u not in seen:
            seen.add(u)
            seeds.append(u)
    if path:
        p = Path(path)
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                u = line.split()[0].upper()
                if u not in seen:
                    seen.add(u)
                    seeds.append(u)
    return seeds


async def run(args: argparse.Namespace) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    max_dollar = args.max_dollar_vol if args.max_dollar_vol and args.max_dollar_vol > 0 else None
    price_max = args.price_max if args.price_max and args.price_max > 0 else None

    band_kw = dict(
        atr_min=args.atr_min,
        atr_max=args.atr_max,
        dvol_min=args.dvol_min,
        dvol_max=args.dvol_max,
        min_dollar=args.min_dollar_vol,
        max_dollar=max_dollar,
        price_min=args.price_min,
        price_max=price_max,
        range_min=args.range_min,
        include_etfs=bool(args.include_etfs),
    )

    print("QBTS-like band:")
    print(f"  ATR%          [{args.atr_min}, {args.atr_max}]")
    print(f"  daily vol%    [{args.dvol_min}, {args.dvol_max}]")
    print(f"  $/day volume  >= ${args.min_dollar_vol:,.0f}"
          + (f"  <= ${max_dollar:,.0f}" if max_dollar else ""))
    print(f"  price         >= ${args.price_min}"
          + (f"  <= ${price_max}" if price_max else ""))
    print(f"  60d range%    >= {args.range_min}")

    candidates: dict[str, str] = {}  # symbol -> source

    if args.offline_cache:
        # Score whatever we already have cached
        for p in HIST.glob("*-1hour.csv"):
            sym = p.name.split("-")[0].upper()
            if sym and sym[0].isalpha():
                candidates.setdefault(sym, "cache")
        for s in load_seeds(args.seeds_file, args.seed or []):
            candidates.setdefault(s, "seed")
        print(f"\nOffline mode: {len(candidates)} cached/seed symbols")
        hits = await score_offline(candidates, band_kw)
        return write_outputs(hits, args, band_kw)

    # Live IB path
    seeds = load_seeds(args.seeds_file, args.seed or [])
    for s in seeds:
        candidates[s] = "seed"

    port = 7497 if args.live else 4002
    cid = args.client_id if args.client_id is not None else random.randint(40, 9999)
    conn = IBKRConnection(host=args.host, port=port, client_id=cid, lax_paper=True)
    try:
        if not await conn.connect():
            logger.error("IBKR connection failed on %s:%s", args.host, port)
            return 2
        ib = conn.get_ib()

        if not args.no_ib_scan:
            scan_codes = [c.strip() for c in args.scan_codes.split(",") if c.strip()]
            scanned = await ib_scanner_symbols(
                ib,
                scan_codes=scan_codes,
                rows=args.scanner_rows,
                above_price=args.price_min,
                above_volume=args.scanner_above_volume,
            )
            for s in scanned:
                candidates.setdefault(s, "scanner")
            print(f"IB scanner returned {len(scanned)} symbols; total candidates={len(candidates)}")
        else:
            print(f"Skipping IB scanner; candidates from seeds only: {len(candidates)}")

        hits: list[ScanHit] = []
        # Cap work for pacing
        symbols = list(candidates.keys())[: args.max_symbols]
        print(f"Fetching daily (+ short 1h for ATR) for {len(symbols)} symbols (pacing ~{args.pace_sec}s)...")
        for i, sym in enumerate(symbols, 1):
            src = candidates[sym]
            daily = await fetch_daily_bars(ib, sym, days=args.lookback_days)
            await asyncio.sleep(args.pace_sec)
            hourly = load_1h_cache(sym)
            if hourly is None and not args.skip_hourly_atr:
                hourly = await fetch_hourly_bars(ib, sym, days=15)
                await asyncio.sleep(args.pace_sec * 0.5)
            if (daily is None or daily.empty) and hourly is not None:
                daily = daily_from_1h(hourly)
                src = f"{src}+cache"
            m = compute_metrics(daily, hourly) if daily is not None or hourly is not None else None
            if not m:
                logger.info("[%d/%d] %s: no bars", i, len(symbols), sym)
                continue
            ok, why = passes_qbts_band(m, symbol=sym, **band_kw)
            hit = ScanHit(
                symbol=sym,
                last=m["last"],
                atr_pct=m["atr_pct"],
                daily_vol_pct=m["daily_vol_pct"],
                avg_daily_dollar_vol=m["avg_daily_dollar_vol"],
                avg_daily_shares=m["avg_daily_shares"],
                range_60d_pct=m["range_60d_pct"],
                mean_abs_daily_pct=m["mean_abs_daily_pct"],
                n_days=int(m["n_days"]),
                source=src,
                pass_filter=ok,
                reject_reason="" if ok else why,
            )
            hits.append(hit)
            flag = "PASS" if ok else "fail"
            atr_tag = "1h" if m.get("atr_source", 0) else "1d"
            print(
                f"[{i}/{len(symbols)}] {flag:4s} {sym:6s}  px={m['last']:7.2f}  "
                f"ATR%{atr_tag}={m['atr_pct']:5.2f}  dVol%={m['daily_vol_pct']:5.2f}  "
                f"$vol/d=${m['avg_daily_dollar_vol']/1e6:7.0f}M  "
                f"rng60={m['range_60d_pct']:5.1f}%  {'' if ok else why}"
            )

        return write_outputs(hits, args, band_kw)
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass


async def score_offline(candidates: dict[str, str], band_kw: dict) -> list[ScanHit]:
    hits: list[ScanHit] = []
    for i, (sym, src) in enumerate(sorted(candidates.items()), 1):
        hourly = load_1h_cache(sym)
        if hourly is None or hourly.empty:
            continue
        daily = daily_from_1h(hourly)
        m = compute_metrics(daily, hourly)
        if not m:
            continue
        ok, why = passes_qbts_band(m, symbol=sym, **band_kw)
        hits.append(
            ScanHit(
                symbol=sym,
                last=m["last"],
                atr_pct=m["atr_pct"],
                daily_vol_pct=m["daily_vol_pct"],
                avg_daily_dollar_vol=m["avg_daily_dollar_vol"],
                avg_daily_shares=m["avg_daily_shares"],
                range_60d_pct=m["range_60d_pct"],
                mean_abs_daily_pct=m["mean_abs_daily_pct"],
                n_days=int(m["n_days"]),
                source=src,
                pass_filter=ok,
                reject_reason="" if ok else why,
            )
        )
        flag = "PASS" if ok else "fail"
        print(
            f"[{i}] {flag:4s} {sym:6s}  px={m['last']:7.2f}  "
            f"ATR%1h={m['atr_pct']:5.2f}  dVol%={m['daily_vol_pct']:5.2f}  "
            f"$vol/d=${m['avg_daily_dollar_vol']/1e6:7.0f}M  "
            f"{'' if ok else why}"
        )
    return hits


def write_outputs(hits: list[ScanHit], args: argparse.Namespace, band_kw: dict) -> int:
    passed = [h for h in hits if h.pass_filter]
    # Rank: closest to QBTS centroid (ATR~3.5, dvol~8, prefer higher liquidity)
    def score(h: ScanHit) -> float:
        atr_pen = abs(h.atr_pct - 3.5)
        dvol_pen = abs(h.daily_vol_pct - 8.0)
        # prefer more liquid within band (log scale)
        liq = math.log10(max(h.avg_daily_dollar_vol, 1.0))
        return -(atr_pen + dvol_pen) + 0.15 * liq

    passed_sorted = sorted(passed, key=score, reverse=True)
    all_sorted = sorted(hits, key=lambda h: (not h.pass_filter, -score(h) if h.pass_filter else 0))

    print(f"\n=== PASS ({len(passed_sorted)}/{len(hits)}) — QBTS-like ===")
    if not passed_sorted:
        print("(none — loosen bands or expand scanner/seeds)")
    for h in passed_sorted:
        print(
            f"  {h.symbol:6s}  px={h.last:7.2f}  ATR%={h.atr_pct:5.2f}  "
            f"dVol%={h.daily_vol_pct:5.2f}  $vol/d=${h.avg_daily_dollar_vol/1e6:,.0f}M  "
            f"rng60={h.range_60d_pct:5.1f}%  src={h.source}"
        )

    stamp = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y%m%d_%H%M%S")
    out_json = LOGS / f"qbts_like_scan_{stamp}.json"
    out_csv = LOGS / f"qbts_like_scan_{stamp}.csv"
    latest_json = LOGS / "qbts_like_scan_latest.json"
    latest_csv = LOGS / "qbts_like_scan_latest.csv"
    watch_out = LOGS / "qbts_like_watchlist.txt"

    payload = {
        "generated_at": stamp,
        "band": band_kw,
        "n_scored": len(hits),
        "n_pass": len(passed_sorted),
        "passed": [asdict(h) for h in passed_sorted],
        "all": [asdict(h) for h in all_sorted],
    }
    out_json.write_text(json.dumps(payload, indent=2, default=str))
    latest_json.write_text(json.dumps(payload, indent=2, default=str))

    rows = [asdict(h) for h in all_sorted]
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    pd.DataFrame(rows).to_csv(latest_csv, index=False)

    watch_out.write_text(
        "# QBTS-like scan hits (paste into watchlist or rank_symbols_jun22.py)\n"
        + "\n".join(h.symbol for h in passed_sorted)
        + ("\n" if passed_sorted else "")
    )

    print(f"\nWrote {out_json}")
    print(f"Wrote {out_csv}")
    print(f"Wrote {watch_out}")
    if passed_sorted:
        print("\nNext:")
        print("  # Prefetch 1h history for ranking")
        print(
            "  PYTHONPATH=src ./py scripts/prefetch_ib_bars_cache.py "
            + " ".join(h.symbol for h in passed_sorted[:20])
            + ' --duration "3 Y" --bar-size "1 hour"'
        )
        print(
            "  PYTHONPATH=src ./py scripts/rank_symbols_jun22.py --symbols "
            + ",".join(h.symbol for h in passed_sorted[:20])
            + " --write-screener"
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="IBKR scanner for QBTS-like high-vol liquid stocks")
    # Bands (defaults = QBTS cluster ± slack)
    p.add_argument("--atr-min", type=float, default=2.5, help="Min ATR%% (default 2.5; core 3–4)")
    p.add_argument("--atr-max", type=float, default=5.0, help="Max ATR%% (default 5.0)")
    p.add_argument("--dvol-min", type=float, default=5.5, help="Min daily return vol%% (default 5.5; core 7–9)")
    p.add_argument("--dvol-max", type=float, default=12.0, help="Max daily return vol%% (default 12)")
    p.add_argument("--min-dollar-vol", type=float, default=5e8, help="Min avg $/day volume (default 5e8)")
    p.add_argument("--max-dollar-vol", type=float, default=0, help="Max avg $/day volume (0=no cap)")
    p.add_argument("--price-min", type=float, default=10.0)
    p.add_argument("--price-max", type=float, default=80.0, help="Soft cap for speculative cluster (0=no cap)")
    p.add_argument("--range-min", type=float, default=20.0, help="Min 60d high-low range %%")

    # IB / scanner
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--live", action="store_true", help="Use live gateway port 7497 (default paper 4002)")
    p.add_argument("--client-id", type=int, default=None)
    p.add_argument(
        "--scan-codes",
        default="MOST_ACTIVE,HOT_BY_VOLUME,TOP_PERC_GAIN,TOP_PERC_LOSS,HOT_BY_PRICE",
        help="Comma-separated IBKR scanCode values",
    )
    p.add_argument("--scanner-rows", type=int, default=50)
    p.add_argument(
        "--scanner-above-volume",
        type=int,
        default=5_000_000,
        help="IB scanner aboveVolume filter (shares)",
    )
    p.add_argument("--no-ib-scan", action="store_true", help="Skip IB market scanner; seeds only")
    p.add_argument("--offline-cache", action="store_true", help="No IB; score local 1h caches only")
    p.add_argument("--seeds-file", default=None, help="Extra tickers file (one per line)")
    p.add_argument("--seed", action="append", default=[], help="Extra ticker (repeatable)")
    p.add_argument("--max-symbols", type=int, default=80, help="Cap symbols to fetch daily bars for")
    p.add_argument("--lookback-days", type=int, default=90)
    p.add_argument("--pace-sec", type=float, default=1.2, help="Sleep between IB historical requests")
    p.add_argument(
        "--skip-hourly-atr",
        action="store_true",
        help="Do not fetch short 1h bars for ATR%% (uses daily ATR%% fallback; QBTS-like band will look too high)",
    )
    p.add_argument(
        "--include-etfs",
        action="store_true",
        help="Allow known ETFs (QQQ/TQQQ/SLV/…) through the filter (default: exclude)",
    )

    args = p.parse_args()
    # price_max 0 means uncapped
    if args.price_max == 0:
        args.price_max = 0.0
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
