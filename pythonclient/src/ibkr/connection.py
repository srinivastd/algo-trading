"""
IBKR Connection Manager
Handles connection to IBKR Gateway/TWS and provides connection status.
"""
import logging
import asyncio
from ib_insync import IB
from typing import List, Optional

logger = logging.getLogger(__name__)

class IBKRConnection:
    def __init__(self):
        self.ib = IB()
        self._connected = False
        
    async def connect(self, client_id: int = 0, max_retries: int = 3, retry_delay: int = 2) -> bool:
        """
        Connect to IBKR Gateway/TWS.
        
        Args:
            client_id: Client ID to use for this connection (default: 0)
            max_retries: Maximum number of connection attempts
            retry_delay: Delay between retries in seconds
            
        Returns:
            bool: True if connection successful, False otherwise
        """
        for attempt in range(max_retries):
            try:
                logger.info(f"Connection attempt {attempt + 1}/{max_retries}")
                
                # Connect to IBKR Gateway Paper Trading
                # Port 4002 is for paper trading, 4001 is for live trading
                await self.ib.connectAsync('127.0.0.1', 4002, clientId=client_id)
                
                if self.ib.isConnected():
                    self._connected = True
                    logger.info("Successfully connected to IBKR Gateway")
                    
                    # Get and log account information
                    accounts = self.ib.managedAccounts()
                    if accounts:
                        logger.info(f"Connected to account: {accounts[0]}")
                        
                        # Get account summary to verify it's paper trading
                        account_summary = await self.ib.reqAccountSummaryAsync()
                        logger.info("Account Summary:")
                        for summary in account_summary:
                            logger.info(f"{summary.tag}: {summary.value}")
                    
                    return True
                    
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
        return self.ib.isConnected() if self.ib else False
        
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