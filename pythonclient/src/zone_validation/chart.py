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
_MAX_ZONE_SHAPES_PER_SIDE = 35
_ZONE_LOOKBACK_DAYS = 30
_PROJECTED_FILL_STEP_MINUTES = 8


def _parse_fill_ts(raw: str | None) -> pd.Timestamp | None:
    if not raw or not str(raw).strip():
        return None
    t = pd.to_datetime(str(raw).strip(), utc=True, errors="coerce")
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(ET)


def _normalize_zone_ts(zt: Any) -> pd.Timestamp:
    ts = pd.Timestamp(zt)
    if ts.tzinfo is None:
        return ts.tz_localize(ET)
    return ts.tz_convert(ET)


def _select_plot_zones(
    zones: Sequence[Zone],
    *,
    day_start: pd.Timestamp,
    day_end: pd.Timestamp,
    price_lo: float,
    price_hi: float,
    side: str,
) -> list[tuple[pd.Timestamp, float, float]]:
    """
    Plotly shape validation is very slow with thousands of rectangles. Keep recent,
    price-relevant zones for the visual overlay and leave full counts in API metadata.
    """
    lookback_start = day_start - pd.Timedelta(days=_ZONE_LOOKBACK_DAYS)
    pad = max((price_hi - price_lo) * 0.20, price_hi * 0.03, 1.0)
    y_min = price_lo - pad
    y_max = price_hi + pad
    selected: list[tuple[pd.Timestamp, float, float]] = []
    for raw_zt, hi, lo in zones:
        zt = _normalize_zone_ts(raw_zt)
        if zt > day_end or zt < lookback_start:
            continue
        hi_f = float(hi)
        lo_f = float(lo)
        if hi_f < y_min or lo_f > y_max:
            continue
        if side == "supply" and lo_f > y_max:
            continue
        if side == "demand" and hi_f < y_min:
            continue
        selected.append((zt, hi_f, lo_f))

    if len(selected) > _MAX_ZONE_SHAPES_PER_SIDE:
        mid_price = (price_lo + price_hi) / 2.0
        selected = sorted(
            selected,
            key=lambda z: (min(abs(z[1] - mid_price), abs(z[2] - mid_price)), -z[0].timestamp()),
        )[:_MAX_ZONE_SHAPES_PER_SIDE]
    return sorted(selected, key=lambda z: z[0])


def _zone_outline_xy(
    zones: Sequence[tuple[pd.Timestamp, float, float]],
    *,
    plot_start: pd.Timestamp,
    plot_end: pd.Timestamp,
) -> tuple[list[pd.Timestamp | None], list[float | None]]:
    xs: list[pd.Timestamp | None] = []
    ys: list[float | None] = []
    for zt, hi, lo in zones:
        x0 = max(zt, plot_start)
        x1 = plot_end
        xs.extend([x0, x1, x1, x0, x0, None])
        ys.extend([hi, hi, lo, lo, hi, None])
    return xs, ys


def _nearest_zone_band(
    zones: Sequence[tuple[pd.Timestamp, float, float]],
    *,
    side: str,
    ref_price: float,
    price_range: float,
) -> dict[str, Any] | None:
    if not zones:
        return None
    enriched = []
    for zt, hi, lo in zones:
        center = (float(hi) + float(lo)) / 2.0
        enriched.append((zt, float(hi), float(lo), center))

    if side == "supply":
        directional = [z for z in enriched if z[3] >= ref_price]
    else:
        directional = [z for z in enriched if z[3] <= ref_price]
    candidates = directional or enriched
    anchor = min(candidates, key=lambda z: abs(z[3] - ref_price))
    cluster_radius = max(price_range * 0.10, ref_price * 0.002, 0.35)
    cluster = [z for z in enriched if abs(z[3] - anchor[3]) <= cluster_radius]
    if not cluster:
        cluster = [anchor]
    return {
        "start": min(z[0] for z in cluster),
        "high": max(z[1] for z in cluster),
        "low": min(z[2] for z in cluster),
        "count": len(cluster),
    }


