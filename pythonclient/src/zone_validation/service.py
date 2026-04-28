"""
Shared entry point for CLI and HTTP: load bars, run identify_zones, build Plotly figure.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import Any, Mapping

from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
from trade_journal import TradeJournal

from . import build_zone_trade_figure, load_ohlcv_csv, pick_cache_csv

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ZoneChartError(ValueError):
    """User-facing error (bad args, missing cache, empty data)."""


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

    title = f"{sym} {day} | zones + journal | {csv_path.name}"
    fig = build_zone_trade_figure(
        df_ind,
        day,
        supply,
        demand,
        trades,
        title=title,
    )

    meta: dict[str, Any] = {
        "symbol": sym,
        "day": day,
        "csv": str(csv_path),
        "bar_rows_total": int(len(df_ind)),
        "supply_zones": len(supply),
        "demand_zones": len(demand),
        "journal_trades_plotted": len(trades),
    }
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
