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
from ib_insync import IB, MarketOrder, StopOrder, LimitOrder, Trade, Stock, Order, Crypto
from typing import List, Optional, Dict
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.crypto_utils import CryptoUtils

logger = logging.getLogger(__name__)

class IBKRConnection:
    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 0):
        """
        Initialize IBKR connection with host, port, and client ID.
        
        Args:
            host: Host address (default: "127.0.0.1")
            port: Port number (4002 for paper, 7497 for live, default: 4002)
            client_id: Client ID for this connection (default: 0)
        """
        self.ib = IB()
        self.host = host
        self.port = port
        self.client_id = client_id
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

    async def place_order(self, symbol: str, action: str, quantity: int, order_type: str,
                         limit_price: float = None, aux_price: float = None,
                         parent_id: int = None) -> Optional[int]:
        """Place a trade order."""
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")

            # Pre-validate the symbol to catch issues early
            logger.debug(f"Validating symbol {symbol} before placing order...")
            if not await self.validate_symbol(symbol):
                logger.error(f"Symbol validation failed for {symbol} - cannot place order")
                return None

            market_open = await self.is_market_open(symbol)

            # Check if market is open for this symbol
            if not market_open:
                requested_price = limit_price if order_type == 'LMT' else aux_price if order_type == 'STP' else None
                logger.info(
                    "ORDER_ATTEMPT symbol=%s action=%s qty=%s type=%s market_open=%s ref=%s "
                    "requested=%s final=%s tick=%s parent=%s status=SKIPPED_MARKET_CLOSED",
                    symbol.upper(),
                    action.upper(),
                    quantity,
                    order_type,
                    market_open,
                    None,
                    f"{requested_price:.6f}" if requested_price is not None else "MKT",
                    None,
                    None,
                    parent_id if parent_id is not None else "-",
                )
                logger.info(f"Skipping order for {symbol}: market is closed")
                return None

            # Create appropriate contract based on symbol type (crypto vs equity)
            contract = CryptoUtils.create_contract(symbol)
            
            # Validate contract before placing order
            qualified_contracts = await self.get_ib().qualifyContractsAsync(contract)
            if not qualified_contracts:
                logger.error(f"Could not qualify contract for {symbol} - symbol may be inactive or invalid")
                return None
            
            # Use the first qualified contract
            contract = qualified_contracts[0]
            logger.debug(f"Contract qualified for {symbol}: {contract}")
            
            # Verify contract has valid details using the validation utility
            if not CryptoUtils.validate_contract(contract, symbol):
                logger.error(f"Contract validation failed for {symbol}")
                return None

            # Determine if this is a crypto order
            is_crypto = CryptoUtils.is_crypto(symbol)

            # Obtain market snapshot + minimum tick for meaningful, valid pricing.
            market_price = await self._get_market_reference_price(contract, symbol)
            min_tick = await self._get_min_tick(contract)

            # Normalize incoming prices to exchange tick size and keep LMT orders
            # close to current market to avoid unrealistic stale backtest pricing.
            requested_price = limit_price if order_type == 'LMT' else aux_price if order_type == 'STP' else None
            if order_type == 'LMT' and limit_price is not None:
                if market_price and market_price > 0:
                    limit_price = self._anchor_limit_price_near_market(
                        requested_price=limit_price,
                        market_price=market_price,
                        action=action,
                        max_distance_bps=35  # 0.35% max drift from market
                    )
                if min_tick:
                    limit_price = self._round_to_tick(limit_price, min_tick, action)

            if order_type == 'STP' and aux_price is not None and min_tick:
                # Stops should follow protective direction.
                # SELL stop rounds down, BUY stop rounds up.
                aux_price = self._round_to_tick(aux_price, min_tick, action)

            final_price = limit_price if order_type == 'LMT' else aux_price if order_type == 'STP' else None
            logger.info(
                "ORDER_ATTEMPT symbol=%s action=%s qty=%s type=%s market_open=%s ref=%s "
                "requested=%s final=%s tick=%s parent=%s status=SUBMITTING",
                symbol.upper(),
                action.upper(),
                quantity,
                order_type,
                market_open,
                f"{market_price:.6f}" if market_price is not None else "NA",
                f"{requested_price:.6f}" if requested_price is not None else "MKT",
                f"{final_price:.6f}" if final_price is not None else "MKT",
                f"{min_tick:.6f}" if min_tick is not None else "NA",
                parent_id if parent_id is not None else "-",
            )
            
            # Set appropriate TIF (Time In Force) for crypto vs equity
            tif = 'IOC' if is_crypto else 'GTC'  # Crypto must use IOC, stocks can use GTC
            
            if order_type == 'LMT':
                order = LimitOrder(action, quantity, limit_price, tif=tif, transmit=True)
                if is_crypto:
                    order.outsideRth = True  # Allow 24/7 trading for crypto
            elif order_type == 'STP':
                order = StopOrder(action, quantity, aux_price, tif=tif, transmit=True)
                if is_crypto:
                    order.outsideRth = True  # Allow 24/7 trading for crypto
                if parent_id:
                    order.parentId = parent_id
            else:
                order = MarketOrder(action, quantity, tif=tif, transmit=True)
                if is_crypto:
                    order.outsideRth = True  # Allow 24/7 trading for crypto

            trade = self.get_ib().placeOrder(contract, order)
            await asyncio.sleep(3)  # Increased wait time for order processing

            # Check order status after placement with better error handling
            if hasattr(trade, 'orderStatus') and trade.orderStatus:
                status = trade.orderStatus.status
                if status in ['Filled', 'Submitted', 'PendingSubmit', 'PreSubmitted']:
                    order_id = trade.order.orderId
                    asset_type = "CRYPTO" if is_crypto else "EQUITY"
                    price_str = f"{limit_price or aux_price or 'market'}"
                    logger.info(f"Placed {asset_type} order {order_id}: {action} {quantity} shares of {symbol} at {price_str} (TIF: {tif})")
                    return order_id
                elif status == 'Inactive':
                    logger.warning(f"Order for {symbol} is inactive - attempting retry after delay")
                    
                    # Wait and check if order becomes active
                    await asyncio.sleep(5)
                    
                    # Refresh the trade object to get updated status
                    try:
                        # Get updated trade status
                        updated_trade = None
                        for existing_trade in self.get_ib().trades():
                            if existing_trade.order.orderId == trade.order.orderId:
                                updated_trade = existing_trade
                                break
                        
                        if updated_trade and hasattr(updated_trade, 'orderStatus'):
                            updated_status = updated_trade.orderStatus.status
                            if updated_status in ['Filled', 'Submitted', 'PendingSubmit', 'PreSubmitted']:
                                logger.info(f"Order for {symbol} became active after retry: {updated_status}")
                                return trade.order.orderId
                            else:
                                logger.error(f"Order for {symbol} remains inactive after retry: {updated_status}")
                        
                    except Exception as retry_error:
                        logger.warning(f"Error during retry check for {symbol}: {retry_error}")
                    
                    # Cancel the inactive order to clean up
                    try:
                        self.get_ib().cancelOrder(trade.order)
                        logger.info(f"Cancelled inactive order for {symbol}")
                    except Exception as cancel_error:
                        logger.warning(f"Could not cancel inactive order: {cancel_error}")
                    return None
                elif status == 'Cancelled':
                    logger.error(f"Order for {symbol} was cancelled by the system")
                    return None
                else:
                    logger.error(f"Order failed for {symbol}: {status}")
                    return None
            else:
                logger.error(f"No order status available for {symbol} - trade object may be invalid")
                return None
                
        except Exception as e:
            logger.error(f"Failed to place order for {symbol}: {str(e)}")
            return None

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
            
            # Method 1: Try account summary with proper API call
            try:
                # For ib_insync, reqAccountSummaryAsync takes positional arguments
                account_values = await self.get_ib().reqAccountSummaryAsync(
                    'All',
                    'NetLiquidation'
                )
                
                for value in account_values:
                    if value.tag == 'NetLiquidation' and value.currency == 'USD':
                        equity = float(value.value)
                        logger.info(f"Account equity (summary): ${equity:,.2f}")
                        return equity
                        
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