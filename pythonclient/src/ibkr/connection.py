"""
IBKR Connection Manager
Handles connection to IBKR Gateway/TWS and provides connection status.
Version 1.5 - Added crypto trading support with clean separation
"""
import logging
import asyncio
import datetime
import math
import pytz
from dataclasses import dataclass, field
from ib_insync import IB, MarketOrder, StopOrder, LimitOrder, Trade, Stock, Order, Crypto
from typing import Any, List, Optional, Dict
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.crypto_utils import CryptoUtils

logger = logging.getLogger(__name__)


def _serialize_ib_trade_log(trade: Any, max_entries: int = 12) -> List[Dict[str, Any]]:
    """Last N IB trade log lines for JSON / diagnostics (status, message, errorCode)."""
    out: List[Dict[str, Any]] = []
    if trade is None or not hasattr(trade, "log") or not trade.log:
        return out
    tail = trade.log[-max_entries:] if len(trade.log) > max_entries else trade.log
    for entry in tail:
        ec = getattr(entry, "errorCode", None)
        out.append(
            {
                "status": getattr(entry, "status", None),
                "message": getattr(entry, "message", None) or str(entry),
                "errorCode": int(ec) if ec is not None else None,
            }
        )
    return out


def _ib_log_error_code(logs: List[Dict[str, Any]]) -> Optional[int]:
    for row in reversed(logs):
        ec = row.get("errorCode")
        if ec is not None and ec != 0:
            return int(ec)
    return None


@dataclass
class PlaceOrderResult:
    """Structured outcome from place_order for logs, JSON, and journal messages."""

    order_id: Optional[int] = None
    ok: bool = False
    code: str = "UNKNOWN"
    message: str = ""
    ib_status: Optional[str] = None
    ib_error_code: Optional[int] = None
    ib_log: List[Dict[str, Any]] = field(default_factory=list)
    quantity_requested: Optional[int] = None
    quantity_submitted: Optional[int] = None
    sizing: Optional[Dict[str, Any]] = None
    margin_gates: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
        }
        if self.order_id is not None:
            d["order_id"] = self.order_id
        if self.ib_status is not None:
            d["ib_status"] = self.ib_status
        if self.ib_error_code is not None:
            d["ib_error_code"] = self.ib_error_code
        if self.ib_log:
            d["ib_log"] = self.ib_log
        if self.quantity_requested is not None:
            d["quantity_requested"] = self.quantity_requested
        if self.quantity_submitted is not None:
            d["quantity_submitted"] = self.quantity_submitted
        if self.sizing:
            d["sizing"] = self.sizing
        if self.margin_gates:
            d["margin_gates"] = self.margin_gates
        return d


def _account_summary_tag_float(rows: List[Any], tag: str) -> Optional[float]:
    """
    Numeric account summary field with **live US-style** currency priority.

    IB sends one row per (tag, currency). For parity with TWS Account Window (US accounts):
    **USD** first, then **BASE** (totals in account base currency), then **empty** currency
    (IB sometimes uses this for account-wide scalars). If none of those exist but there is
    **exactly one** other currency row, use it and log at DEBUG (single-currency edge case).
    If several non-USD/BASE currencies disagree, return None — we do **not** pick an
    arbitrary EUR vs GBP row (that would not match how you read risk in TWS).
    """
    matches: List[tuple[str, float]] = []
    for v in rows:
        if getattr(v, "tag", None) != tag:
            continue
        cur = getattr(v, "currency", None) or ""
        try:
            matches.append((cur, float(v.value)))
        except (TypeError, ValueError):
            continue

    if not matches:
        return None

    for pref in ("USD", "BASE", ""):
        for cur, val in matches:
            if cur == pref:
                return val

    others = [(c, val) for c, val in matches if c not in ("USD", "BASE", "")]
    if len(others) == 1:
        cur, val = others[0]
        logger.debug(
            "account summary tag=%s value from sole non-USD/BASE currency row %s (verify vs TWS if unexpected)",
            tag,
            cur,
        )
        return val
    if len(others) > 1:
        logger.debug(
            "account summary tag=%s omitted: multiple non-USD/BASE rows %s (ambiguous vs live display)",
            tag,
            [c for c, _ in others],
        )
    return None


def _account_summary_tag_str(rows: List[Any], tag: str) -> Optional[str]:
    """String account summary field; same currency priority as ``_account_summary_tag_float``."""
    matches: List[tuple[str, str]] = []
    for v in rows:
        if getattr(v, "tag", None) != tag:
            continue
        cur = getattr(v, "currency", None) or ""
        val = getattr(v, "value", None)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            matches.append((cur, s))

    if not matches:
        return None

    for pref in ("USD", "BASE", ""):
        for cur, s in matches:
            if cur == pref:
                return s

    others = [(c, s) for c, s in matches if c not in ("USD", "BASE", "")]
    if len(others) == 1:
        cur, s = others[0]
        logger.debug(
            "account summary tag=%s string from sole non-USD/BASE currency row %s",
            tag,
            cur,
        )
        return s
    if len(others) > 1:
        logger.debug(
            "account summary tag=%s string omitted: multiple non-USD/BASE rows %s",
            tag,
            [c for c, _ in others],
        )
    return None


def _ib_pnl_float_value(x: Any) -> Optional[float]:
    """IB ``PnL`` fields use nan until the first gateway tick."""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _portfolio_unrealized_total(ib: IB, account: str) -> Optional[float]:
    """Sum ``PortfolioItem.unrealizedPNL`` when account-level ``reqPnL`` is still NaN."""
    try:
        items = ib.portfolio(account)
    except Exception:
        return None
    if not items:
        return None
    total = 0.0
    any_ok = False
    for it in items:
        v = _ib_pnl_float_value(getattr(it, "unrealizedPNL", None))
        if v is not None:
            any_ok = True
            total += v
    return total if any_ok else None


