"""
IBKR Connection Manager
Handles connection to IBKR Gateway/TWS and provides connection status.
"""
import logging
import asyncio
from ib_insync import IB
from typing import List, Optional
import sys

logger = logging.getLogger(__name__)

class IBKRConnection:
    def __init__(self):
        self.ib = IB()
        self._connected = False
        self.client = self.ib  # For compatibility with main.py
        
    async def connect(self, client_id: int = 0, paper: bool = True, max_retries: int = 3, retry_delay: int = 2) -> bool:
        """
        Connect to IBKR Gateway/TWS.
        
        Args:
            client_id: Client ID to use for this connection (default: 0)
            paper: If True, connect to paper trading account (default: True)
            max_retries: Maximum number of connection attempts
            retry_delay: Delay between retries in seconds
            
        Returns:
            bool: True if connection successful, False otherwise
        """
        # Print trading mode banner
        logger.info("="*80)
        if paper:
            logger.info("PAPER TRADING MODE")
            logger.info("This is a simulation - no real trades will be executed")
            logger.info("Port: 4002 (Paper Trading)")
        else:
            logger.warning("LIVE TRADING MODE")
            logger.warning("WARNING: This will execute real trades with real money")
            logger.warning("Port: 4001 (Live Trading)")
            
            # Ask for confirmation in live trading mode
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
                
                # Set host and port based on paper trading flag
                if paper:
                    host = "127.0.0.1"
                    port = 4002
                    logger.info("Connecting to paper trading account at 127.0.0.1:4002")
                else:
                    host = "127.0.0.1"
                    port = 4001
                    logger.info("Connecting to live account at 127.0.0.1:4001")
                
                # Check if IBKR Gateway is running
                try:
                    # Connect to IBKR Gateway
                    await self.ib.connectAsync(host, port, clientId=client_id, timeout=10)
                except ConnectionRefusedError:
                    logger.error("Connection refused. Please ensure IBKR Gateway is running.")
                    if paper:
                        logger.info("For paper trading: Start IBKR Gateway and enable paper trading")
                        logger.info("1. Open IBKR Gateway")
                        logger.info("2. Log in to your paper trading account")
                        logger.info("3. Ensure paper trading is enabled")
                        logger.info("4. Verify port 4002 is available")
                    else:
                        logger.info("For live trading: Start IBKR Gateway and log in to your account")
                        logger.info("1. Open IBKR Gateway")
                        logger.info("2. Log in to your live trading account")
                        logger.info("3. Verify port 4001 is available")
                    return False
                except Exception as e:
                    logger.error(f"Connection error: {str(e)}")
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                    continue
                
                if self.ib.isConnected():
                    self._connected = True
                    logger.info("Successfully connected to IBKR Gateway")
                    
                    # Get and log account information
                    accounts = self.ib.managedAccounts()
                    if accounts:
                        logger.info(f"Connected to account: {accounts[0]}")
                        
                        # Get account summary to verify trading mode
                        try:
                            account_summary = await self.ib.reqAccountSummaryAsync()
                            logger.info("\nAccount Summary:")
                            for summary in account_summary:
                                logger.info(f"{summary.tag}: {summary.value}")
                            
                            # Verify trading mode
                            if paper:
                                logger.info("\nVerifying paper trading mode...")
                                # Add any specific checks for paper trading account
                            else:
                                logger.warning("\nVerifying live trading mode...")
                                # Add any specific checks for live trading account
                                
                        except Exception as e:
                            logger.warning(f"Could not fetch account summary: {str(e)}")
                    
                    return True
                else:
                    logger.error("Connection failed - not connected after connectAsync")
                    
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