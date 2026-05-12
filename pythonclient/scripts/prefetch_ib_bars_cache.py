#!/usr/bin/env python3
"""
Prefetch IBKR historical bars into ``pythonclient/historical_data/`` (gitignored CSV cache).

Uses the same path scheme as ``HistoricalDataManager._get_cached_data_path``, so subsequent
``main.py`` / ``batch_backtest`` runs reuse data without `--force-refresh`.

From pythonclient/:

  PYTHONPATH=src ./py scripts/prefetch_ib_bars_cache.py QBTS --duration \"6 M\" --bar-size \"15 mins\"

Requires TWS / IB Gateway (paper 4002 by default).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytz

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ibkr.connection import IBKRConnection  # noqa: E402
from ibkr.historical_data import HistoricalDataManager  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _et_window(duration: str) -> tuple[str, str]:
    et = pytz.timezone("US/Eastern")
    end_date = datetime.now(et)
    parts = duration.strip().split()
    if len(parts) != 2:
        raise SystemExit(f"Invalid duration {duration!r}; use e.g. '6 M', '3 Y', '180 D'.")
    n = int(parts[0])
    u = parts[1].upper()
    if u == "Y":
        start_date = end_date - timedelta(days=n * 365)
    elif u == "M":
        start_date = end_date - timedelta(days=n * 30)
    elif u == "D":
        start_date = end_date - timedelta(days=n)
    else:
        raise SystemExit("Duration unit must be Y, M, or D.")
    return (
        start_date.strftime("%Y-%m-%d %H:%M:%S"),
        end_date.strftime("%Y-%m-%d %H:%M:%S"),
    )


async def _run(args: argparse.Namespace) -> int:
    cache_root = Path(args.historical_data_dir).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip().upper() for s in args.symbols if s.strip()]
    if not symbols:
        logger.error("Provide at least one symbol.")
        return 1

    start_s, end_s = _et_window(args.duration)

    cid = (
        args.client_id
        if args.client_id is not None
        else random.randint(20, 9999)
    )
    port = 7497 if args.live else 4002

    conn = IBKRConnection(
        host=args.host,
        port=port,
        client_id=cid,
        allow_equity_orders_when_market_closed=args.allow_closed_market,
        lax_paper=args.lax_paper,
    )

    dm = HistoricalDataManager(conn, data_dir=str(cache_root))

    try:
        logger.info(
            "Connecting IBKR (%s:%s client_id=%s) cache=%s symbols=%s range=%s..%s bar=%s ib_chunk=%s",
            args.host,
            port,
            cid,
            cache_root,
            ",".join(symbols),
            start_s[:10],
            end_s[:10],
            args.bar_size,
            args.ib_chunk_duration,
        )
        if not await conn.connect():
            logger.error("IBKR connection failed.")
            return 2

        out = await dm.get_historical_data_multiple(
            symbols,
            start_s,
            end_s,
            bar_size=args.bar_size,
            duration=args.ib_chunk_duration,
            force_refresh=args.force_refresh,
            max_concurrent=min(args.max_concurrent, len(symbols)),
        )
        bad = [s for s, df in out.items() if df is None or getattr(df, "empty", False)]
        if bad:
            logger.error("No usable data for: %s", ", ".join(bad))
            return 3
        for s, df in out.items():
            logger.info("OK %s rows=%s", s, len(df))
        return 0
    finally:
        try:
            await conn.disconnect()
        except Exception:
            pass


def main() -> None:
    p = argparse.ArgumentParser(description="Prefetch IBKR bars into historical_data/ cache.")
    p.add_argument("symbols", nargs="+", help="Tickers")
    p.add_argument("--duration", default="6 M", help='Window ending now, e.g. "6 M" (matches baseline research)')
    p.add_argument("--bar-size", default="15 mins")
    p.add_argument(
        "--historical-data-dir",
        default=str((_ROOT / "historical_data").resolve()),
        help="Cache directory (default: pythonclient/historical_data)",
    )
    p.add_argument(
        "--ib-chunk-duration",
        default="1 M",
        metavar="SPEC",
        help="IB pacing chunk for walk-back requests (inside range); default '1 M'",
    )
    p.add_argument("--force-refresh", action="store_true", help="Ignore existing CSV overlap")
    p.add_argument("--max-concurrent", type=int, default=2)
    p.add_argument("--client-id", type=int, default=None)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--live", action="store_true", help="Gateway live port 7497 (default: paper 4002)")
    p.add_argument(
        "--allow-closed-market",
        action="store_true",
        help="Same flag semantics as paper main (ordering outside RTH if needed)",
    )
    p.add_argument("--lax-paper", action="store_true")

    args = p.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
