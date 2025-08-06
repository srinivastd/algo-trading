"""
IBKR Connection Manager
Handles connection to IBKR Gateway/TWS and provides connection status.
Version 1.4 - Added Accumulate/Distribute order support
"""
import logging
import asyncio
from ib_insync import IB, MarketOrder, StopOrder, LimitOrder, Trade, Stock, Order
from typing import List, Optional, Dict, Tuple
import sys

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

    async def place_accumulate_order(self, symbol: str, action: str, total_quantity: int, 
                                   base_price: float, price_increment: float = 0.01,
                                   level_size: int = None, max_levels: int = 5) -> Optional[int]:
        """
        Place an Accumulate order to gradually buy/sell as price moves favorably.
        
        Args:
            symbol: Trading symbol
            action: 'BUY' or 'SELL'
            total_quantity: Total quantity to accumulate/distribute
            base_price: Starting price level
            price_increment: Price increment between levels
            level_size: Quantity per level (if None, evenly distributed)
            max_levels: Maximum number of price levels
            
        Returns:
            Order ID if successful, None otherwise
        """
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")

            contract = Stock(symbol, "SMART", "USD")
            await self.get_ib().qualifyContractsAsync(contract)

            # Calculate level size if not provided
            if level_size is None:
                level_size = total_quantity // max_levels
                if total_quantity % max_levels != 0:
                    level_size += 1

            # Create scale order (Accumulate/Distribute)
            order = Order()
            order.action = action
            order.totalQuantity = total_quantity
            order.orderType = "SCALE"
            order.scaleInitLevelSize = level_size
            order.scaleSubsLevelSize = level_size
            order.scalePriceIncrement = price_increment
            order.scaleAutoReset = True
            order.tif = "GTC"
            order.transmit = True

            # Set initial price based on action
            if action == "BUY":
                # For accumulate (buy), start at base price and go down
                order.lmtPrice = base_price
            else:
                # For distribute (sell), start at base price and go up
                order.lmtPrice = base_price

            trade = self.get_ib().placeOrder(contract, order)
            await asyncio.sleep(2)

            if trade.orderStatus.status in ['Filled', 'Submitted', 'PendingSubmit']:
                order_id = trade.order.orderId
                logger.info(f"Placed ACCUMULATE order {order_id}: {action} {total_quantity} shares of {symbol} "
                          f"starting at ${base_price:.2f} with ${price_increment:.2f} increments")
                return order_id
            else:
                logger.error(f"Accumulate order failed: {trade.orderStatus.status}")
                return None
        except Exception as e:
            logger.error(f"Failed to place accumulate order: {str(e)}")
            return None

    async def place_distribute_order(self, symbol: str, action: str, total_quantity: int,
                                   base_price: float, price_increment: float = 0.01,
                                   level_size: int = None, max_levels: int = 5) -> Optional[int]:
        """
        Place a Distribute order to gradually sell/buy as price moves favorably.
        This is essentially the same as accumulate but with different price direction.
        
        Args:
            symbol: Trading symbol
            action: 'BUY' or 'SELL' (opposite of accumulate direction)
            total_quantity: Total quantity to distribute
            base_price: Starting price level
            price_increment: Price increment between levels
            level_size: Quantity per level (if None, evenly distributed)
            max_levels: Maximum number of price levels
            
        Returns:
            Order ID if successful, None otherwise
        """
        return await self.place_accumulate_order(
            symbol=symbol,
            action=action,
            total_quantity=total_quantity,
            base_price=base_price,
            price_increment=price_increment,
            level_size=level_size,
            max_levels=max_levels
        )

    async def place_smart_accumulate_order(self, symbol: str, action: str, total_quantity: int,
                                         current_price: float, volatility: float = 0.02,
                                         risk_tolerance: float = 0.5) -> Optional[int]:
        """
        Place a smart accumulate order with dynamic price increments based on volatility.
        
        Args:
            symbol: Trading symbol
            action: 'BUY' or 'SELL'
            total_quantity: Total quantity to accumulate/distribute
            current_price: Current market price
            volatility: Expected price volatility (default 2%)
            risk_tolerance: Risk tolerance (0.1 = conservative, 0.9 = aggressive)
            
        Returns:
            Order ID if successful, None otherwise
        """
        try:
            # Calculate dynamic price increment based on volatility and risk tolerance
            price_increment = current_price * volatility * risk_tolerance
            
            # Calculate optimal number of levels based on volatility
            if volatility < 0.01:  # Low volatility
                max_levels = 3
            elif volatility < 0.03:  # Medium volatility
                max_levels = 5
            else:  # High volatility
                max_levels = 7

            # Adjust base price based on action
            if action == "BUY":
                # For accumulate (buy), start slightly below current price
                base_price = current_price * (1 - volatility * 0.5)
            else:
                # For distribute (sell), start slightly above current price
                base_price = current_price * (1 + volatility * 0.5)

            logger.info(f"Smart accumulate order: {action} {total_quantity} {symbol} "
                      f"at ${base_price:.2f} with ${price_increment:.3f} increments "
                      f"(volatility: {volatility:.1%}, levels: {max_levels})")

            return await self.place_accumulate_order(
                symbol=symbol,
                action=action,
                total_quantity=total_quantity,
                base_price=base_price,
                price_increment=price_increment,
                max_levels=max_levels
            )
        except Exception as e:
            logger.error(f"Failed to place smart accumulate order: {str(e)}")
            return None

    async def place_order(self, symbol: str, action: str, quantity: int, order_type: str,
                         limit_price: float = None, aux_price: float = None,
                         parent_id: int = None) -> Optional[int]:
        """Place a trade order."""
        try:
            if not self.is_connected():
                raise ConnectionError("Not connected to IBKR")

            contract = Stock(symbol, "SMART", "USD")
            await self.get_ib().qualifyContractsAsync(contract)

            if order_type == 'LMT':
                order = LimitOrder(action, quantity, limit_price, tif='GTC', transmit=True)
            elif order_type == 'STP':
                order = StopOrder(action, quantity, aux_price, tif='GTC', transmit=True)
                if parent_id:
                    order.parentId = parent_id
            elif order_type == 'ACCUMULATE':
                # Use smart accumulate for better profit maximization
                return await self.place_smart_accumulate_order(
                    symbol=symbol,
                    action=action,
                    total_quantity=quantity,
                    current_price=limit_price or 0,
                    volatility=0.02  # Default 2% volatility
                )
            elif order_type == 'DISTRIBUTE':
                # Use smart accumulate for distribution
                return await self.place_smart_accumulate_order(
                    symbol=symbol,
                    action=action,
                    total_quantity=quantity,
                    current_price=limit_price or 0,
                    volatility=0.02  # Default 2% volatility
                )
            else:
                order = MarketOrder(action, quantity, tif='GTC', transmit=True)

            trade = self.get_ib().placeOrder(contract, order)
            await asyncio.sleep(2)  # Allow time for order to process

            # Check order status after placement
            if trade.orderStatus.status in ['Filled', 'Submitted', 'PendingSubmit']:
                order_id = trade.order.orderId
                logger.info(f"Placed order {order_id}: {action} {quantity} shares of {symbol} at {limit_price or 'market'}")
                return order_id
            else:
                logger.error(f"Order failed: {trade.orderStatus.status}")
                return None
        except Exception as e:
            logger.error(f"Failed to place order: {str(e)}")
            return None

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