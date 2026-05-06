"""
Shared entry point for CLI and HTTP: load bars, run identify_zones, build Plotly figure.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
from trade_journal import TradeJournal

from . import build_zone_trade_figure
from .data import (
    et_day_bounds_for_index,
    load_ohlcv_csv,
    pick_cache_csv,
    pick_display_session_day,
    session_day_bars_error_hint,
)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ZoneChartError(Exception):
    """User-facing error (bad args, missing cache, empty data). Do not subclass ValueError (API mapping)."""


def _groq_init_param_names() -> set[str]:
    sig = inspect.signature(GroqRecommendationsStrategy.__init__)
    return {p for p in sig.parameters if p != "self"}


def groq_strategy_kwargs_from_run_params(raw: Mapping[str, Any]) -> dict[str, Any]:
    """
    Keep only ``GroqRecommendationsStrategy`` constructor keys; drop journal-only fields
    (e.g. check_interval, strategy_exit_signals).
    """
    names = _groq_init_param_names()
    return {k: v for k, v in raw.items() if k in names}


def _pythonclient_root() -> Path:
    return Path(__file__).resolve().parents[2]


def render_zone_trade_chart(
    *,
    symbol: str,
    day_yyyy_mm_dd: str,
    pythonclient_root: Path | None = None,
    data_dir: Path | None = None,
    csv_path: Path | None = None,
    bar_size: str = "15 mins",
    journal_db: Path | None = None,
    no_journal: bool = False,
    run_id: str | None = None,
    strategy_params: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """
    Returns ``(plotly.graph_objects.Figure, meta_dict)``.

    ``csv_path`` must already be validated by the caller (CLI). For HTTP, resolve under ``data_dir`` only.
    """
    root = pythonclient_root or _pythonclient_root()
    dd = data_dir if data_dir is not None else root / "historical_data"
    jpath = journal_db if journal_db is not None else root / "trade_journal.db"

    sym = symbol.strip().upper()
    day = day_yyyy_mm_dd.strip()[:10]
    if not _DAY_RE.match(day):
        raise ZoneChartError("day must be YYYY-MM-DD")

    if csv_path is None:
        picked = pick_cache_csv(dd, sym, bar_size, prefer_path=None)
        if picked is None:
            raise ZoneChartError(
                f"No cache CSV for {sym} ({bar_size}). "
                f"Add {sym}-*-{bar_size.replace(' ', '')}.csv under {dd} or pass an allowed csv_path."
            )
        csv_path = picked
    else:
        if not csv_path.is_file():
            raise ZoneChartError(f"CSV not found: {csv_path}")

    raw = load_ohlcv_csv(csv_path)
    if raw.empty:
        raise ZoneChartError(f"Empty CSV: {csv_path}")

    ohlcv_trim_note: str | None = None
    ohlcv_rows_full = len(raw)
    # Multi-year 5s (or very long) files make indicator/zone/Plotly JSON take minutes+ and hit client timeouts.
    # Keep a rolling window around the view day so the chart API stays responsive.
    _OHLCV_ROW_WARN = 45_000
    if ohlcv_rows_full > _OHLCV_ROW_WARN:
        _ds, _de = et_day_bounds_for_index(raw.index, day)
        t_lo = max(raw.index.min(), _ds - pd.Timedelta(days=14))
        t_hi = min(_de + pd.Timedelta(hours=12), raw.index.max() + pd.Timedelta(seconds=1))
        w = raw.loc[(raw.index >= t_lo) & (raw.index < t_hi)]
        if len(w) >= 500:
            raw = w
            ohlcv_trim_note = f"using {len(raw):,} of {ohlcv_rows_full:,} rows (14d+session around {day}; dense cache speed cap)"
    _OHLCV_ROW_HARD = 60_000
    if len(raw) > _OHLCV_ROW_HARD:
        n0 = len(raw)
        raw = raw.iloc[-_OHLCV_ROW_HARD :].copy()
        extra = f" kept last {_OHLCV_ROW_HARD:,} of {n0:,} in window" if ohlcv_trim_note else f"kept last {_OHLCV_ROW_HARD:,} of {n0:,} rows for speed"
        ohlcv_trim_note = (ohlcv_trim_note or "dense bar file") + extra

    strat_kw: dict[str, Any] = {}
    if run_id and str(run_id).strip():
        if not jpath.is_file():
            raise ZoneChartError(
                f"Journal DB not found at {jpath}. Cannot load run_id {run_id!r}."
            )
        journal = TradeJournal(jpath)
        run_raw = journal.get_run_params(str(run_id).strip())
        if not run_raw:
            raise ZoneChartError(f"Unknown run_id: {str(run_id).strip()!r} (not in runs table).")
        strat_kw = groq_strategy_kwargs_from_run_params(run_raw)
    strat_kw = {**strat_kw, **dict(strategy_params or {})}
    strat_kw["skip_chromadb"] = True
    strategy = GroqRecommendationsStrategy(**strat_kw)
    df_ind = strategy.calculate_indicators(raw.copy())
    supply, demand = strategy.identify_zones(df_ind)

    trades: list = []
    if not no_journal:
        if not jpath.is_file():
            raise ZoneChartError(
                f"Journal DB not found at {jpath}. Use no_journal to plot without fills."
            )
        journal = TradeJournal(jpath)
        trades = journal.get_trades_touching_calendar_day(day)
        trades = [t for t in trades if (t.get("symbol") or "").strip().upper() == sym]

    plot_day, pick_note = pick_display_session_day(df_ind, day)
    if plot_day is None:
        raise ZoneChartError(pick_note or "No session day to plot")
    if session_day_bars_error_hint(df_ind, plot_day) is not None:
        tmin, tmax = df_ind.index.min(), df_ind.index.max()
        raise ZoneChartError(
            f"Internal: chosen plot day {plot_day!r} has no bars (index {tmin} … {tmax})"
        )

    title = f"{sym} journal {day} | candles {plot_day}"
    if pick_note:
        title = f"{sym} journal {day} | candles {plot_day} (cache fallback)"
    try:
        fig = build_zone_trade_figure(
            df_ind,
            plot_day,
            supply,
            demand,
            trades,
            title=title,
            day_requested=day,
        )
    except ValueError as e:
        raise ZoneChartError(str(e)) from e

    meta: dict[str, Any] = {
        "symbol": sym,
        "day": day,
        "plot_session_day": plot_day,
        "day_note": pick_note,
        "chart_api_version": 2,
        "csv": str(csv_path),
        "bar_rows_total": int(len(df_ind)),
        "cache_index_start": str(df_ind.index.min()),
        "cache_index_end": str(df_ind.index.max()),
        "supply_zones": len(supply),
        "demand_zones": len(demand),
        "journal_trades_plotted": len(trades),
    }
    fig_meta = fig.layout.meta if isinstance(fig.layout.meta, dict) else {}
    if fig_meta:
        meta.update(fig_meta)
        if (
            meta.get("supply_zones_plotted") != len(supply)
            or meta.get("demand_zones_plotted") != len(demand)
        ):
            meta["zone_plot_note"] = (
                f"collapsed {len(supply)} supply and {len(demand)} demand levels into "
                f"{meta.get('supply_zone_bands_plotted', 0)} supply / "
                f"{meta.get('demand_zone_bands_plotted', 0)} demand band(s)"
            )
        if meta.get("projected_off_session_fill_markers"):
            meta["fill_marker_note"] = (
                f"{meta['projected_off_session_fill_markers']} fill marker(s) are outside the candle session "
                f"and are shown at the right edge with actual time in hover"
            )
        if meta.get("decision_zones_plotted"):
            meta["decision_zone_note"] = (
                f"plotted {meta['decision_zones_plotted']} exact decision zone(s) captured at signal time"
            )
        elif meta.get("trades_missing_decision_zone"):
            meta["decision_zone_note"] = (
                "Exact decision zone was not captured for this trade; showing reconstructed context only."
            )
    if ohlcv_rows_full > _OHLCV_ROW_WARN or ohlcv_trim_note:
        meta["ohlcv_rows_in_file"] = ohlcv_rows_full
    if ohlcv_trim_note:
        meta["ohlcv_note"] = ohlcv_trim_note
    if run_id and str(run_id).strip():
        meta["run_id"] = str(run_id).strip()
        meta["params_from_run"] = True
    return fig, meta


def parse_strategy_params_json(raw: str | None) -> dict[str, Any]:
    if raw is None or not str(raw).strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ZoneChartError(f"strategy_params is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ZoneChartError("strategy_params JSON must be an object")
    return obj


def resolve_csv_under_data_dir(data_dir: Path, csv_basename: str) -> Path:
    """Allow only a single filename under ``data_dir`` (no path traversal)."""
    raw = csv_basename.strip()
    if not raw or "/" in raw or "\\" in raw or ".." in raw:
        raise ZoneChartError("csv_file must be a single filename (no directories)")
    if raw.startswith("."):
        raise ZoneChartError("csv_file cannot start with '.'")
    if Path(raw).name != raw:
        raise ZoneChartError("csv_file must be a basename only")
    path = (data_dir.resolve() / raw).resolve()
    base = data_dir.resolve()
    try:
        path.relative_to(base)
    except ValueError as e:
        raise ZoneChartError("csv_file must stay under data_dir") from e
    return path
