"""
Load cached IB-style OHLCV CSVs (same layout as ibkr/historical_data cache).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import pytz

_ET = pytz.timezone("America/New_York")


def bar_size_to_cache_suffix(bar_size: str) -> str:
    """``15 mins`` -> ``15mins`` (matches HistoricalDataManager cache filenames)."""
    return bar_size.replace(" ", "")


def pick_cache_csv(
    data_dir: Path,
    symbol: str,
    bar_size: str = "15 mins",
    *,
    prefer_path: Optional[Path] = None,
) -> Optional[Path]:
    """
    Return the newest matching cache file under ``data_dir``, or ``prefer_path`` if it exists.

    Pattern: ``{SYMBOL}-{start}-{end}-{barsize}.csv`` e.g. ``NVDA-2023-04-01-2026-04-28-15mins.csv``.
    """
    if prefer_path is not None and prefer_path.is_file():
        return prefer_path.resolve()
    if not data_dir.is_dir():
        return None
    suf = bar_size_to_cache_suffix(bar_size)
    pat = f"{symbol.strip().upper()}-*-{suf}.csv"
    matches = sorted(
        data_dir.glob(pat),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return matches[0] if matches else None


def load_ohlcv_csv(path: Path) -> pd.DataFrame:
    """
    Read cache CSV with index column ``date``; normalize to US/Eastern (same as HistoricalDataManager._get_latest_cached_data).
    """
    path = path.resolve()
    df = pd.read_csv(path, index_col="date", parse_dates=False)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index, utc=True)
    df.index = df.index.tz_convert("America/New_York")
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def session_day_bars_error_hint(df: pd.DataFrame, day_yyyy_mm_dd: str) -> str | None:
    """
    If the index has at least one bar on the given ET calendar day, return ``None``.

    Otherwise return a user-facing message with the data's index range (for picking a valid day).
    """
    if df.empty:
        return "OHLCV is empty after load."
    day = day_yyyy_mm_dd.strip()[:10]
    d0 = datetime.strptime(day, "%Y-%m-%d")
    day_start = pd.Timestamp(_ET.localize(d0))
    day_end = day_start + pd.Timedelta(days=1)
    win = (df.index >= day_start) & (df.index < day_end)
    if bool(win.any()):
        return None
    tmin, tmax = df.index.min(), df.index.max()
    return (
        f"No bars for session {day} (ET) in this cache. "
        f"File index range: {tmin} … {tmax}. "
        f"Choose a day inside that range, or refresh/download CSV that includes your session."
    )
