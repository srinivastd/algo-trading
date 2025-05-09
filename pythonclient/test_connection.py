"""
Test file for IBKR connectivity using ib_insync library.
"""
from ib_insync import *
import asyncio

async def main():
    # Create IB connection instance
    ib = IB()
    
    try:
        # Connect to IBKR Gateway Paper Trading
        # Port 4002 is for paper trading, 4001 is for live trading
        await ib.connectAsync('127.0.0.1', 4002, clientId=0)
        
        # Check connection status
        print("Connected:", ib.isConnected())
        
        # Get all available accounts
        accounts = ib.managedAccounts()
        print("\nAvailable accounts:")
        for acc in accounts:
            print(f"- {acc}")
            
        # Get account information
        account = accounts[0]
        print(f"\nConnected to account: {account}")
        
        # Get account summary to verify it's paper trading
        account_summary = await ib.reqAccountSummaryAsync()
        print("\nAccount Summary:")
        for summary in account_summary:
            print(f"{summary.tag}: {summary.value}")
        
        # Get next valid order ID
        next_order_id = ib.client.getReqId()
        print(f"\nNext valid order ID: {next_order_id}")
        
        # Keep the connection alive for a few seconds to demonstrate
        await asyncio.sleep(3)
        
    except Exception as e:
        print(f"Error occurred: {e}")
    
    finally:
        # Disconnect
        ib.disconnect()
        print("\nDisconnected from IBKR")

if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())