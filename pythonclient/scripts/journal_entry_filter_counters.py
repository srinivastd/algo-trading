#!/usr/bin/env python3
"""
Counterfactual entry filters on **paper / watch** journal rows: how many trades would
**fail** stricter ``adx_min`` and **max zone age** (freshness) rules, using values
captured at entry in ``indicator_snapshot_json``.

**Optional** ``--compare-pnl``: among **closed** trades with entry ADX, compare
``realized_pnl`` for ADX ≥ threshold vs below (same counterfactual as stricter ``adx_min``).

Usage (from ``pythonclient/``)::

  PYTHONPATH=src ./py scripts/journal_entry_filter_counters.py --last-weeks 3 \\
    --adx-min 33 --max-zone-age-hours 6 --compare-pnl
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]


def _parse_snapshot(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        d = json.loads(raw) if isinstance(raw, str) else {}
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def _f(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _entry_adx(snap: dict[str, Any]) -> Optional[float]:
    v = _f(snap.get("adx"))
    if v is not None:
        return v
    dz = snap.get("decision_zone")
    if isinstance(dz, dict):
        return _f(dz.get("adx"))
    return None


def _entry_zone_age_hours(snap: dict[str, Any]) -> Optional[float]:
    dz = snap.get("decision_zone")
    if not isinstance(dz, dict):
        return None
    return _f(dz.get("decision_zone_age_hours"))


def _bucket_pnl_stats(pnls: list[float]) -> tuple[int, int, float, float, float]:
    """n, wins, sum_pnl, avg_pnl, median_pnl"""
    if not pnls:
        return 0, 0, 0.0, 0.0, float("nan")
    n = len(pnls)
    wins = sum(1 for x in pnls if x > 0)
    s = sum(pnls)
    med = statistics.median(pnls)
    return n, wins, s, s / n, med


def main() -> int:
    p = argparse.ArgumentParser(
        description="Count journal rows vs counterfactual ADX / zone-age (freshness) thresholds."
    )
    p.add_argument("--journal-db", type=Path, default=_ROOT / "trade_journal.db")
    p.add_argument("--from-day", default=None, help="Inclusive YYYY-MM-DD (entry_fill day)")
    p.add_argument("--to-day", default=None, help="Inclusive YYYY-MM-DD")
    p.add_argument(
        "--last-days",
        type=int,
        default=None,
        metavar="N",
        help="Use calendar window [today-(N-1), today] if --from-day/--to-day omitted",
    )
    p.add_argument(
        "--last-weeks",
        type=int,
        default=None,
        metavar="W",
        help="Same as --last-days (7 * W); do not combine with --last-days",
    )
    p.add_argument(
        "--adx-min",
        type=float,
        default=33.0,
        help="Trade passes if entry ADX >= this (default: 33)",
    )
    p.add_argument(
        "--max-zone-age-hours",
        type=float,
        default=6.0,
        help="Trade passes if decision_zone_age_hours <= this (default: 6)",
    )
    p.add_argument(
        "--status",
        choices=("closed", "open", "any"),
        default="closed",
        help="Filter trades.status (default: closed)",
    )
    p.add_argument(
        "--compare-pnl",
        action="store_true",
        help="For closed trades with entry ADX, compare realized PnL: ADX>=threshold vs ADX<threshold",
    )
    args = p.parse_args()
    db = args.journal_db.resolve()
    if not db.is_file():
        print(f"Database not found: {db}", file=sys.stderr)
        return 1

    if args.last_days is not None and args.last_weeks is not None:
        print("Use either --last-days or --last-weeks, not both.", file=sys.stderr)
        return 2
    last_n = args.last_days
    if args.last_weeks is not None:
        if args.last_weeks < 1:
            print("--last-weeks must be >= 1", file=sys.stderr)
            return 2
        last_n = args.last_weeks * 7

    if args.from_day or args.to_day:
        if not args.from_day or not args.to_day:
            print("Use both --from-day and --to-day, or use --last-days / --last-weeks.", file=sys.stderr)
            return 2
        day_lo = args.from_day[:10]
        day_hi = args.to_day[:10]
    elif last_n is not None:
        if last_n < 1:
            print("--last-days must be >= 1", file=sys.stderr)
            return 2
        today = date.today()
        start = today - timedelta(days=last_n - 1)
        day_lo = start.isoformat()
        day_hi = today.isoformat()
    else:
        print("Provide --from-day and --to-day, or --last-days / --last-weeks.", file=sys.stderr)
        return 2

    status_filter = ""
    params: list[Any] = []
    if args.status == "closed":
        status_filter = "AND lower(trim(status)) = 'closed'"
    elif args.status == "open":
        status_filter = "AND lower(trim(status)) = 'open'"

    q = f"""
        SELECT trade_id, symbol, status, entry_fill_time, realized_pnl, indicator_snapshot_json
        FROM trades
        WHERE entry_fill_time IS NOT NULL AND trim(entry_fill_time) != ''
          AND substr(trim(entry_fill_time), 1, 10) >= ?
          AND substr(trim(entry_fill_time), 1, 10) <= ?
          {status_filter}
        ORDER BY entry_fill_time
    """
    params = [day_lo, day_hi]

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(q, params))
    con.close()

    adx_thr = float(args.adx_min)
    zone_thr = float(args.max_zone_age_hours)

    n = len(rows)
    missing_adx = 0
    adx_fail = 0
    missing_zone_age = 0
    zone_fail = 0
    both_known_pass = 0
    fail_adx_only = 0
    fail_zone_only = 0
    fail_both = 0
    with_both = 0

    for r in rows:
        snap = _parse_snapshot(r["indicator_snapshot_json"])
        adx = _entry_adx(snap)
        zage = _entry_zone_age_hours(snap)

        if adx is None:
            missing_adx += 1
        elif adx < adx_thr:
            adx_fail += 1

        if zage is None:
            missing_zone_age += 1
        elif zage > zone_thr:
            zone_fail += 1

        if adx is not None and zage is not None:
            with_both += 1
            bad_adx = adx < adx_thr
            bad_zone = zage > zone_thr
            if not bad_adx and not bad_zone:
                both_known_pass += 1
            elif bad_adx and bad_zone:
                fail_both += 1
            elif bad_adx:
                fail_adx_only += 1
            else:
                fail_zone_only += 1

    print(f"Journal: {db}")
    print(f"Entry-day window: [{day_lo}, {day_hi}]  status={args.status}")
    print(f"Rules: ADX >= {adx_thr:g}   zone_age_hours <= {zone_thr:g}")
    print(f"Rows (with entry_fill_time in window): {n}")
    print()
    print("--- Field availability ---")
    print(f"  missing entry ADX:        {missing_adx}")
    print(f"  missing zone_age_hours:   {missing_zone_age}")
    print(f"  rows with BOTH ADX+zone:  {with_both}")
    print()
    print("--- If rule is ADX only (among rows with ADX) ---")
    have_adx = n - missing_adx
    print(f"  have ADX: {have_adx}   fail ADX < {adx_thr:g}: {adx_fail}   pass: {have_adx - adx_fail}")
    if have_adx:
        print(f"  pass rate: {100.0 * (have_adx - adx_fail) / have_adx:.1f}%")
    print()
    print("--- If rule is zone age only (among rows with zone_age) ---")
    have_zone = n - missing_zone_age
    pass_zone = have_zone - zone_fail
    print(f"  have zone_age: {have_zone}   fail zone_age > {zone_thr:g}: {zone_fail}   pass: {pass_zone}")
    if have_zone:
        print(f"  pass rate: {100.0 * pass_zone / have_zone:.1f}%")
    print()
    print("--- BOTH known (ADX and zone_age): joint outcomes ---")
    print(f"  pass both:        {both_known_pass}")
    print(f"  fail ADX only:    {fail_adx_only}")
    print(f"  fail zone only:   {fail_zone_only}")
    print(f"  fail both:        {fail_both}")
    if with_both:
        print(f"  pass-both rate:   {100.0 * both_known_pass / with_both:.1f}%")
    print()
    print(
        "Note: missing ADX/zone usually means no decision_zone at entry (e.g. pre-upgrade snapshot) "
        "or non-Groq strategy row."
    )

    if args.compare_pnl:
        print()
        print("--- Closed-trade PnL by entry ADX vs --adx-min (counterfactual filter) ---")
        if args.status != "closed":
            print("  (use --status closed for meaningful PnL outcomes; skipped)")
        else:
            ge_pnls: list[float] = []
            lt_pnls: list[float] = []
            miss_adx_pnls: list[float] = []
            for r in rows:
                st = (r["status"] or "").strip().lower()
                if st != "closed":
                    continue
                pnl_raw = r["realized_pnl"]
                try:
                    pnl = float(pnl_raw) if pnl_raw is not None else None
                except (TypeError, ValueError):
                    pnl = None
                if pnl is None:
                    continue
                snap = _parse_snapshot(r["indicator_snapshot_json"])
                adx = _entry_adx(snap)
                if adx is None:
                    miss_adx_pnls.append(pnl)
                elif adx >= adx_thr:
                    ge_pnls.append(pnl)
                else:
                    lt_pnls.append(pnl)

            n_ge, w_ge, sum_ge, avg_ge, med_ge = _bucket_pnl_stats(ge_pnls)
            n_lt, w_lt, sum_lt, avg_lt, med_lt = _bucket_pnl_stats(lt_pnls)
            n_m, w_m, sum_m, avg_m, med_m = _bucket_pnl_stats(miss_adx_pnls)

            def _line(label: str, n_b: int, w_b: int, sum_b: float, avg_b: float, med_b: float) -> None:
                wr = 100.0 * w_b / n_b if n_b else 0.0
                med_s = f"{med_b:.2f}" if not math.isnan(med_b) else "n/a"
                print(
                    f"  {label:18} n={n_b:4d}  win%={wr:5.1f}  sum_pnl={sum_b:10.2f}  "
                    f"avg={avg_b:8.2f}  med={med_s}"
                )

            print(f"  Threshold: ADX >= {adx_thr:g}  (keep) vs ADX < {adx_thr:g}  (filtered out)")
            _line(f"ADX>={adx_thr:g} (keep)", n_ge, w_ge, sum_ge, avg_ge, med_ge)
            _line(f"ADX<{adx_thr:g} (drop)", n_lt, w_lt, sum_lt, avg_lt, med_lt)
            _line("ADX unknown", n_m, w_m, sum_m, avg_m, med_m)
            if n_ge and n_lt:
                print()
                print(
                    f"  Δ avg PnL (keep minus drop): {avg_ge - avg_lt:+.2f} per trade  "
                    f"(positive => higher-ADX subset had better mean outcome in this window)"
                )
            if not n_ge and not n_lt:
                print("  No closed trades with realized_pnl and entry ADX in window.")
            print(
                "  Caution: selection bias, small-n noise, and regime shift; "
                "use for exploration, not proof of future edge."
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
