"""
Test script for IBKRConnection class.
Tests connection, account info, and disconnection functionality.
"""
import asyncio
import logging
import sys
from src.ibkr.connection import IBKRConnection

# Set up logging to both file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('connection_test.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)

async def test_connection():
    """Test the IBKRConnection class functionality."""
    logger.info("="*50)
    logger.info("Starting IBKR Connection Test")
    logger.info("="*50)
    
    # Initialize connection
    connection = IBKRConnection()
    
    try:
        # Test connection
        logger.info("Testing connection...")
        if await connection.connect():
            logger.info("✓ Connection successful")
            
            # Test account retrieval
            logger.info("\nTesting account retrieval...")
            accounts = await connection.get_accounts()
            if accounts:
                logger.info("✓ Accounts retrieved successfully")
                logger.info("Available accounts:")
                for acc in accounts:
                    logger.info(f"- {acc}")
            else:
                logger.error("✗ Failed to retrieve accounts")
            
            # Test next order ID
            logger.info("\nTesting next order ID retrieval...")
            next_id = await connection.get_next_order_id()
            if next_id is not None:
                logger.info(f"✓ Next order ID: {next_id}")
            else:
                logger.error("✗ Failed to get next order ID")
                
        else:
            logger.error("✗ Connection failed")
            
    except Exception as e:
        logger.error(f"Error during test: {e}", exc_info=True)
        
    finally:
        # Test disconnection
        logger.info("\nTesting disconnection...")
        await connection.disconnect()
        if not connection.is_connected():
            logger.info("✓ Disconnection successful")
        else:
            logger.error("✗ Disconnection failed")
            
    logger.info("="*50)
    logger.info("Connection Test Complete")
    logger.info("="*50)

if __name__ == "__main__":
    asyncio.run(test_connection()) 