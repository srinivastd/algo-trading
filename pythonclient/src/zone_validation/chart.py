"""
Plotly figure: candlesticks, zone bands, journal entry/exit markers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence, Tuple

import pandas as pd
import pytz

from .data import et_day_bounds_for_index

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError as e:  # pragma: no cover
    raise ImportError("zone_validation chart requires plotly; pip install plotly") from e

ET = pytz.timezone("America/New_York")

Zone = Tuple[pd.Timestamp, float, float]


def _parse_fill_ts(raw: str | None) -> pd.Timestamp | None:
    if not raw or not str(raw).strip():
        return None
    t = pd.to_datetime(str(raw).strip(), utc=True, errors="coerce")
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(ET)


def build_zone_trade_figure(
    ohlcv: pd.DataFrame,
    session_day: str,
    supply_zones: Sequence[Zone],
    demand_zones: Sequence[Zone],
    trades: list[dict[str, Any]] | None = None,
    *,
    title: str | None = None,
) -> go.Figure:
    """
    Candlesticks + volume for ``session_day`` (ET); overlay zones that intersect the window;
    scatter entry/exit prices from journal rows for that symbol/day.

    Zones are drawn from pivot time through end of session day (horizontal bands).
    """
    trades = trades or []
    day_start, day_end = et_day_bounds_for_index(ohlcv.index, session_day)

    win = (ohlcv.index >= day_start) & (ohlcv.index < day_end)
    plot_df = ohlcv.loc[win].copy()
    if plot_df.empty:
        raise ValueError(f"No bars in {session_day} (ET) after filtering index.")

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.72, 0.28],
    )

    fig.add_trace(
        go.Candlestick(
            x=plot_df.index,
            open=plot_df["open"],
            high=plot_df["high"],
            low=plot_df["low"],
            close=plot_df["close"],
            name="OHLC",
        ),
        row=1,
        col=1,
    )

    if "volume" in plot_df.columns:
        fig.add_trace(
            go.Bar(x=plot_df.index, y=plot_df["volume"], name="Volume", marker_color="rgba(100,100,200,0.5)"),
            row=2,
            col=1,
        )

    hi_max = float(plot_df["high"].max()) if len(plot_df) else 0.0
    for zt, hi, lo in supply_zones:
        zt = pd.Timestamp(zt)
        if zt.tzinfo is None:
            zt = zt.tz_localize(ET)
        else:
            zt = zt.tz_convert(ET)
        if zt > day_end or (hi_max > 0 and lo > hi_max * 1.2):
            continue
        x0 = max(zt, day_start)
        x1 = day_end
        fig.add_shape(
            type="rect",
            x0=x0,
            x1=x1,
            y0=lo,
            y1=hi,
            fillcolor="rgba(255,80,80,0.12)",
            line=dict(width=0),
            layer="below",
            row=1,
            col=1,
        )

    for zt, hi, lo in demand_zones:
        zt = pd.Timestamp(zt)
        if zt.tzinfo is None:
            zt = zt.tz_localize(ET)
        else:
            zt = zt.tz_convert(ET)
        if zt > day_end:
            continue
        x0 = max(zt, day_start)
        x1 = day_end
        fig.add_shape(
            type="rect",
            x0=x0,
            x1=x1,
            y0=lo,
            y1=hi,
            fillcolor="rgba(80,200,120,0.12)",
            line=dict(width=0),
            layer="below",
            row=1,
            col=1,
        )

    fig.update_layout(
        title=title or f"Zones + journal {session_day}",
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        height=720,
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    ex, ey, etext = [], [], []
    xx, xy, xtext = [], [], []
    for t in trades:
        tid = str(t.get("trade_id", ""))
        e_ts = _parse_fill_ts(t.get("entry_fill_time"))
        x_ts = _parse_fill_ts(t.get("exit_fill_time"))
        e_px = t.get("entry_fill_price")
        x_px = t.get("exit_fill_price")
        if e_ts is not None and day_start <= e_ts < day_end and e_px is not None:
            ex.append(e_ts)
            ey.append(float(e_px))
            etext.append(f"entry {tid}")
        if x_ts is not None and day_start <= x_ts < day_end and x_px is not None:
            xx.append(x_ts)
            xy.append(float(x_px))
            xtext.append(f"exit {tid}")
    if ex:
        fig.add_trace(
            go.Scatter(
                x=ex,
                y=ey,
                mode="markers",
                marker=dict(symbol="triangle-up", size=12, color="lime", line=dict(width=1, color="white")),
                name="Entry fills",
                text=etext,
                hovertemplate="%{text}<br>%{x}<br>%{y:.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )
    if xx:
        fig.add_trace(
            go.Scatter(
                x=xx,
                y=xy,
                mode="markers",
                marker=dict(symbol="x", size=10, color="orange", line=dict(width=1)),
                name="Exit fills",
                text=xtext,
                hovertemplate="%{text}<br>%{x}<br>%{y:.4f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    fig.update_xaxes(range=[day_start, day_end], row=1, col=1)
    fig.update_xaxes(range=[day_start, day_end], row=2, col=1)

    return fig