def _affordable_max_shares(
    ref_price: float,
    headroom: Dict[str, Optional[float]],
    *,
    utilization: float = 0.88,
    reg_t_margin_fraction: float = 0.52,
) -> Optional[int]:
    """
    Conservative whole-share cap from IB summary tags. Uses the minimum of
    independent caps so we stay under buying power, cash, and rough Reg-T margin.
    Returns None if no tags available (caller should not clamp).
    """
    if ref_price <= 0:
        return None
    caps: List[int] = []
    bp = headroom.get("buying_power")
    if bp is not None and bp > 0:
        caps.append(int((bp * utilization) / ref_price))
    ex = headroom.get("excess_liquidity")
    if ex is not None and ex > 0 and reg_t_margin_fraction > 0:
        caps.append(int((ex * utilization) / (reg_t_margin_fraction * ref_price)))
    faf = headroom.get("full_available_funds")
    if faf is not None and faf > 0:
        caps.append(int((faf * utilization) / ref_price))
    av = headroom.get("available_funds")
    if av is not None and av > 0:
        caps.append(int((av * utilization) / ref_price))
    if not caps:
        return None
    return max(min(caps), 0)


def place_order_failure_from_trade(trade: Any, code: str, message: str) -> PlaceOrderResult:
    logs = _serialize_ib_trade_log(trade)
    oid: Optional[int] = None
    st: Optional[str] = None
    if trade is not None and getattr(trade, "order", None):
        oid = trade.order.orderId
    if trade is not None and hasattr(trade, "orderStatus") and trade.orderStatus:
        st = trade.orderStatus.status
    ec = _ib_log_error_code(logs)
    return PlaceOrderResult(
        order_id=oid,
        ok=False,
        code=code,
        message=message,
        ib_status=st,
        ib_error_code=ec,
        ib_log=logs,
    )


