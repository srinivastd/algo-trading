"""
Enhanced NVDA Breakout Pullback Strategy Backtest with realistic exit conditions.
"""
import asyncio
import logging
from ib_insync import *
import pandas as pd
from ta.momentum import RSIIndicator
import numpy as np

# Set up logging
logging.basicConfig(filename='nvda_backtest_enhanced.log', level=logging.INFO)

async def main():
    # Create IB connection instance (for data only)
    ib = IB()
    
    try:
        # Connect to IBKR Gateway Paper Trading
        await ib.connectAsync('127.0.0.1', 4002, clientId=0)
        print("Connected:", ib.isConnected())

        # Define NVDA contract
        contract = Stock('NVDA', 'SMART', 'USD')

        # Request historical data (1-year 1-hour bars)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime='',
            durationStr='1 Y',
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
            return (range_high - range_low) / df['close'].iloc[start] < 0.02

        # Backtest variables
        initial_capital = 100000
        risk_per_trade = 1000  # 1% of $100,000
        capital = initial_capital
        total_trades = 0
        wins = 0
        losses = 0

        # Simulate trades with realistic exits
        for i in range(5, len(df)-24):  # Ensure enough candles for multi-candle resolution
            if (is_consolidation(df, i-5, i) and 
                df['close'].iloc[i] > df['high'].iloc[i-5:i].max() and 
                df['close'].iloc[i] > df['ma_50'].iloc[i] and 
                (is_bullish_engulfing(df, i) or is_hammer(df, i)) and 
                30 <= df['rsi'].iloc[i] <= 70 and 
                df['volume'].iloc[i] > 1.5 * df['volume_ma'].iloc[i]):
                breakout_price = df['close'].iloc[i]
                pullback_level = df['ma_20'].iloc[i+1]
                if (df['close'].iloc[i+1] < breakout_price and 
                    df['close'].iloc[i+2] > pullback_level):
                    entry_price = pullback_level
                    stop_loss = df['low'].iloc[i-5:i].min()
                    target = breakout_price + (breakout_price - stop_loss) * 2

                    risk_per_share = entry_price - stop_loss
                    if risk_per_share <= 0:
                        continue
                    shares = int(risk_per_trade // risk_per_share)
                    if shares <= 0:
                        continue

                    # Simulate trade over 24 hours (24 candles)
                    total_trades += 1
                    profit = 0
                    trailing_stop = stop_loss
                    highest_price = entry_price
                    max_holding_period = 24  # 24 hours

                    for j in range(i+3, min(i+3+max_holding_period, len(df))):
                        # Update trailing stop (1% below highest price)
                        highest_price = max(highest_price, df['high'].iloc[j])
                        trailing_stop = max(trailing_stop, highest_price * 0.99)

                        # Check intraday high/low
                        if df['high'].iloc[j] >= target:
                            profit = (target - entry_price) * shares
                            wins += 1
                            break
                        elif df['low'].iloc[j] <= trailing_stop:
                            profit = (trailing_stop - entry_price) * shares
                            if profit > 0:
                                wins += 1
                            else:
                                losses += 1
                            break
                        # Time-based exit at the end of holding period
                        elif j == i+3+max_holding_period-1:
                            profit = (df['close'].iloc[j] - entry_price) * shares
                            if profit > 0:
                                wins += 1
                            else:
                                losses += 1

                    capital += profit
                    logging.info(f"Trade {total_trades}: Entry={entry_price}, Stop={stop_loss}, Target={target}, Shares={shares}, Profit={profit}, Capital={capital}")

        # Calculate and print results
        win_rate = wins / total_trades if total_trades > 0 else 0
        return_percentage = ((capital - initial_capital) / initial_capital) * 100
        print(f"\nBacktest Results:")
        print(f"Total Trades: {total_trades}")
        print(f"Wins: {wins}, Losses: {losses}, Win Rate: {win_rate:.2%}")
        print(f"Final Capital: ${capital:.2f}")
        print(f"Return: {return_percentage:.2f}%")

    except Exception as e:
        print(f"Error occurred: {e}")
    
    finally:
        ib.disconnect()
        print("\nDisconnected from IBKR")

if __name__ == "__main__":
    asyncio.run(main())