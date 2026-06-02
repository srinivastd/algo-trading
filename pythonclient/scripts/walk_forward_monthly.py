#!/usr/bin/env python3
"""
Run **groq** (or any main.py strategy) backtests per calendar month by calling
``src/main.py`` with ``--bt-start-day`` / ``--bt-end-day``.

Pick ``--duration`` large enough that the IB/CSV fetch covers every month you
iterate (typically the span from earliest month boundary to ``now``). Indicators
are computed on that full sample before slicing — strict no-leak WF would shrink
the feature window per month (not attempted here).

From pythonclient/::

  PYTHONPATH=src ./py scripts/walk_forward_monthly.py QBTS \\
    --from-month 2025-01 --to-month 2025-03 \\
    --duration \"2 Y\" --bar-size \"15 mins\" \\
    --risk-per-trade 2000 --zone-lookback-hours 28 \\
    --out runs/wf_qbts.jsonl

Pass extra ``main.py`` flags after ``--``::

  ./py scripts/walk_forward_monthly.py QBTS --from-month 2025-01 --to-month 2025-01 -- \\
    --debug
"""
from __future__ import annotations

import argparse
import calendar
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Tuple

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from batch_backtest import build_main_argv, latest_row_for_symbol, row_dict_from_chroma  # noqa: E402


def _parse_ym(s: str) -> Tuple[int, int]:
    raw = (s or "").strip()
    parts = raw.split("-")
    if len(parts) < 2:
        raise argparse.ArgumentTypeError(f"Expected YYYY-MM, got {s!r}")
    y = int(parts[0])
    m = int(parts[1])
    if not (1 <= m <= 12):
        raise argparse.ArgumentTypeError(f"Bad month in {s!r}")
    return y, m


def _month_iter(lo: Tuple[int, int], hi: Tuple[int, int]) -> Iterator[Tuple[int, int]]:
    y, mo = lo
    while (y, mo) <= hi:
        yield y, mo
        if mo == 12:
            y += 1
            mo = 1
        else:
            mo += 1


def _month_bounds(y: int, m: int) -> Tuple[str, str]:
    last = calendar.monthrange(y, m)[1]
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}"


def main(argv: List[str] | None = None) -> int:
    raw = argv if argv is not None else sys.argv[1:]
    if "--" in raw:
        split_at = raw.index("--")
        user_main_extra = raw[split_at + 1 :]
        raw = raw[:split_at]
    else:
        user_main_extra = []

    p = argparse.ArgumentParser(description="Monthly calendar slices via main.py --bt-*-day.")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--from-month", type=_parse_ym, required=True)
    p.add_argument("--to-month", type=_parse_ym, required=True)
    p.add_argument("--strategy", default="groq")
    p.add_argument("--duration", default="2 Y")
    p.add_argument("--bar-size", default="15 mins")
    p.add_argument("--risk-per-trade", type=float, default=2000.0)
    p.add_argument("--max-duration", type=float, default=12.0)
    p.add_argument("--stop-loss", type=float, default=1.1)
    p.add_argument("--target", type=float, default=4.8)
    p.add_argument("--initial-capital", type=float, default=100000.0)
    p.add_argument("--zone-lookback", type=int, default=7)
    p.add_argument("--zone-lookback-hours", type=float, default=None)
    p.add_argument("--adx-min", type=float, default=28.5)
    p.add_argument("--zone-freshness-hours", type=float, default=8.0)
    p.add_argument("--volume-breakout-mult", type=float, default=1.36)
    p.add_argument("--volume-confirm-mult", type=float, default=1.19)
    p.add_argument("--position-size-cap", type=float, default=0.95)
    p.add_argument("--long-min-rsi", type=float, default=18.0)
    p.add_argument("--min-hold-hours", type=float, default=2.0)
    p.add_argument("--client-id", type=int, default=99)
    p.add_argument("--historical-data-dir", default=str((_ROOT / "historical_data").resolve()))
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out", type=Path, default=None, help="Append JSON Lines summaries")
    p.add_argument("--chroma-dir", type=Path, default=_SRC / "chroma_db", help="Read latest row after each run")

    args = p.parse_args(raw)

    lo, hi = args.from_month, args.to_month
    if hi < lo:
        print("--to-month must be >= --from-month", file=sys.stderr)
        return 2

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)

    dbg = "--debug" in [str(x) for x in user_main_extra]

    chroma_abs = args.chroma_dir.resolve()

    for sym_u in [s.strip().upper() for s in args.symbols if s.strip()]:
        for ym in _month_iter(lo, hi):
            y, mo = ym
            d0, d1 = _month_bounds(y, mo)
            ym_s = f"{y:04d}-{mo:02d}"

            cmd = build_main_argv(
                _SRC,
                sym_u,
                args.strategy,
                args.duration,
                args.bar_size,
                args.risk_per_trade,
                args.max_duration,
                args.stop_loss,
                args.target,
                args.initial_capital,
                args.force_refresh,
                args.client_id,
                dbg,
                zone_lookback=args.zone_lookback,
                zone_lookback_hours=args.zone_lookback_hours,
                adx_min=args.adx_min,
                zone_freshness_hours=args.zone_freshness_hours,
                volume_breakout_mult=args.volume_breakout_mult,
                volume_confirm_mult=args.volume_confirm_mult,
                position_size_cap=args.position_size_cap,
                historical_data_dir=args.historical_data_dir,
                bt_start_day=d0,
                bt_end_day=d1,
            )
            cmd.extend(
                [
                    "--long-min-rsi",
                    str(args.long_min_rsi),
                    "--min-hold-hours",
                    str(args.min_hold_hours),
                ]
            )

            if dbg:
                cmd.extend(str(x) for x in user_main_extra if str(x) != "--debug")
            else:
                cmd.extend(str(x) for x in user_main_extra)

            lab = f"{sym_u} {ym_s} {d0}..{d1}"
            print("\n===", lab, "===", flush=True)
            print(cmd, flush=True)
            if args.dry_run:
                continue

            subprocess.run(cmd, cwd=str(_SRC), check=False)

            got = latest_row_for_symbol(chroma_abs, sym_u, args.strategy)
            summary: Dict[str, object] = {
                "symbol": sym_u,
                "calendar_month": ym_s,
                "bt_start_day": d0,
                "bt_end_day": d1,
                "status": "ok" if got else "no_chroma_row",
            }
            if got:
                rid, meta = got
                row = row_dict_from_chroma(rid, meta, sym_u, args.strategy, "ok", "")
                summary["return_pct"] = row.get("return_pct")
                summary["total_trades"] = row.get("total_trades")
                summary["run_id"] = rid

            if args.out:
                with args.out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(summary, default=str) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
