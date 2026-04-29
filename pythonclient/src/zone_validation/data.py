"""
Load cached IB-style OHLCV CSVs (same layout as ibkr/historical_data cache).
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any, Optional

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


def list_matching_cache_csvs(
    data_dir: Path,
    symbol: str,
    bar_size: str = "15 mins",
) -> list[dict[str, Any]]:
    """
    Newest first: files named ``{SYMBOL}-*-{barSize}.csv`` under ``data_dir``.
    """
    if not data_dir.is_dir():
        return []
    suf = bar_size_to_cache_suffix(bar_size)
    pat = f"{symbol.strip().upper()}-*-{suf}.csv"
    out: list[dict[str, Any]] = []
    for p in sorted(
        data_dir.glob(pat),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    ):
        st = p.stat()
        out.append(
            {
                "name": p.name,
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=dt_timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
            }
        )
    return out


def et_day_bounds_for_index(
    index: pd.DatetimeIndex, day_yyyy_mm_dd: str
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """
    ET calendar day window aligned to the *index* timezone (zoneinfo / pytz-consistent for comparisons).

    Using the same ``Timestamp`` type as the bars avoids a bug where pytz ``localize`` bounds do not
    match a ``DatetimeIndex`` produced by ``tz_convert("America/New_York")`` (all ``>=``/``<`` can fail).
    """
    day = day_yyyy_mm_dd.strip()[:10]
    if index.empty:
        return pd.Timestamp(day, tz="America/New_York"), pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(
            days=1
        )
    tz = index.tz
    if tz is not None:
        day_start = pd.Timestamp(day, tz=tz)
    else:
        day_start = pd.Timestamp(day, tz="America/New_York")
    day_end = day_start + pd.Timedelta(days=1)
    return day_start, day_end


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
    day_start, day_end = et_day_bounds_for_index(df.index, day)
    win = (df.index >= day_start) & (df.index < day_end)
    if bool(win.any()):
        return None
    tmin, tmax = df.index.min(), df.index.max()
    return (
        f"No bars for session {day} (ET) in this cache. "
        f"File index range: {tmin} … {tmax}. "
        f"Choose a day inside that range, or refresh/download CSV that includes your session."
    )
