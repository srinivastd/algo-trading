#!/usr/bin/env python3
"""
Cancel open orders (optional) and submit market orders to flatten all non-zero positions.

Uses ib_insync directly so closing orders are not run through strategy sizing logic.

Typical usage (paper, preview only):
  cd pythonclient && PYTHONPATH=src ibkr_env_new/bin/python src/flatten_account.py --paper

Actually flatten:
  PYTHONPATH=src python src/flatten_account.py --paper --execute --global-cancel

After-hours US equities (paper diagnostics):
  ... --allow-closed-market
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ib_insync import IB, MarketOrder, Stock, util

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from ibkr.connection import IBKRConnection  # noqa: E402


def _position_qty(pos) -> float:
    return float(pos.position)


def _flatten_action(qty: float) -> str:
    return "SELL" if qty > 0 else "BUY"


async def _flatten_one(
    ib: IB,
    *,
    allow_equity_outside_rth: bool,
    dry_run: bool,
) -> None:
    positions = await ib.reqPositionsAsync()
    rows = [p for p in positions if _position_qty(p) != 0.0]
    if not rows:
        print("No open positions.")
        return

    print(f"Found {len(rows)} position(s) to flatten:")
    for p in rows:
        c = p.contract
        q = _position_qty(p)
        sym = getattr(c, "localSymbol", None) or getattr(c, "symbol", "?")
        print(f"  {sym} secType={c.secType} qty={q}")

    if dry_run:
        print("Dry run: no orders sent. Pass --execute to submit.")
        return

    for p in rows:
        c0 = p.contract
        raw_qty = _position_qty(p)
        action = _flatten_action(raw_qty)
        abs_qty = abs(raw_qty)

        if c0.secType == "CRYPTO":
            qualified = await ib.qualifyContractsAsync(c0)
            if not qualified:
                print(f"SKIP qualify failed: {c0}")
                continue
            c = qualified[0]
            order = MarketOrder(action, abs_qty, tif="IOC")
            order.outsideRth = True
        elif c0.secType == "STK":
            shares = int(abs_qty)
            if shares <= 0:
                print(f"SKIP zero int shares after truncate: {c0.symbol} raw={abs_qty}")
                continue
            # SMART avoids direct-to-primary (e.g. NASDAQ-only) routing that triggers IB 10311
            # when "Block direct-routed orders" is on in TWS precautionary settings.
            stk = Stock(c0.symbol, "SMART", "USD")
            qualified = await ib.qualifyContractsAsync(stk)
            if not qualified:
                print(f"SKIP qualify failed: {stk}")
                continue
            c = qualified[0]
            order = MarketOrder(action, shares, tif="DAY")
            if allow_equity_outside_rth:
                order.outsideRth = True
        else:
            print(f"SKIP unsupported secType={c0.secType} symbol={getattr(c0, 'symbol', c0)}")
            continue

        label = getattr(c, "localSymbol", None) or c.symbol
        print(f"Submitting MKT {action} {order.totalQuantity} {label} ...")
        ib.placeOrder(c, order)
        await asyncio.sleep(2.0)

    print("Done submitting flatten orders. Verify fills and remaining positions in TWS.")


async def _amain(args: argparse.Namespace) -> int:
    port = 4002 if args.paper else 7497
    allow_closed = bool(args.paper and args.allow_closed_market and not args.live)
    conn = IBKRConnection(
        host=args.host,
        port=port,
        client_id=args.client_id,
        allow_equity_orders_when_market_closed=allow_closed,
    )
    if not await conn.connect():
        print("Failed to connect to IBKR.", file=sys.stderr)
        return 1
    try:
        ib = conn.get_ib()
        if args.global_cancel:
            print("Requesting global cancel of all open orders...")
            ib.reqGlobalCancel()
            await asyncio.sleep(1.5)

        await _flatten_one(
            ib,
            allow_equity_outside_rth=allow_closed,
            dry_run=not args.execute,
        )
        return 0
    finally:
        await conn.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Flatten all IBKR positions (market orders).")
    parser.add_argument("--paper", action="store_true", help="Use paper port 4002 (default if neither --live)")
    parser.add_argument("--live", action="store_true", help="Use live port 7497")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--client-id", type=int, default=1)
    parser.add_argument(
        "--allow-closed-market",
        action="store_true",
        help="Paper only: set outsideRth on stock MKT orders (matches main.py --allow-closed-market)",
    )
    parser.add_argument(
        "--global-cancel",
        action="store_true",
        help="Call reqGlobalCancel() before flattening (clears working bracket/stop legs)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit orders; without this flag, only list positions (safe default)",
    )
    args = parser.parse_args()
    if not args.live and not args.paper:
        args.paper = True
    if args.allow_closed_market and not args.paper:
        print("--allow-closed-market ignored (use with --paper)", file=sys.stderr)
        args.allow_closed_market = False

    util.startLoop()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
