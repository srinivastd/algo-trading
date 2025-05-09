"""
NVDA Breakout Pullback Strategy with Candlestick Confirmation using ib_insync.
Integrates account verification, summary, and order ID fetching.
"""
import asyncio
import logging
from ib_insync import *
import pandas as pd
from ta.momentum import RSIIndicator
import numpy as np

# Set up logging
logging.basicConfig(filename='nvda_trades.log', level=logging.INFO)

async def main():
    # Create IB connection instance
    ib = IB()
    
    try:
        # Connect to IBKR Gateway Paper Trading (port 4002)
        await ib.connectAsync('127.0.0.1', 4002, clientId=0)
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

        # Define NVDA contract
        contract = Stock('NVDA', 'SMART', 'USD')

        # Request historical data (1-hour bars for the last month)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr='1 M',
            barSizeSetting='1 hour',
            whatToShow='TRADES',
            useRTH=True
        )

        # Convert to DataFrame
        df = pd.DataFrame(bars)
        df['date'] = df['date'].apply(lambda x: x.astimezone(pd.UTC))
        df = df.set_index('date')

        # Calculate technical indicators
        df['rsi'] = RSIIndicator(df['close']).rsi()
        df['ma_20'] = df['close'].rolling(window=20).mean()
        df['ma_50'] = df['close'].rolling(window=50).mean()
        df['volume_ma'] = df['volume'].rolling(window=20).mean()

        # Request real-time data
        ib.qualifyContracts(contract)
        ticker = ib.reqMktData(contract, '', True, False)

        # Strategy functions
        def is_bullish_engulfing(df, i):
            if i > 0 and df['open'].iloc[i] < df['close'].iloc[i-1] and df['close'].iloc[i] > df['open'].iloc[i-1]:
                return df['close'].iloc[i] > df['open'].iloc[i]
            return False

        def is_hammer(df, i):
            body = abs(df['close'].iloc[i] - df['open'].iloc[i])
            lower_wick = df['low'].iloc[i] - min(df['open'].iloc[i], df['close'].iloc[i])
            upper_wick = max(df['open'].iloc[i], df['close'].iloc[i]) - df['high'].iloc[i]
            return body > 0 and lower_wick > 2 * body and upper_wick < body / 2

        def is_consolidation(df, start, end):
            range_high = df['high'][start:end].max()
            range_low = df['low'][start:end].min()
            return (range_high - range_low) / df['close'].iloc[start] < 0.02  # 2% range

        # Trade execution logic
        for i in range(5, len(df)-1):
            if (is_consolidation(df, i-5, i) and 
                df['close'].iloc[i] > df['high'].iloc[i-5:i].max() and 
                df['close'].iloc[i] > df['ma_50'].iloc[i] and  # Uptrend confirmation
                (is_bullish_engulfing(df, i) or is_hammer(df, i)) and 
                30 <= df['rsi'].iloc[i] <= 70 and 
                df['volume'].iloc[i] > 1.5 * df['volume_ma'].iloc[i]):
                breakout_price = df['close'].iloc[i]
                pullback_level = df['ma_20'].iloc[i+1]
                if (i+2 < len(df) and df['close'].iloc[i+1] < breakout_price and 
                    df['close'].iloc[i+2] > pullback_level):
                    entry_price = pullback_level
                    stop_loss = df['low'].iloc[i-5:i].min()
                    target = breakout_price + (breakout_price - stop_loss) * 2  # 2:1 reward-to-risk

                    # Calculate position size
                    risk_amount = 1000  # 1% of $100,000
                    risk_per_share = entry_price - stop_loss
                    if risk_per_share <= 0:
                        continue
                    shares = int(risk_amount // risk_per_share)

                    # Place bracket order
                    if shares > 0:
                        order = BracketOrder(
                            parent=LimitOrder('BUY', shares, entry_price, orderId=next_order_id),
                            takeProfit=LimitOrder('SELL', shares, target, orderId=next_order_id+1),
                            stopLoss=StopOrder('SELL', shares, stop_loss, orderId=next_order_id+2)
                        )
                      ##  trade = ib.placeOrder(contract, order)
                        logging.info(f"Trade: Entry={entry_price}, Stop={stop_loss}, Target={target}, Shares={shares}")
                     ##   print(f"Trade submitted: {trade}")
                        # Increment order ID for the next trade
                        next_order_id += 3

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