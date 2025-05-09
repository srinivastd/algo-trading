"""
Stock Analysis Script using IBKR API
This script connects to IBKR, retrieves historical data for a specified stock,
and prepares it for candlestick analysis.
"""
import asyncio
import logging
from ib_insync import *
import pandas as pd
from ta.momentum import RSIIndicator
import argparse
from datetime import datetime, timedelta
import pytz
import time
import os
from typing import Dict, List, Optional, Tuple

# Set up logging
logging.basicConfig(
    filename='stock_analysis.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class StrategyBase:
    """Base class for all trading strategies"""
    def __init__(self, name: str):
        self.name = name
        self.indicators = {}
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate strategy-specific indicators"""
        raise NotImplementedError
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate trading signals"""
        raise NotImplementedError
        
    def backtest(self, df: pd.DataFrame, initial_capital: float = 100000) -> Dict:
        """Run backtest on historical data"""
        raise NotImplementedError

class BreakoutPullbackStrategy(StrategyBase):
    """Breakout Pullback Strategy with Candlestick Confirmation"""
    
    def __init__(self):
        super().__init__("Breakout Pullback Strategy")
        self.risk_per_trade = 1000  # 1% of $100,000
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate technical indicators"""
        df['rsi'] = RSIIndicator(df['close']).rsi()
        df['ma_20'] = df['close'].rolling(window=20).mean()
        df['ma_50'] = df['close'].rolling(window=50).mean()
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        return df
        
    def is_bullish_engulfing(self, df: pd.DataFrame, i: int) -> bool:
        """Check for bullish engulfing pattern"""
        if i > 0 and df['open'].iloc[i] < df['close'].iloc[i-1] and df['close'].iloc[i] > df['open'].iloc[i-1]:
            return df['close'].iloc[i] > df['open'].iloc[i]
        return False
        
    def is_hammer(self, df: pd.DataFrame, i: int) -> bool:
        """Check for hammer pattern"""
        body = abs(df['close'].iloc[i] - df['open'].iloc[i])
        lower_wick = df['low'].iloc[i] - min(df['open'].iloc[i], df['close'].iloc[i])
        upper_wick = max(df['open'].iloc[i], df['close'].iloc[i]) - df['high'].iloc[i]
        return body > 0 and lower_wick > 2 * body and upper_wick < body / 2
        
    def is_consolidation(self, df: pd.DataFrame, start: int, end: int) -> bool:
        """Check for consolidation pattern"""
        range_high = df['high'][start:end].max()
        range_low = df['low'][start:end].min()
        return (range_high - range_low) / df['close'].iloc[start] < 0.02
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate trading signals"""
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0  # 0: no signal, 1: buy, -1: sell
        
        for i in range(5, len(df)-1):
            if (self.is_consolidation(df, i-5, i) and 
                df['close'].iloc[i] > df['high'].iloc[i-5:i].max() and 
                df['close'].iloc[i] > df['ma_50'].iloc[i] and 
                (self.is_bullish_engulfing(df, i) or self.is_hammer(df, i)) and 
                30 <= df['rsi'].iloc[i] <= 70 and 
                df['volume'].iloc[i] > 1.5 * df['volume_ma'].iloc[i]):
                
                signals.iloc[i]['signal'] = 1
                
        return signals
        
    def backtest(self, df: pd.DataFrame, initial_capital: float = 100000) -> Dict:
        """Run backtest on historical data"""
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []
        
        for i in range(len(df)):
            if signals['signal'].iloc[i] == 1 and position == 0:
                # Entry
                entry_price = df['close'].iloc[i]
                stop_loss = df['low'].iloc[i-5:i].min()
                target = entry_price + (entry_price - stop_loss) * 2
                
                risk_per_share = entry_price - stop_loss
                if risk_per_share <= 0:
                    continue
                    
                shares = int(self.risk_per_trade // risk_per_share)
                if shares <= 0:
                    continue
                    
                position = shares
                trades.append({
                    'entry_date': df.index[i],
                    'entry_price': entry_price,
                    'shares': shares,
                    'stop_loss': stop_loss,
                    'target': target
                })
                
            elif position > 0:
                # Check exit conditions
                current_trade = trades[-1]
                if (df['high'].iloc[i] >= current_trade['target'] or 
                    df['low'].iloc[i] <= current_trade['stop_loss']):
                    exit_price = (current_trade['target'] if df['high'].iloc[i] >= current_trade['target']
                                else current_trade['stop_loss'])
                    profit = (exit_price - current_trade['entry_price']) * current_trade['shares']
                    capital += profit
                    position = 0
                    trades[-1].update({
                        'exit_date': df.index[i],
                        'exit_price': exit_price,
                        'profit': profit
                    })
        
        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': len(trades),
            'trades': trades
        }

def save_historical_data(df: pd.DataFrame, symbol: str, duration: str, bar_size: str) -> str:
    """
    Save historical data to CSV with a descriptive filename.
    
    Args:
        df: DataFrame containing historical data
        symbol: Stock symbol
        duration: Duration of historical data
        bar_size: Size of each bar
    
    Returns:
        str: Path to the saved file
    """
    try:
        # Create data directory if it doesn't exist
        data_dir = 'historical_data'
        os.makedirs(data_dir, exist_ok=True)
        
        # Format the filename
        current_date = datetime.now().strftime('%Y%m%d')
        filename = f"{symbol}-{duration.replace(' ', '')}-{bar_size.replace(' ', '')}-{current_date}.csv"
        filepath = os.path.join(data_dir, filename)
        
        # Save to CSV with all relevant information
        df.to_csv(filepath)
        
        # Log the save
        logging.info(f"Saved historical data to {filepath}")
        print(f"\nData saved to {filepath}")
        
        return filepath
        
    except Exception as e:
        error_msg = f"Error saving historical data: {str(e)}"
        logging.error(error_msg)
        print(error_msg)
        return None

async def close_existing_connection(ib: IB) -> None:
    """
    Attempt to close any existing IB connection.
    """
    try:
        if ib.isConnected():
            print("Closing existing IB connection...")
            await ib.disconnect()
            # Wait briefly to ensure disconnection
            await asyncio.sleep(1)
            print("Existing connection closed.")
    except Exception as e:
        print(f"Error closing existing connection: {str(e)}")
        logging.error(f"Error closing existing connection: {str(e)}")

async def connect_to_ibkr(ib: IB, max_retries: int = 3) -> bool:
    """
    Connect to IBKR with retry logic
    """
    # First, try to close any existing connections
    await close_existing_connection(ib)
    
    for attempt in range(max_retries):
        try:
            if not ib.isConnected():
                print(f"Connection attempt {attempt + 1}/{max_retries}")
                await ib.connectAsync('127.0.0.1', 4002, clientId=1)
                if ib.isConnected():
                    print("Successfully connected to IBKR")
                    # Wait briefly to ensure connection is stable
                    await asyncio.sleep(1)
                    return True
            else:
                return True
        except Exception as e:
            print(f"Connection attempt {attempt + 1} failed: {str(e)}")
            logging.error(f"Connection attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2)  # Wait before retrying
    return False

async def get_historical_data(ib: IB, symbol: str, duration: str = '1 M', bar_size: str = '1 hour'):
    """
    Retrieve historical data for the specified stock.
    """
    try:
        print(f"Defining contract for {symbol}...")
        # Define stock contract
        contract = Stock(symbol, 'SMART', 'USD')
        
        # Qualify the contract
        qualified_contracts = await ib.qualifyContractsAsync(contract)
        if not qualified_contracts:
            print(f"Could not qualify contract for {symbol}")
            return None
            
        contract = qualified_contracts[0]
        print(f"Contract qualified: {contract}")
        
        print(f"Requesting historical data for {symbol}...")
        # Request historical data
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime='',
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=True,
            keepUpToDate=False
        )
        
        if not bars:
            print(f"No historical data received for {symbol}")
            logging.error(f"No historical data received for {symbol}")
            return None
            
        print(f"Received {len(bars)} bars of historical data")
        
        # Convert to DataFrame
        df = pd.DataFrame(bars)
        
        # Handle date conversion properly
        if isinstance(df['date'].iloc[0], datetime):
            df['date'] = df['date'].dt.tz_localize('UTC')
        else:
            # If it's a date object, convert to datetime first
            df['date'] = pd.to_datetime(df['date']).dt.tz_localize('UTC')
            
        df = df.set_index('date')
        
        # Calculate technical indicators
        print("Calculating technical indicators...")
        df['rsi'] = RSIIndicator(df['close']).rsi()
        df['ma_20'] = df['close'].rolling(window=20).mean()
        df['ma_50'] = df['close'].rolling(window=50).mean()
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        
        return df
        
    except Exception as e:
        print(f"Error getting historical data: {str(e)}")
        logging.error(f"Error getting historical data for {symbol}: {str(e)}")
        return None

async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Stock Analysis with IBKR')
    parser.add_argument('symbol', type=str, help='Stock symbol (e.g., NVDA)')
    parser.add_argument('--duration', type=str, default='1 M', help='Duration of historical data (e.g., 1 M, 1 Y)')
    parser.add_argument('--bar-size', type=str, default='1 hour', help='Bar size (e.g., 1 hour, 1 day)')
    parser.add_argument('--strategy', type=str, default='breakout', help='Strategy to use (e.g., breakout)')
    args = parser.parse_args()
    
    # Create IB connection instance
    ib = IB()
    
    try:
        print("Connecting to IBKR Gateway...")
        # Connect with retry logic
        if not await connect_to_ibkr(ib):
            print("Failed to connect to IBKR Gateway after multiple attempts")
            return
        
        # Get account information
        accounts = ib.managedAccounts()
        print("\nAvailable accounts:")
        for acc in accounts:
            print(f"- {acc}")
        
        # Get historical data
        print(f"\nFetching historical data for {args.symbol}...")
        df = await get_historical_data(ib, args.symbol, args.duration, args.bar_size)
        
        if df is not None and not df.empty:
            print(f"\nSuccessfully retrieved {len(df)} bars of historical data")
            print("\nLatest data points:")
            print(df.tail())
            
            # Initialize and run strategy
            if args.strategy == 'breakout':
                strategy = BreakoutPullbackStrategy()
                df = strategy.calculate_indicators(df)
                results = strategy.backtest(df)
                
                print("\nBacktest Results:")
                print(f"Initial Capital: ${results['initial_capital']:,.2f}")
                print(f"Final Capital: ${results['final_capital']:,.2f}")
                print(f"Return: {results['return_pct']:.2f}%")
                print(f"Total Trades: {results['total_trades']}")
                
                # Save data to CSV with descriptive filename
                filepath = save_historical_data(df, args.symbol, args.duration, args.bar_size)
                
                if filepath:
                    # Basic statistics
                    print("\nBasic Statistics:")
                    print(f"Date Range: {df.index[0]} to {df.index[-1]}")
                    print(f"Number of bars: {len(df)}")
                    print(f"Current price: ${df['close'].iloc[-1]:.2f}")
                    print(f"RSI: {df['rsi'].iloc[-1]:.2f}")
                    print(f"20-day MA: ${df['ma_20'].iloc[-1]:.2f}")
                    print(f"50-day MA: ${df['ma_50'].iloc[-1]:.2f}")
        else:
            print("No data was retrieved. Please check the logs for details.")
        
        # Keep the connection alive briefly to ensure all data is processed
        await asyncio.sleep(3)
        
    except Exception as e:
        print(f"Error occurred: {e}")
        logging.error(f"Error in main: {str(e)}")
    
    finally:
        # Disconnect
        await close_existing_connection(ib)
        print("\nDisconnected from IBKR")

if __name__ == "__main__":
    asyncio.run(main()) 