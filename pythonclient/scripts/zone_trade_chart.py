#!/usr/bin/env python3
"""
MVP: symbol + session calendar day -> cached OHLCV, identify_zones, Plotly chart with journal fills.

Run from repo (example):
  cd pythonclient && PYTHONPATH=src python scripts/zone_trade_chart.py \\
    --symbol NVDA --day 2026-04-28 --html /tmp/nvda_zones.html

Uses the newest matching IB cache CSV under historical_data/ (see ibkr/historical_data.py naming).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy  # noqa: E402
from trade_journal import TradeJournal  # noqa: E402
from zone_validation import load_ohlcv_csv, pick_cache_csv, build_zone_trade_figure  # noqa: E402


def _pythonclient_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main() -> int:
    root = _pythonclient_root()
    p = argparse.ArgumentParser(description="Plot OHLCV zones + journal fills for one ET session day.")
    p.add_argument("--symbol", required=True, help="Ticker, e.g. NVDA")
    p.add_argument("--day", required=True, help="Session calendar day YYYY-MM-DD (America/New_York)")
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Explicit cache CSV (overrides --data-dir lookup)",
    )
    p.add_argument(
        "--data-dir",
        type=Path,
        default=root / "historical_data",
        help="Directory with IB cache CSVs (default: pythonclient/historical_data)",
    )
    p.add_argument("--bar-size", default="15 mins", help="Bar size label matching cache filename suffix")
    p.add_argument(
        "--journal-db",
        type=Path,
        default=root / "trade_journal.db",
        help="SQLite journal (default: pythonclient/trade_journal.db)",
    )
    p.add_argument("--no-journal", action="store_true", help="Skip DB; zones and OHLCV only")
    p.add_argument(
        "--strategy-params",
        type=Path,
        default=None,
        help="Optional JSON object of GroqRecommendationsStrategy kwargs (matches a run/experiment)",
    )
    p.add_argument("--html", type=Path, default=None, help="Write interactive HTML (Plotly)")
    p.add_argument("--show", action="store_true", help="Open default browser (requires display)")
    args = p.parse_args()

    sym = args.symbol.strip().upper()
    csv_path = pick_cache_csv(args.data_dir, sym, args.bar_size, prefer_path=args.csv)
    if csv_path is None:
        print(
            f"No cache CSV for {sym} ({args.bar_size}). "
            f"Place a file like {sym}-YYYY-MM-DD-YYYY-MM-DD-{args.bar_size.replace(' ', '')}.csv "
            f"under {args.data_dir} or pass --csv.",
            file=sys.stderr,
        )
        return 1

    raw = load_ohlcv_csv(csv_path)
    if raw.empty:
        print(f"Empty CSV: {csv_path}", file=sys.stderr)
        return 1

    strat_kw: dict = {}
    if args.strategy_params and args.strategy_params.is_file():
        strat_kw = json.loads(args.strategy_params.read_text(encoding="utf-8"))
        if not isinstance(strat_kw, dict):
            print("--strategy-params must be a JSON object", file=sys.stderr)
            return 1

    strategy = GroqRecommendationsStrategy(**strat_kw)
    df_ind = strategy.calculate_indicators(raw.copy())
    supply, demand = strategy.identify_zones(df_ind)

    trades: list = []
    if not args.no_journal and args.journal_db.is_file():
        journal = TradeJournal(args.journal_db)
        trades = journal.get_trades_touching_calendar_day(args.day.strip()[:10])
        trades = [t for t in trades if (t.get("symbol") or "").strip().upper() == sym]
    elif not args.no_journal:
        print(f"Journal DB not found ({args.journal_db}); use --no-journal or fix path.", file=sys.stderr)
        return 1

    title = f"{sym} {args.day} | zones + journal | {csv_path.name}"
    fig = build_zone_trade_figure(
        df_ind,
        args.day.strip()[:10],
        supply,
        demand,
        trades,
        title=title,
    )

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        fig.write_html(str(args.html), include_plotlyjs="cdn")
        print(f"Wrote {args.html}")
    if args.show:
        fig.show()
    if not args.html and not args.show:
        out = root / "zone_validation_last.html"
        fig.write_html(str(out), include_plotlyjs="cdn")
        print(f"Wrote {out} (pass --html or --show to customize)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
