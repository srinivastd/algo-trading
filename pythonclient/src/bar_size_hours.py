"""Parse IB-style bar sizes (used by HistoricalDataManager) into clock hours."""

from __future__ import annotations


def parse_bar_size_to_hours(bar_size: str) -> float:
    """
    Convert IB bar size strings to hours.

    Expects whitespace-separated count + unit as used with IBKR Historical Data:
    ``15 mins``, ``4 hours``, ``1 hour``, ``30 secs``, ``1 day``.
    """
    tokens = (bar_size or "").strip().split()
    if len(tokens) < 2:
        raise ValueError(f"Unrecognized bar_size: {bar_size!r}")

    try:
        val = float(tokens[0])
    except ValueError as e:
        raise ValueError(f"Unrecognized bar_size: {bar_size!r}") from e

    u = "".join(tokens[1:]).lower().rstrip(".").rstrip("s")

    seconds_map = {"sec"}
    minute_map = {"min", "minute"}
    hour_map = {"hour", "hr", "h"}
    day_map = {"day", "d"}
    week_map = {"week", "wk", "w"}

    if u in seconds_map:
        return val / 3600.0
    if u in minute_map:
        return val / 60.0
    if u in hour_map:
        return val
    if u in day_map:
        return val * 24.0
    if u in week_map:
        return val * 168.0

    raise ValueError(f"Unrecognized bar_size unit in: {bar_size!r}")


def zone_lookback_bars_from_clock_hours(
    zone_lookback_hours: float,
    bar_size: str,
    *,
    minimum_bars: int = 2,
) -> int:
    """Map a target **clock** depth to an integer bar count for the given bar size."""
    if zone_lookback_hours <= 0:
        raise ValueError("zone_lookback_hours must be positive")
    bar_h = parse_bar_size_to_hours(bar_size)
    if bar_h <= 0:
        raise ValueError(f"Non-positive bar length in hours for {bar_size!r}")
    n = int(round(float(zone_lookback_hours) / bar_h))
    return max(minimum_bars, max(1, n))