class IBKRConnection:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 0,
        allow_equity_orders_when_market_closed: bool = False,
        lax_paper: bool = False,
    ):
        """
        Initialize IBKR connection with host, port, and client ID.
        
        Args:
            host: Host address (default: "127.0.0.1")
            port: Port number (4002 for paper, 7497 for live, default: 4002)
            client_id: Client ID for this connection (default: 0)
            allow_equity_orders_when_market_closed: If True, skip the local US-hours gate
                for stocks and set ``outsideRth`` on equity orders (for paper diagnostics).
        """
        self.ib = IB()
        self.host = host
        self.port = port
        self.client_id = client_id
        self._allow_equity_orders_when_market_closed = allow_equity_orders_when_market_closed
        self._lax_paper = bool(lax_paper)
        self._connected = False
        self.client = self.ib  # For compatibility with main.py

    async def connect(self, max_retries: int = 3, retry_delay: int = 2) -> bool:
        """
        Connect to IBKR Gateway/TWS with retry logic.
        
        Args:
            max_retries: Maximum number of connection attempts (default: 3)
            retry_delay: Delay between retries in seconds (default: 2)
            
        Returns:
            bool: True if connection successful, False otherwise
        """
        # Print trading mode banner based on port
        logger.info("="*80)
        if self.port == 4002:
            logger.info("PAPER TRADING MODE")
            logger.info("This is a simulation - no real trades will be executed")
            logger.info(f"Port: {self.port} (Paper Trading)")
        else:
            logger.warning("LIVE TRADING MODE")
            logger.warning("WARNING: This will execute real trades with real money")
            logger.warning(f"Port: {self.port} (Live Trading)")
            try:
                response = input("\nAre you sure you want to proceed with LIVE TRADING? (yes/no): ").lower()
                if response != 'yes':
                    logger.info("Live trading cancelled by user")
                    return False
            except KeyboardInterrupt:
                logger.info("\nLive trading cancelled by user")
                return False
        logger.info("="*80)

        for attempt in range(max_retries):
            try:
                logger.info(f"Connection attempt {attempt + 1}/{max_retries}")
                logger.info(f"Connecting to {self.host}:{self.port} with client ID {self.client_id}")
                
                # Attempt to connect
                await self.ib.connectAsync(self.host, self.port, clientId=self.client_id, timeout=10)
                
                if self.ib.isConnected():
                    self._connected = True
                    logger.info("Successfully connected to IBKR Gateway")
                    
                    # Get and log account information
                    accounts = self.ib.managedAccounts()
                    if accounts:
                        logger.info(f"Connected to account: {accounts[0]}")
                        try:
                            account_summary = await self.ib.reqAccountSummaryAsync()
                            logger.info("\nAccount Summary:")
                            for summary in account_summary:
                                logger.info(f"{summary.tag}: {summary.value}")
                        except Exception as e:
                            logger.warning(f"Could not fetch account summary: {str(e)}")
                    
                    return True
                else:
                    logger.error("Connection failed - not connected after connectAsync")
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                    continue
                
            except ConnectionRefusedError:
                logger.error(f"Connection refused at {self.host}:{self.port}. Please ensure IBKR Gateway is running.")
                if self.port == 4002:
                    logger.info("For paper trading: Start IBKR Gateway and enable paper trading")
                    logger.info("1. Open IBKR Gateway")
                    logger.info("2. Log in to your paper trading account")
                    logger.info("3. Ensure port 4002 is available")
                else:
                    logger.info("For live trading: Start IBKR Gateway and log in to your account")
                    logger.info("1. Open IBKR Gateway")
                    logger.info("2. Log in to your live trading account")
                    logger.info("3. Ensure port 4001 or 7497 is available")
                return False
            except Exception as e:
                logger.error(f"Connection attempt {attempt + 1} failed: {str(e)}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.error("All connection attempts failed")
                    return False
                    
        return False

    async def disconnect(self) -> None:
        """Disconnect from IBKR Gateway/TWS."""
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
                self._connected = False
                logger.info("Disconnected from IBKR Gateway")
        except Exception as e:
            logger.error(f"Error during disconnect: {str(e)}")

    def is_connected(self) -> bool:
        """Check if connected to IBKR Gateway/TWS."""
        return self._connected and self.ib.isConnected()

    def get_ib(self) -> IB:
        """Get the IB instance."""
        return self.ib

    async def get_accounts(self) -> List[str]:
        """Get list of available accounts."""
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")
            return self.ib.managedAccounts()
        except Exception as e:
            logger.error(f"Error getting accounts: {str(e)}")
            return []

    async def get_next_order_id(self) -> Optional[int]:
        """Get the next valid order ID."""
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")
            return self.ib.client.getReqId()
        except Exception as e:
            logger.error(f"Error getting next order ID: {str(e)}")
            return None

    async def validate_symbol(self, symbol: str) -> bool:
        """Validate that a symbol is tradeable by checking market data availability"""
        try:
            if not self.is_connected():
                logger.warning(f"Cannot validate {symbol} - not connected to IBKR")
                return False
                
            # Create and qualify contract
            contract = CryptoUtils.create_contract(symbol)
            qualified_contracts = await self.get_ib().qualifyContractsAsync(contract)
            
            if not qualified_contracts:
                logger.error(f"Symbol {symbol} could not be qualified - may be invalid or delisted")
                return False
                
            contract = qualified_contracts[0]
            
            # Validate the contract
            if not CryptoUtils.validate_contract(contract, symbol):
                return False
                
            # For equities, if we can qualify the contract, it's likely tradeable
            # Market data might not be available during off-hours but the symbol is still valid
            if not CryptoUtils.is_crypto(symbol):
                logger.info(f"Symbol {symbol} validated via contract qualification (Exchange: {getattr(contract, 'primaryExchange', 'Unknown')})")
                return True
                
            # For crypto, try to get market data since it should be available 24/7
            try:
                ticker = self.get_ib().reqMktData(contract, '', True, False)
                await asyncio.sleep(2)  # Wait for data
                
                # Check if we got valid market data
                if hasattr(ticker, 'marketPrice') and ticker.marketPrice() > 0:
                    logger.info(f"Crypto symbol {symbol} validated - current price: ${ticker.marketPrice():.2f}")
                    return True
                elif hasattr(ticker, 'bid') and ticker.bid > 0:
                    logger.info(f"Crypto symbol {symbol} validated - current bid: ${ticker.bid:.2f}")
                    return True
                elif hasattr(ticker, 'last') and ticker.last > 0:
                    logger.info(f"Crypto symbol {symbol} validated - last price: ${ticker.last:.2f}")
                    return True
                else:
                    logger.warning(f"Crypto symbol {symbol} qualified but no valid market data received")
                    # Still return True for crypto if contract qualified
                    return True
                    
            except Exception as data_error:
                logger.warning(f"Could not get market data for crypto {symbol}: {data_error}")
                # Still return True if contract qualified
                return True
            finally:
                # Clean up market data subscription
                try:
                    self.get_ib().cancelMktData(contract)
                except:
                    pass
                    
        except Exception as e:
            logger.error(f"Error validating symbol {symbol}: {e}")
            return False

    async def is_market_open(self, symbol: str) -> bool:
        """Check if the market is open for the given symbol"""
        try:
            if not self.is_connected():
                return False
                
            # For crypto, market is always open (24/7)
            if CryptoUtils.is_crypto(symbol):
                return True
                
            # For stocks, check if it's during regular trading hours
            # This is a simple check - IBKR will ultimately validate
            
            # Get current time in Eastern timezone (US market timezone)
            eastern = pytz.timezone('US/Eastern')
            now = datetime.datetime.now(eastern)
            current_time = now.time()
            
            # Check if it's a weekday
            if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
                logger.warning(f"Market is closed (weekend) for {symbol}")
                return False
                
            # Check if it's during regular trading hours (9:30 AM - 4:00 PM ET)
            market_open = datetime.time(9, 30)
            market_close = datetime.time(16, 0)
            
            if current_time < market_open or current_time > market_close:
                logger.warning(f"Market is closed (outside trading hours) for {symbol}. Current time: {current_time}")
                return False
                
            return True
            
        except Exception as e:
            logger.warning(f"Could not determine market status for {symbol}: {e}")
            return True  # Default to allowing the order attempt

    async def get_account_sizing_headroom(self) -> Dict[str, Optional[float]]:
        """Pull margin/cash tags from IB account summary (USD/BASE) for order sizing."""
        empty: Dict[str, Optional[float]] = {
            "buying_power": None,
            "excess_liquidity": None,
            "full_available_funds": None,
            "available_funds": None,
            "net_liquidation": None,
            "equity_with_loan": None,
        }
        if not self.is_connected():
            return empty
        try:
            ib = self.get_ib()
            accounts = ib.managedAccounts() or []
            acct = accounts[0] if accounts else ""
            rows = await ib.accountSummaryAsync(acct)
        except Exception as e:
            logger.debug("get_account_sizing_headroom failed: %s", e)
            return empty
        return {
            "buying_power": _account_summary_tag_float(rows, "BuyingPower"),
            "excess_liquidity": _account_summary_tag_float(rows, "ExcessLiquidity"),
            "full_available_funds": _account_summary_tag_float(rows, "FullAvailableFunds"),
            "available_funds": _account_summary_tag_float(rows, "AvailableFunds"),
            "net_liquidation": _account_summary_tag_float(rows, "NetLiquidation"),
            "equity_with_loan": _account_summary_tag_float(rows, "EquityWithLoanValue"),
        }

    async def get_account_margin_gate_snapshot(self) -> Dict[str, Any]:
        """
        IB account summary fields that mirror live margin/cash/PDT gates (paper or live).

        Use alongside ``get_account_sizing_headroom`` / ``place_order`` rejections (e.g.
        INSUFFICIENT_BUYING_POWER). DayTradesRemaining: -1 means unlimited day trades per IB.
        """
        empty: Dict[str, Any] = {
            "account_type": None,
            "net_liquidation": None,
            "total_cash_value": None,
            "settled_cash": None,
            "buying_power": None,
            "excess_liquidity": None,
            "available_funds": None,
            "full_available_funds": None,
            "day_trades_remaining": None,
            "cushion": None,
            "highest_severity": None,
        }
        if not self.is_connected():
            return empty
        try:
            ib = self.get_ib()
            accounts = ib.managedAccounts() or []
            acct = accounts[0] if accounts else ""
            rows = await ib.accountSummaryAsync(acct)
        except Exception as e:
            logger.debug("get_account_margin_gate_snapshot failed: %s", e)
            return empty
        dtr = _account_summary_tag_float(rows, "DayTradesRemaining")
        return {
            "account_type": _account_summary_tag_str(rows, "AccountType"),
            "net_liquidation": _account_summary_tag_float(rows, "NetLiquidation"),
            "total_cash_value": _account_summary_tag_float(rows, "TotalCashValue"),
            "settled_cash": _account_summary_tag_float(rows, "SettledCash"),
            "buying_power": _account_summary_tag_float(rows, "BuyingPower"),
            "excess_liquidity": _account_summary_tag_float(rows, "ExcessLiquidity"),
            "available_funds": _account_summary_tag_float(rows, "AvailableFunds"),
            "full_available_funds": _account_summary_tag_float(rows, "FullAvailableFunds"),
            "day_trades_remaining": dtr,
            "cushion": _account_summary_tag_float(rows, "Cushion"),
            "highest_severity": _account_summary_tag_str(rows, "HighestSeverity"),
        }

    async def get_account_pnl_ib_snapshot(self) -> Dict[str, Optional[float]]:
        """
        Account-level P&L from IB ``reqPnL`` (TWS/Gateway P&L stream).

        ``daily_pnl`` / ``unrealized_pnl`` / ``realized_pnl`` match IB's definitions for the
        subscription (realized is the closed-leg component IB attributes to the session).
        Use for journal ``daily_snapshots`` / dashboards; per-trade realized still comes from
        executions if you need fill-level truth.

        **Important:** A ``reqPnL`` subscription often exists with all fields still ``nan`` until
        the first ticks arrive. We **always** wait for non-nan values (short bounded poll). If
        account-level unrealized is still missing, we fall back to summing ``ib.portfolio()``
        ``unrealizedPNL`` (same numbers TWS uses for open positions when streaming catches up).
        """
        empty: Dict[str, Optional[float]] = {
            "daily_pnl": None,
            "unrealized_pnl": None,
            "realized_pnl": None,
        }
        if not self.is_connected():
            return empty
        try:
            ib = self.get_ib()
            accounts = ib.managedAccounts() or []
            acct = accounts[0] if accounts else ""
            if not acct:
                return empty
            if not ib.pnl(acct, ""):
                ib.reqPnL(acct, "")
            # Always wait: if a subscription already existed, the old code skipped this and
            # persisted SQL NULL from nan forever.
            for _ in range(60):
                await asyncio.sleep(0.1)
                rows = ib.pnl(acct, "")
                if not rows:
                    break
                p0 = rows[0]
                if (
                    _ib_pnl_float_value(p0.realizedPnL) is not None
                    or _ib_pnl_float_value(p0.unrealizedPnL) is not None
                    or _ib_pnl_float_value(p0.dailyPnL) is not None
                ):
                    break
            rows = ib.pnl(acct, "")
            if not rows:
                return empty
            p = rows[0]
            daily = _ib_pnl_float_value(p.dailyPnL)
            unreal = _ib_pnl_float_value(p.unrealizedPnL)
            real = _ib_pnl_float_value(p.realizedPnL)
            if unreal is None:
                unreal = _portfolio_unrealized_total(ib, acct)
            return {
                "daily_pnl": daily,
                "unrealized_pnl": unreal,
                "realized_pnl": real,
            }
        except Exception as e:
            logger.debug("get_account_pnl_ib_snapshot failed: %s", e)
            return empty

    def cancel_account_pnl_subscription(self) -> None:
        """Release IB P&L stream (call when tearing down a watch session)."""
        try:
            ib = self.get_ib()
            accounts = ib.managedAccounts() or []
            if not accounts:
                return
            acct = accounts[0]
            if ib.pnl(acct, ""):
                ib.cancelPnL(acct, "")
        except Exception as e:
            logger.debug("cancel_account_pnl_subscription: %s", e)

    async def get_current_positions(self) -> List[Dict[str, Any]]:
        """
        Return current open positions from IBKR portfolio (matches logs and TWS).

        Positive position = LONG, negative = SHORT. Used by dashboard live panel and
        position sync to journal. Returns only non-zero positions.
        """
        if not self.is_connected():
            logger.debug("get_current_positions: not connected")
            return []

        try:
            ib = self.get_ib()
            positions = await ib.reqPositionsAsync()
            result: List[Dict[str, Any]] = []
            for p in positions:
                pos = float(getattr(p, "position", 0.0))
                if abs(pos) < 0.001:
                    continue
                contract = getattr(p, "contract", None)
                symbol = getattr(contract, "symbol", "UNKNOWN").upper() if contract else "UNKNOWN"
                result.append({
                    "symbol": symbol,
                    "secType": getattr(contract, "secType", "STK") if contract else "STK",
                    "position": pos,  # positive=long, negative=short
                    "avgCost": float(getattr(p, "avgCost", 0.0)),
                    "unrealizedPNL": float(getattr(p, "unrealizedPNL", 0.0)) if hasattr(p, "unrealizedPNL") else None,
                    "marketValue": float(getattr(p, "marketValue", 0.0)) if hasattr(p, "marketValue") else None,
                    "marketPrice": getattr(p, "marketPrice", None),
                })
            if result:
                logger.info(f"Retrieved {len(result)} current IBKR positions: {[p['symbol'] for p in result]}")
            return result
        except Exception as e:
            logger.warning(f"get_current_positions failed: {e}")
            return []

    async def _sell_only_closes_or_reduces_long(self, symbol: str, quantity: int) -> bool:
        """
        True when SELL size is fully covered by an existing **long** stock position (close/trim only).
        False for opening a short, adding to a short, or when flat (SELL would open short) — those
        should still go through the rough BP share cap like BUY.
        """
        qty = max(0, int(quantity))
        if qty <= 0:
            return True
        sym_u = symbol.upper()
        try:
            ib = self.get_ib()
            positions = await ib.reqPositionsAsync()
            for p in positions:
                if p.contract.symbol.upper() != sym_u:
                    continue
                if getattr(p.contract, "secType", "") not in ("STK", ""):
                    continue
                pos = float(p.position)
                if pos > 1e-6:
                    return qty <= pos + 1e-3
                return False
        except Exception as e:
            logger.debug("_sell_only_closes_or_reduces_long failed: %s", e)
        return False

    async def pre_trade_check(
        self,
        symbol: str,
        action: str = "BUY",
        quantity: int = 100,
        ref_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Comprehensive pre-trade gate that makes **paper behave like live**.

        - Uses ``get_account_margin_gate_snapshot()`` (DayTradesRemaining, SettledCash, BuyingPower...).
        - **PDT**: blocks if DayTradesRemaining==0.
        - **Settlement**: checks SettledCash for CASH accounts (uses ``ref_price`` when provided).
        - **Buying power**: enforced in ``place_order`` via live ``ref_price`` and
          ``_affordable_max_shares`` (clip/skip), not here.
        - **--lax-paper** (paper only) bypasses all gates for diagnostics.

        Returns dict with "allowed", "reason", full gates, etc.
        """
        gates = await self.get_account_margin_gate_snapshot()
        result = {
            "allowed": True,
            "reason": "OK",
            "margin_gates": gates,
            "day_trades_remaining": gates.get("day_trades_remaining"),
            "settled_cash": gates.get("settled_cash"),
            "buying_power": gates.get("buying_power"),
            "suggested_max_shares": None,
            "lax_paper": self._lax_paper,
        }

        if not self.is_connected():
            result.update({"allowed": False, "reason": "NOT_CONNECTED"})
            return result

        if self._lax_paper:
            logger.warning(
                "LAX_PAPER_MODE enabled — skipping PDT, settlement, and buying-power gates. "
                "Use ONLY for diagnostics. Live trading will be strict."
            )
            return result

        dtr = gates.get("day_trades_remaining")
        if dtr is not None:
            try:
                dtr_float = float(dtr)
                if dtr_float == 0.0:
                    result.update({
                        "allowed": False,
                        "reason": "PDT_LIMIT_EXCEEDED (DayTradesRemaining=0)",
                    })
                    logger.warning(
                        "PRE_TRADE_REJECTED symbol=%s action=%s reason=PDT_LIMIT_EXCEEDED "
                        "DayTradesRemaining=0 — paper/live will be blocked until next session rollover",
                        symbol.upper(), action.upper()
                    )
                    return result
                elif dtr_float > 0 and dtr_float < 4:
                    logger.info(
                        "PRE_TRADE_WARNING symbol=%s action=%s DayTradesRemaining=%.0f (close to PDT limit)",
                        symbol.upper(), action.upper(), dtr_float
                    )
            except (TypeError, ValueError):
                pass

        # Settlement check for cash accounts
        acct_type = gates.get("account_type", "")
        settled = gates.get("settled_cash")
        est_px = float(ref_price) if ref_price is not None and ref_price > 0 else 150.0
        if "CASH" in str(acct_type).upper() and settled is not None:
            est_cost = quantity * est_px
            if settled < est_cost * 1.1:  # 10% buffer
                result.update({
                    "allowed": False,
                    "reason": f"INSUFFICIENT_SETTLED_CASH (settled=${settled:,.0f}, need~${est_cost*1.1:,.0f})",
                })
                logger.warning(
                    "PRE_TRADE_REJECTED symbol=%s action=%s reason=INSUFFICIENT_SETTLED_CASH "
                    "settled_cash=%.2f est_cost=%.2f est_px=%.2f account_type=%s",
                    symbol.upper(), action.upper(), settled, est_cost, est_px, acct_type
                )
                return result

        # Buying-power sizing is applied in ``place_order`` using ``ref_price`` +
        # ``_affordable_max_shares`` (clip or skip). Do not reject here with a placeholder
        # price — that caused false INSUFFICIENT_BUYING_POWER before the live snapshot.

        bp: Optional[float] = None
        au = action.upper()
        apply_bp_share_cap = True
        if au == "SELL" and await self._sell_only_closes_or_reduces_long(symbol, quantity):
            apply_bp_share_cap = False
        if apply_bp_share_cap and ref_price is not None and ref_price > 0:
            headroom = await self.get_account_sizing_headroom()
            bp = headroom.get("buying_power") or headroom.get("available_funds")
            aff = _affordable_max_shares(float(ref_price), headroom)
            if aff is not None:
                result["suggested_max_shares"] = aff

        logger.debug(
            "PRE_TRADE_ALLOWED symbol=%s action=%s dtr=%s bp=%.0f settled=%.0f acct=%s",
            symbol.upper(), action.upper(),
            result["day_trades_remaining"], bp or 0, settled or 0, acct_type or "?"
        )
        return result

    async def place_order(self, symbol: str, action: str, quantity: int, order_type: str,
                         limit_price: float = None, aux_price: float = None,
                         parent_id: int = None) -> PlaceOrderResult:
        """Place a trade order. Inspect ``result.as_dict()`` for IB log lines and error codes."""
        try:
            qty_requested = max(0, int(quantity))
            if not self.is_connected():
                res = PlaceOrderResult(
                    ok=False,
                    code="NOT_CONNECTED",
                    message="Not connected to IBKR",
                )
                logger.error(
                    "PLACE_ORDER_FAILED symbol=%s code=%s message=%s",
                    symbol.upper(),
                    res.code,
                    res.message,
                )
                return res

            logger.debug(f"Validating symbol {symbol} before placing order...")

            if not await self.validate_symbol(symbol):
                res = PlaceOrderResult(
                    ok=False,
                    code="SYMBOL_VALIDATION_FAILED",
                    message=f"Symbol validation failed for {symbol}",
                )
                logger.error(
                    "PLACE_ORDER_FAILED symbol=%s code=%s message=%s",
                    symbol.upper(),
                    res.code,
                    res.message,
                )
                return res

            market_open = await self.is_market_open(symbol)
            # Crypto is always "open" in is_market_open; equities respect US session unless bypassed.
            if (
                not market_open
                and not CryptoUtils.is_crypto(symbol)
                and not self._allow_equity_orders_when_market_closed
            ):
                requested_price = limit_price if order_type == 'LMT' else aux_price if order_type == 'STP' else None
                logger.info(
                    "ORDER_ATTEMPT symbol=%s action=%s qty=%s type=%s market_open=%s ref=%s "
                    "requested=%s final=%s tick=%s parent=%s status=SKIPPED_MARKET_CLOSED",
                    symbol.upper(),
                    action.upper(),
                    qty_requested,
                    order_type,
                    market_open,
                    None,
                    f"{requested_price:.6f}" if requested_price is not None else "MKT",
                    None,
                    None,
                    parent_id if parent_id is not None else "-",
                )
                logger.info(f"Skipping order for {symbol}: market is closed")
                res = PlaceOrderResult(
                    ok=False,
                    code="MARKET_CLOSED",
                    message="Market closed; order not submitted",
                )
                logger.error(
                    "PLACE_ORDER_FAILED symbol=%s code=%s message=%s",
                    symbol.upper(),
                    res.code,
                    res.message,
                )
                return res

            contract = CryptoUtils.create_contract(symbol)

            qualified_contracts = await self.get_ib().qualifyContractsAsync(contract)
            if not qualified_contracts:
                res = PlaceOrderResult(
                    ok=False,
                    code="CONTRACT_QUALIFY_FAILED",
                    message=f"Could not qualify contract for {symbol}",
                )
                logger.error(
                    "PLACE_ORDER_FAILED symbol=%s code=%s message=%s",
                    symbol.upper(),
                    res.code,
                    res.message,
                )
                return res

            contract = qualified_contracts[0]
            logger.debug(f"Contract qualified for {symbol}: {contract}")

            if not CryptoUtils.validate_contract(contract, symbol):
                res = PlaceOrderResult(
                    ok=False,
                    code="CONTRACT_INVALID",
                    message=f"Contract validation failed for {symbol}",
                )
                logger.error(
                    "PLACE_ORDER_FAILED symbol=%s code=%s message=%s",
                    symbol.upper(),
                    res.code,
                    res.message,
                )
                return res

            is_crypto = CryptoUtils.is_crypto(symbol)
            equity_outside_rth = (
                self._allow_equity_orders_when_market_closed
                and not is_crypto
                and not market_open
            )
            if equity_outside_rth:
                logger.info(
                    "ORDER_OUTSIDE_LOCAL_SESSION symbol=%s action=%s (allow_equity_orders_when_market_closed; "
                    "outsideRth=True for equity)",
                    symbol.upper(),
                    action.upper(),
                )

            market_price = await self._get_market_reference_price(contract, symbol)
            min_tick = await self._get_min_tick(contract)

            requested_price = limit_price if order_type == 'LMT' else aux_price if order_type == 'STP' else None
            if order_type == 'LMT' and limit_price is not None:
                if market_price and market_price > 0:
                    limit_price = self._anchor_limit_price_near_market(
                        requested_price=limit_price,
                        market_price=market_price,
                        action=action,
                        max_distance_bps=35,
                    )
                if min_tick:
                    limit_price = self._round_to_tick(limit_price, min_tick, action)

            if order_type == 'STP' and aux_price is not None and min_tick:
                aux_price = self._round_to_tick(aux_price, min_tick, action)

            final_price = limit_price if order_type == 'LMT' else aux_price if order_type == 'STP' else None

            ref_px: Optional[float] = None
            if final_price is not None and final_price > 0:
                ref_px = float(final_price)
            elif market_price is not None and market_price > 0:
                ref_px = float(market_price)

            # PDT / settlement (with live ref when available); BP clip happens below.
            precheck = await self.pre_trade_check(symbol, action, qty_requested, ref_price=ref_px)
            if not precheck.get("allowed", False):
                res = PlaceOrderResult(
                    ok=False,
                    code=precheck.get("reason", "PRE_TRADE_REJECTED").split()[0],
                    message=precheck.get("reason", "Pre-trade gate failed"),
                    margin_gates=precheck.get("margin_gates"),
                    quantity_requested=qty_requested,
                    quantity_submitted=0,
                )
                logger.error(
                    "PRE_TRADE_REJECTED symbol=%s action=%s qty=%s reason=%s gates=%s",
                    symbol.upper(),
                    action.upper(),
                    qty_requested,
                    precheck.get("reason"),
                    {k: v for k, v in (precheck.get("margin_gates") or {}).items()
                     if k in ("day_trades_remaining", "settled_cash", "buying_power")},
                )
                return res

            qty = qty_requested
            sizing_meta: Optional[Dict[str, Any]] = None
            # Size only standalone equity orders; bracket legs must match parent quantity.
            # Skip BP-based clamp for SELL that only closes/trims a long (exit liquidity).
            apply_bp_clip = not is_crypto and parent_id is None and qty > 0
            if apply_bp_clip and action.upper() == "SELL" and await self._sell_only_closes_or_reduces_long(
                symbol, qty
            ):
                apply_bp_clip = False
            if apply_bp_clip and ref_px is not None and ref_px > 0:
                headroom = await self.get_account_sizing_headroom()
                max_q = _affordable_max_shares(ref_px, headroom)
                if max_q is not None:
                    if max_q == 0:
                        res = PlaceOrderResult(
                            ok=False,
                            code="SKIPPED_INSUFFICIENT_BP",
                            message="No affordable size at reference price; order not submitted",
                            quantity_requested=qty_requested,
                            quantity_submitted=0,
                            sizing={
                                "ref_price": ref_px,
                                "headroom": {k: v for k, v in headroom.items() if v is not None},
                            },
                        )
                        logger.warning(
                            "PLACE_ORDER_SKIPPED symbol=%s code=%s ref_px=%s headroom=%s",
                            symbol.upper(),
                            res.code,
                            ref_px,
                            res.sizing.get("headroom") if res.sizing else {},
                        )
                        return res
                    if max_q < qty:
                        logger.warning(
                            "ORDER_SIZE_CLAMPED symbol=%s action=%s requested=%s capped=%s ref_px=%.4f",
                            symbol.upper(),
                            action.upper(),
                            qty,
                            max_q,
                            ref_px,
                        )
                        sizing_meta = {
                            "requested": qty_requested,
                            "capped_to": max_q,
                            "ref_price": ref_px,
                            "headroom": {k: v for k, v in headroom.items() if v is not None},
                        }
                        qty = max_q

            logger.info(
                "ORDER_ATTEMPT symbol=%s action=%s qty=%s type=%s market_open=%s ref=%s "
                "requested=%s final=%s tick=%s parent=%s status=SUBMITTING",
                symbol.upper(),
                action.upper(),
                qty,
                order_type,
                market_open,
                f"{market_price:.6f}" if market_price is not None else "NA",
                f"{requested_price:.6f}" if requested_price is not None else "MKT",
                f"{final_price:.6f}" if final_price is not None else "MKT",
                f"{min_tick:.6f}" if min_tick is not None else "NA",
                parent_id if parent_id is not None else "-",
            )

            tif = 'IOC' if is_crypto else 'GTC'

            if order_type == 'LMT':
                order = LimitOrder(action, qty, limit_price, tif=tif, transmit=True)
                if is_crypto or equity_outside_rth:
                    order.outsideRth = True
            elif order_type == 'STP':
                order = StopOrder(action, qty, aux_price, tif=tif, transmit=True)
                if is_crypto or equity_outside_rth:
                    order.outsideRth = True
                if parent_id:
                    order.parentId = parent_id
            else:
                order = MarketOrder(action, qty, tif=tif, transmit=True)
                if is_crypto or equity_outside_rth:
                    order.outsideRth = True

            trade = self.get_ib().placeOrder(contract, order)
            await asyncio.sleep(3)

            if hasattr(trade, 'orderStatus') and trade.orderStatus:
                status = trade.orderStatus.status
                if status in ['Filled', 'Submitted', 'PendingSubmit', 'PreSubmitted']:
                    order_id = trade.order.orderId
                    asset_type = "CRYPTO" if is_crypto else "EQUITY"
                    price_str = f"{limit_price or aux_price or 'market'}"
                    logger.info(
                        f"Placed {asset_type} order {order_id}: {action} {qty} shares of {symbol} at {price_str} (TIF: {tif})"
                    )
                    return PlaceOrderResult(
                        order_id=order_id,
                        ok=True,
                        code="OK",
                        message=f"{asset_type} order accepted ({status})",
                        ib_status=status,
                        quantity_requested=qty_requested,
                        quantity_submitted=qty,
                        sizing=sizing_meta,
                    )
                if status == 'Inactive':
                    logger.warning(f"Order for {symbol} is inactive - attempting retry after delay")
                    await asyncio.sleep(5)
                    updated_trade = None
                    try:
                        for existing_trade in self.get_ib().trades():
                            if existing_trade.order.orderId == trade.order.orderId:
                                updated_trade = existing_trade
                                break

                        if updated_trade and hasattr(updated_trade, 'orderStatus'):
                            updated_status = updated_trade.orderStatus.status
                            if updated_status in ['Filled', 'Submitted', 'PendingSubmit', 'PreSubmitted']:
                                logger.info(f"Order for {symbol} became active after retry: {updated_status}")
                                return PlaceOrderResult(
                                    order_id=trade.order.orderId,
                                    ok=True,
                                    code="OK",
                                    message=f"Order accepted after retry ({updated_status})",
                                    ib_status=updated_status,
                                    quantity_requested=qty_requested,
                                    quantity_submitted=qty,
                                    sizing=sizing_meta,
                                )
                            logger.error(f"Order for {symbol} remains inactive after retry: {updated_status}")
                    except Exception as retry_error:
                        logger.warning(f"Error during retry check for {symbol}: {retry_error}")

                    try:
                        self.get_ib().cancelOrder(trade.order)
                        logger.info(f"Cancelled inactive order for {symbol}")
                    except Exception as cancel_error:
                        logger.warning(f"Could not cancel inactive order: {cancel_error}")

                    diag_trade = updated_trade if updated_trade is not None else trade
                    _os = getattr(diag_trade, "orderStatus", None) if diag_trade else None
                    _ibst = getattr(_os, "status", None) if _os else None
                    res = place_order_failure_from_trade(
                        diag_trade,
                        "ORDER_INACTIVE",
                        f"Order inactive or rejected after retry (ib_status={_ibst})",
                    )
                    logger.error(
                        "PLACE_ORDER_FAILED symbol=%s code=%s ib_status=%s ib_error_code=%s log_tail=%s",
                        symbol.upper(),
                        res.code,
                        res.ib_status,
                        res.ib_error_code,
                        res.ib_log[-3:] if res.ib_log else [],
                    )
                    return res
                if status == 'Cancelled':
                    res = place_order_failure_from_trade(trade, "ORDER_CANCELLED", "Order cancelled by IB/TWS")
                    logger.error(
                        "PLACE_ORDER_FAILED symbol=%s code=%s ib_status=%s ib_error_code=%s",
                        symbol.upper(),
                        res.code,
                        res.ib_status,
                        res.ib_error_code,
                    )
                    return res
                res = place_order_failure_from_trade(
                    trade,
                    "IB_STATUS_NOT_ACCEPTED",
                    f"Order not in an accepted state: {status}",
                )
                logger.error(
                    "PLACE_ORDER_FAILED symbol=%s code=%s ib_status=%s ib_error_code=%s log_tail=%s",
                    symbol.upper(),
                    res.code,
                    res.ib_status,
                    res.ib_error_code,
                    res.ib_log[-3:] if res.ib_log else [],
                )
                return res

            logs = _serialize_ib_trade_log(trade)
            res = PlaceOrderResult(
                ok=False,
                code="NO_ORDER_STATUS",
                message="No order status on trade object after placeOrder",
                ib_log=logs,
                ib_error_code=_ib_log_error_code(logs),
            )
            logger.error(
                "PLACE_ORDER_FAILED symbol=%s code=%s ib_error_code=%s log=%s",
                symbol.upper(),
                res.code,
                res.ib_error_code,
                res.ib_log[-5:] if res.ib_log else [],
            )
            return res

        except Exception as e:
            res = PlaceOrderResult(
                ok=False,
                code="EXCEPTION",
                message=str(e),
            )
            logger.error(
                "PLACE_ORDER_FAILED symbol=%s code=%s message=%s",
                symbol.upper(),
                res.code,
                res.message,
            )
            return res

    async def _get_market_reference_price(self, contract, symbol: str) -> Optional[float]:
        """Get best-effort reference price (last/market/bid/ask midpoint)."""
        try:
            ticker = self.get_ib().reqMktData(contract, '', True, False)
            await asyncio.sleep(1.0)

            candidates = []
            if hasattr(ticker, 'marketPrice'):
                mp = ticker.marketPrice()
                if mp and mp > 0:
                    candidates.append(float(mp))
            if hasattr(ticker, 'last') and ticker.last and ticker.last > 0:
                candidates.append(float(ticker.last))
            if hasattr(ticker, 'close') and ticker.close and ticker.close > 0:
                candidates.append(float(ticker.close))
            if hasattr(ticker, 'bid') and hasattr(ticker, 'ask'):
                if ticker.bid and ticker.ask and ticker.bid > 0 and ticker.ask > 0:
                    candidates.append(float((ticker.bid + ticker.ask) / 2))

            return candidates[0] if candidates else None
        except Exception as e:
            logger.debug(f"Could not fetch market snapshot for {symbol}: {e}")
            return None
        finally:
            try:
                self.get_ib().cancelMktData(contract)
            except Exception:
                pass

    async def _get_min_tick(self, contract) -> Optional[float]:
        """Get exchange minimum tick size for the contract."""
        try:
            details = await self.get_ib().reqContractDetailsAsync(contract)
            if details and details[0].minTick and details[0].minTick > 0:
                return float(details[0].minTick)
        except Exception as e:
            logger.debug(f"Could not fetch min tick for {getattr(contract, 'symbol', 'unknown')}: {e}")
        return None

    def _round_to_tick(self, price: float, min_tick: float, action: str) -> float:
        if min_tick <= 0:
            return float(price)
        steps = price / min_tick
        if action.upper() == 'BUY':
            rounded_steps = math.ceil(steps)
        elif action.upper() == 'SELL':
            rounded_steps = math.floor(steps)
        else:
            rounded_steps = round(steps)
        rounded = rounded_steps * min_tick
        return float(round(rounded, 8))

    def _anchor_limit_price_near_market(
        self,
        requested_price: float,
        market_price: float,
        action: str,
        max_distance_bps: float = 35
    ) -> float:
        """
        Clamp requested limit price to a tight band around market price so orders
        remain meaningful and not far from current market conditions.
        """
        if market_price <= 0:
            return float(requested_price)
        band = market_price * (max_distance_bps / 10000.0)
        lower = market_price - band
        upper = market_price + band
        clamped = min(max(requested_price, lower), upper)

        # Keep intent directional:
        # BUY: do not place below lower bound when trying to enter quickly
        # SELL: do not place above upper bound when trying to enter quickly
        if action.upper() == 'BUY':
            return float(max(clamped, lower))
        if action.upper() == 'SELL':
            return float(min(clamped, upper))
        return float(clamped)

    async def get_account_equity(self) -> float:
        """Get total account equity for position sizing"""
        try:
            if not self.is_connected():
                logger.warning("Not connected to IBKR for account equity check - using paper trading default")
                return 100000.0  # Paper trading default
            
            try:
                ib = self.get_ib()
                accounts = ib.managedAccounts() or []
                acct = accounts[0] if accounts else ""
                account_values = await ib.accountSummaryAsync(acct)
                nl = _account_summary_tag_float(account_values, "NetLiquidation")
                if nl is not None:
                    logger.info(f"Account equity (summary): ${nl:,.2f}")
                    return nl
            except Exception as e:
                logger.warning(f"Account summary method failed: {e}")
            
            # Method 2: Try account values (fallback)
            try:
                account_values = self.get_ib().accountValues()
                for value in account_values:
                    if value.tag == 'NetLiquidationByCurrency' and value.currency == 'USD':
                        equity = float(value.value)
                        logger.info(f"Account equity (values): ${equity:,.2f}")
                        return equity
                        
            except Exception as e:
                logger.warning(f"Account values method failed: {e}")
            
            # For paper trading, use reasonable default
            logger.warning("Could not get real account equity - using paper trading default")
            return 100000.0  # Paper trading default
            
        except Exception as e:
            logger.error(f"Error getting account equity: {e}")
            return 100000.0  # Paper trading default

    async def get_crypto_positions(self, symbol: str) -> float:
        """Get current crypto position for a symbol"""
        try:
            if not self.is_connected():
                logger.warning("Not connected to IBKR for position check")
                return 0.0
            
            positions = self.get_ib().positions()
            for position in positions:
                if (hasattr(position.contract, 'symbol') and 
                    position.contract.symbol == symbol and 
                    hasattr(position.contract, 'secType') and
                    position.contract.secType == 'CRYPTO'):
                    current_position = float(position.position)
                    logger.info(f"Current {symbol} position: {current_position} shares")
                    return current_position
            
            logger.info(f"No existing {symbol} position found")
            return 0.0
            
        except Exception as e:
            logger.error(f"Error getting crypto positions: {e}")
            return 0.0

    async def get_order_status(self, order_id: int) -> Dict:
        """Get the status of an order."""
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")

            trades = self.get_ib().openTrades()
            for trade in trades:
                if trade.order.orderId == order_id:
                    status = trade.orderStatus
                    return {
                        'filled': status.status == 'Filled',
                        'avg_fill_price': status.avgFillPrice if status.status == 'Filled' else 0.0
                    }

            fills = self.get_ib().fills()
            for fill in fills:
                if fill.order.orderId == order_id:
                    return {
                        'filled': True,
                        'avg_fill_price': fill.execution.avgPrice
                    }

            logger.warning(f"Order {order_id} not found")
            return {'filled': False, 'avg_fill_price': 0.0}
        except Exception as e:
            logger.error(f"Failed to get order status: {str(e)}")
            return {'filled': False, 'avg_fill_price': 0.0}

    async def cancel_order(self, order_id: int) -> bool:
        """Cancel an existing order."""
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")

            trades = self.get_ib().openTrades()
            for trade in trades:
                if trade.order.orderId == order_id:
                    self.get_ib().cancelOrder(trade.order)
                    logger.info(f"Cancelled order {order_id}")
                    return True

            logger.warning(f"Order {order_id} not found for cancellation")
            return False
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {str(e)}")
            return False