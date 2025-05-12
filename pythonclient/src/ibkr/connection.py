"""
IBKR Connection Manager
Handles connection to IBKR Gateway/TWS and provides connection status.
Version 1.3
"""
import logging
import asyncio
from ib_insync import IB, MarketOrder, StopOrder, LimitOrder, Trade
from typing import List, Optional, Dict
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
            else:
                order = MarketOrder(action, quantity, tif='GTC', transmit=True)

            trade = self.get_ib().placeOrder(contract, order)
            await asyncio.sleep(1)  # Allow time for order to process
            await trade.logResponsesAsync()  # Wait for order confirmation

            if trade.orderStatus.status in ['Filled', 'Submitted']:
                order_id = trade.order.orderId
                logger.info(f"Placed order {order_id}: {action} {quantity} shares of {symbol}")
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