def _float_or_none(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if pd.isna(val):
        return None
    return val


def _decision_zone_from_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    snap = trade.get("entry_snapshot") if isinstance(trade, dict) else None
    if not isinstance(snap, dict):
        return None
    dz = snap.get("decision_zone")
    if not isinstance(dz, dict):
        # Allow a flat snapshot shape if older/debug tooling writes fields directly.
        dz = snap
    z_low = _float_or_none(dz.get("decision_zone_low"))
    z_high = _float_or_none(dz.get("decision_zone_high"))
    if z_low is None or z_high is None:
        return None
    if z_low > z_high:
        z_low, z_high = z_high, z_low

    zone_type = str(dz.get("decision_zone_type") or "").strip().lower() or "zone"
    rule = str(dz.get("decision_rule") or "").strip()
    side = str(dz.get("decision_side") or trade.get("side") or "").strip().upper()
    price = _float_or_none(dz.get("decision_price"))
    distance = _float_or_none(dz.get("decision_distance_abs"))
    pct = _float_or_none(dz.get("decision_distance_pct"))
    z_time = dz.get("decision_zone_time")
    return {
        "trade_id": str(trade.get("trade_id") or ""),
        "side": side,
        "zone_type": zone_type,
        "rule": rule,
        "low": z_low,
        "high": z_high,
        "price": price,
        "distance": distance,
        "pct": pct,
        "zone_time": str(z_time) if z_time is not None else "",
    }


def _decision_zone_label(dz: dict[str, Any]) -> str:
    side = dz.get("side") or ""
    rule = str(dz.get("rule") or "").replace("_", " ")
    zone_type = dz.get("zone_type") or "zone"
    label = f"Decision {zone_type}"
    if side or rule:
        label = f"{side}: {rule}".strip(": ")
    if dz.get("distance") is not None:
        label += f"<br>distance ${float(dz['distance']):.2f}"
        if dz.get("pct") is not None:
            label += f" / {float(dz['pct']):.2f}%"
    return label


def build_zone_trade_figure(
    ohlcv: pd.DataFrame,
    session_day: str,
    supply_zones: Sequence[Zone],
    demand_zones: Sequence[Zone],
    trades: list[dict[str, Any]] | None = None,
    *,
    title: str | None = None,
    day_requested: str | None = None,
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
    plot_start = pd.Timestamp(plot_df.index.min())
    plot_end = pd.Timestamp(plot_df.index.max())

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

    price_hi = float(plot_df["high"].max()) if len(plot_df) else 0.0
    price_lo = float(plot_df["low"].min()) if len(plot_df) else 0.0
    price_range = max(price_hi - price_lo, 1.0)
    ref_price = float(plot_df["close"].iloc[-1])
    plot_supply_zones = _select_plot_zones(
        supply_zones,
        day_start=day_start,
        day_end=day_end,
        price_lo=price_lo,
        price_hi=price_hi,
        side="supply",
    )
    plot_demand_zones = _select_plot_zones(
        demand_zones,
        day_start=day_start,
        day_end=day_end,
        price_lo=price_lo,
        price_hi=price_hi,
        side="demand",
    )
    supply_band = _nearest_zone_band(
        plot_supply_zones,
        side="supply",
        ref_price=ref_price,
        price_range=price_range,
    )
    demand_band = _nearest_zone_band(
        plot_demand_zones,
        side="demand",
        ref_price=ref_price,
        price_range=price_range,
    )
    for band, label, fill, line in (
        (supply_band, "Supply zone", "rgba(120,170,255,0.22)", "rgba(120,170,255,0.95)"),
        (demand_band, "Demand zone", "rgba(120,220,170,0.20)", "rgba(120,220,170,0.95)"),
    ):
        if not band:
            continue
        fig.add_shape(
            type="rect",
            x0=plot_start,
            x1=plot_end,
            y0=band["low"],
            y1=band["high"],
            fillcolor=fill,
            line=dict(color=line, width=1.5),
            layer="below",
            row=1,
            col=1,
        )
        fig.add_annotation(
            x=plot_start + (plot_end - plot_start) / 2,
            y=(band["low"] + band["high"]) / 2,
            text=f"{label} ({band['count']})",
            showarrow=False,
            font=dict(size=11, color="#dce8ff"),
            bgcolor="rgba(0,0,0,0.35)",
            borderpad=2,
            row=1,
            col=1,
        )

    _dr = (day_requested or "").strip()[:10] if day_requested else ""
    _ds = (session_day or "").strip()[:10]
    ann = None
    if _dr and _ds and _dr != _ds:
        ann = f"Trades: {_dr} · Candles/ zones: {_ds} (IB cache lags; refresh historical_data to align)"

    fig.update_layout(
        title=dict(text=title or f"Zones + journal {session_day}", font=dict(size=14), x=0.01, xanchor="left"),
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        legend=dict(orientation="h", yanchor="bottom", y=1.03, x=0),
        height=720,
        margin=dict(l=60, r=24, t=92, b=48),
        annotations=(
            [
                dict(
                    text=ann,
                    xref="paper",
                    yref="paper",
                    x=0,
                    y=1.12,
                    showarrow=False,
                    font=dict(size=11, color="#aaa"),
                    xanchor="left",
                )
            ]
            if ann
            else []
        ),
    )
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Volume", row=2, col=1)

    decision_zones = [dz for dz in (_decision_zone_from_trade(t) for t in trades) if dz]
    for idx, dz in enumerate(decision_zones):
        zone_type = str(dz.get("zone_type") or "").lower()
        fill = "rgba(255,210,80,0.28)" if zone_type == "demand" else "rgba(255,145,80,0.28)"
        line = "rgba(255,225,110,1.0)" if zone_type == "demand" else "rgba(255,160,100,1.0)"
        fig.add_shape(
            type="rect",
            x0=plot_start,
            x1=plot_end,
            y0=dz["low"],
            y1=dz["high"],
            fillcolor=fill,
            line=dict(color=line, width=2.5),
            layer="below",
            row=1,
            col=1,
        )
        fig.add_annotation(
            x=plot_start + (plot_end - plot_start) * (0.15 + min(idx, 3) * 0.18),
            y=(float(dz["low"]) + float(dz["high"])) / 2.0,
            text=_decision_zone_label(dz),
            showarrow=False,
            align="left",
            font=dict(size=11, color="#fff7d1"),
            bgcolor="rgba(40,30,0,0.72)",
            bordercolor=line,
            borderwidth=1,
            borderpad=3,
            row=1,
            col=1,
        )

    ex, ey, etext = [], [], []
    xx, xy, xtext = [], [], []
    projected_fill_markers = 0

    def _project_fill_ts(actual_ts: pd.Timestamp) -> pd.Timestamp:
        nonlocal projected_fill_markers
        projected_fill_markers += 1
        return plot_end + pd.Timedelta(
            minutes=_PROJECTED_FILL_STEP_MINUTES * projected_fill_markers
        )

    for t in trades:
        tid = str(t.get("trade_id", ""))
        e_ts = _parse_fill_ts(t.get("entry_fill_time"))
        x_ts = _parse_fill_ts(t.get("exit_fill_time"))
        e_px = t.get("entry_fill_price")
        x_px = t.get("exit_fill_price")
        if e_ts is not None and e_px is not None:
            if day_start <= e_ts < day_end:
                ex.append(e_ts)
                ey.append(float(e_px))
                etext.append(f"entry {tid}")
            else:
                ex.append(_project_fill_ts(e_ts))
                ey.append(float(e_px))
                etext.append(f"entry {tid}<br>actual {e_ts:%Y-%m-%d %H:%M %Z}<br>shown at right edge")
        if x_ts is not None and x_px is not None:
            if day_start <= x_ts < day_end:
                xx.append(x_ts)
                xy.append(float(x_px))
                xtext.append(f"exit {tid}")
            else:
                xx.append(_project_fill_ts(x_ts))
                xy.append(float(x_px))
                xtext.append(f"exit {tid}<br>actual {x_ts:%Y-%m-%d %H:%M %Z}<br>shown at right edge")
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

    y_vals: list[float] = [price_lo, price_hi]
    y_vals.extend(float(v) for v in ey + xy)
    for dz in decision_zones:
        y_vals.extend([float(dz["low"]), float(dz["high"])])
    if y_vals:
        y0 = min(y_vals)
        y1 = max(y_vals)
        y_pad = max((y1 - y0) * 0.08, max(abs(y1), 1.0) * 0.01, 0.25)
        fig.update_yaxes(range=[y0 - y_pad, y1 + y_pad], row=1, col=1)

    # Keep the x-axis focused on the candle session. Off-session journal fills are
    # reported in metadata instead of stretching the chart across multiple days.
    pad = pd.Timedelta(minutes=30)
    ax0 = plot_start - pad
    projected_end = (
        plot_end + pd.Timedelta(minutes=_PROJECTED_FILL_STEP_MINUTES * (projected_fill_markers + 1))
        if projected_fill_markers
        else plot_end
    )
    ax1 = max(plot_end + pad, projected_end)
    fig.update_xaxes(
        range=[ax0, ax1],
        tickformat="%H:%M",
        showticklabels=True,
        ticks="outside",
        showline=True,
        linecolor="rgba(220,220,220,0.55)",
        row=1,
        col=1,
    )
    fig.update_xaxes(
        range=[ax0, ax1],
        title_text=f"Time ({session_day}, ET)",
        tickformat="%H:%M",
        showticklabels=True,
        ticks="outside",
        showline=True,
        linecolor="rgba(220,220,220,0.55)",
        row=2,
        col=1,
    )
    fig.update_layout(
        meta={
            "supply_zones_plotted": len(plot_supply_zones),
            "demand_zones_plotted": len(plot_demand_zones),
            "supply_zone_bands_plotted": 1 if supply_band else 0,
            "demand_zone_bands_plotted": 1 if demand_band else 0,
            "zone_plot_cap_per_side": _MAX_ZONE_SHAPES_PER_SIDE,
            "zone_plot_lookback_days": _ZONE_LOOKBACK_DAYS,
            "decision_zones_plotted": len(decision_zones),
            "trades_missing_decision_zone": max(len(trades) - len(decision_zones), 0),
            "projected_off_session_fill_markers": projected_fill_markers,
        }
    )

    return fig
