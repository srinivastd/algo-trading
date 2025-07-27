"""
Mock Data Generator for Real-Time Market Data Testing

This module provides realistic mock data generation for testing trading strategies
during market off hours. It simulates real-time market data streams that match
the format expected by the IBKR trading system.
"""

import pandas as pd
import numpy as np
import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from decimal import Decimal
import pytz
from ib_insync import RealTimeBar

# Import existing historical data system
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from ibkr.historical_data import HistoricalDataManager
from ibkr.connection import IBKRConnection

logger = logging.getLogger(__name__)

@dataclass
class MockBarData:
    """Mock bar data structure matching RealTimeBar format"""
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    wap: float
    count: int

class MockDataGenerator:
    """
    Generates realistic mock market data for testing trading strategies.
    
    This class creates synthetic OHLCV data that mimics real market behavior
    including price movements, volume patterns, and market volatility.
    """
    
    def __init__(
        self,
        data_folder: str = "historical_data",
        timezone: str = "America/New_York",
        seed: Optional[int] = None
    ):
        """
        Initialize the mock data generator.
        
        Args:
            data_folder: Folder containing historical CSV files for reference
            timezone: Timezone for generated timestamps
            seed: Random seed for reproducible results
        """
        self.data_folder = data_folder
        self.timezone = pytz.timezone(timezone)
        self.seed = seed
        if seed is not None:
            np.random.seed(seed)
            
        # Cache for historical statistics
        self._stats_cache: Dict[str, Dict[str, float]] = {}
        
        logger.info(f"Initialized MockDataGenerator with data folder: {data_folder}")
    
    async def download_historical_data_from_ibkr(
        self,
        symbols: List[str],
        duration: str = "1 M",
        bar_size: str = "1 hour",
        ib_connection: Optional[IBKRConnection] = None
    ) -> Dict[str, bool]:
        """
        Download historical data from IBKR for specified symbols.
        
        Args:
            symbols: List of stock symbols to download data for
            duration: Duration of historical data (e.g., "1 M", "1 Y")
            bar_size: Bar size (e.g., "1 hour", "15 mins")
            ib_connection: IBKR connection instance (optional)
            
        Returns:
            Dict mapping symbol to success status
        """
        results = {}
        
        try:
            # Create IBKR connection if not provided
            if ib_connection is None:
                ib_connection = IBKRConnection()
                await ib_connection.connect()
                should_disconnect = True
            else:
                should_disconnect = False
            
            # Create historical data manager
            data_manager = HistoricalDataManager(ib_connection, self.data_folder)
            
            # Download data for each symbol
            for symbol in symbols:
                try:
                    logger.info(f"Downloading historical data for {symbol}...")
                    
                    # Get historical data
                    df = await data_manager.get_historical_data(
                        symbol=symbol,
                        duration=duration,
                        bar_size=bar_size,
                        force_refresh=True  # Always get fresh data
                    )
                    
                    if df is not None and not df.empty:
                        results[symbol] = True
                        logger.info(f"Successfully downloaded {len(df)} bars for {symbol}")
                        
                        # Save data with ticker prefix for reuse
                        self._save_historical_data_for_mock(symbol, df, duration, bar_size)
                        
                        # Calculate and cache statistics immediately
                        self._calculate_stats_from_dataframe(symbol, df)
                    else:
                        results[symbol] = False
                        logger.warning(f"No data downloaded for {symbol}")
                        
                except Exception as e:
                    results[symbol] = False
                    logger.error(f"Error downloading data for {symbol}: {e}")
            
            # Disconnect if we created the connection
            if should_disconnect:
                await ib_connection.disconnect()
                
        except Exception as e:
            logger.error(f"Error in download_historical_data_from_ibkr: {e}")
            results = {symbol: False for symbol in symbols}
        
        return results
    
    def _save_historical_data_for_mock(self, symbol: str, df: pd.DataFrame, duration: str, bar_size: str) -> str:
        """
        Save historical data with ticker prefix for later mock data generation.
        
        Args:
            symbol: Stock symbol
            df: DataFrame with historical data
            duration: Duration of the data
            bar_size: Bar size of the data
            
        Returns:
            Path to the saved file
        """
        try:
            # Create mock data directory if it doesn't exist
            mock_data_dir = os.path.join(self.data_folder, 'mock_seed_data')
            os.makedirs(mock_data_dir, exist_ok=True)
            
            # Create filename with ticker prefix and metadata
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{symbol.lower()}-{duration.replace(' ', '')}-{bar_size.replace(' ', '')}-{timestamp}.csv"
            filepath = os.path.join(mock_data_dir, filename)
            
            # Save the data
            df.to_csv(filepath)
            
            # Also save a latest version for easy access
            latest_filename = f"{symbol.lower()}-latest.csv"
            latest_filepath = os.path.join(mock_data_dir, latest_filename)
            df.to_csv(latest_filepath)
            
            logger.info(f"Saved {symbol} historical data for mock generation: {filepath}")
            logger.info(f"Saved {symbol} latest data: {latest_filepath}")
            
            return filepath
            
        except Exception as e:
            logger.error(f"Error saving historical data for {symbol}: {e}")
            return None
    
    def _calculate_stats_from_dataframe(self, symbol: str, df: pd.DataFrame) -> None:
        """
        Calculate statistics from a DataFrame and cache them.
        
        Args:
            symbol: Stock symbol
            df: DataFrame with OHLCV data
        """
        try:
            # Calculate comprehensive statistics
            stats = {
                'close_mean': float(df['close'].mean()),
                'close_std': float(df['close'].std()),
                'price_range_mean': float((df['high'] - df['low']).mean()),
                'price_range_std': float((df['high'] - df['low']).std()),
                'volume_mean': float(df['volume'].mean()),
                'volume_std': float(df['volume'].std()),
                'daily_volatility': float(df['close'].std() / np.sqrt(24)),  # ~24 bars per day
                'volume_volatility': float(df['volume'].std() / df['volume'].mean()),
                'price_momentum': float(df['close'].pct_change().mean()),
                'volume_momentum': float(df['volume'].pct_change().mean()),
                'high_low_ratio': float(((df['high'] - df['low']) / df['close']).mean()),
                'open_close_ratio': float(((df['close'] - df['open']) / df['open']).mean())
            }
            
            # Cache the statistics
            self._stats_cache[symbol] = stats
            logger.info(f"Calculated and cached statistics for {symbol}: close_mean=${stats['close_mean']:.2f}")
            
        except Exception as e:
            logger.error(f"Error calculating stats for {symbol}: {e}")
    
    def _load_historical_stats(self, symbol: str) -> Dict[str, float]:
        """
        Load and cache historical statistics for a symbol.
        
        Args:
            symbol: Stock symbol (e.g., 'PLTR')
            
        Returns:
            Dictionary containing price and volume statistics
        """
        if symbol in self._stats_cache:
            return self._stats_cache[symbol]
        
        # Aggregate historical data from all files for this symbol
        all_data = pd.DataFrame()
        
        try:
            # First, try to load from mock seed data (preferred)
            mock_seed_dir = os.path.join(self.data_folder, 'mock_seed_data')
            if os.path.exists(mock_seed_dir):
                # Look for latest version first
                latest_file = os.path.join(mock_seed_dir, f"{symbol.lower()}-latest.csv")
                if os.path.exists(latest_file):
                    logger.info(f"Loading {symbol} data from mock seed: {latest_file}")
                    df = pd.read_csv(latest_file)
                    df['date'] = pd.to_datetime(df['date'], utc=True).dt.tz_convert(self.timezone)
                    df.set_index('date', inplace=True)
                    all_data = df[['open', 'high', 'low', 'close', 'volume']]
                else:
                    # Look for any file with this ticker prefix
                    for filename in os.listdir(mock_seed_dir):
                        if filename.lower().startswith(f"{symbol.lower()}-") and filename.endswith(".csv"):
                            file_path = os.path.join(mock_seed_dir, filename)
                            logger.info(f"Loading {symbol} data from mock seed: {file_path}")
                            df = pd.read_csv(file_path)
                            df['date'] = pd.to_datetime(df['date'], utc=True).dt.tz_convert(self.timezone)
                            df.set_index('date', inplace=True)
                            all_data = df[['open', 'high', 'low', 'close', 'volume']]
                            break
            
            # If no mock seed data found, try original historical data folder
            if all_data.empty:
                for filename in os.listdir(self.data_folder):
                    if filename.lower().startswith(f"{symbol.lower()}-") and filename.endswith(".csv"):
                        file_path = os.path.join(self.data_folder, filename)
                        df = pd.read_csv(file_path)
                        df['date'] = pd.to_datetime(df['date'], utc=True).dt.tz_convert(self.timezone)
                        df.set_index('date', inplace=True)
                        all_data = pd.concat([all_data, df[['open', 'high', 'low', 'close', 'volume']]])
                        
        except Exception as e:
            logger.warning(f"Could not load historical data for {symbol}: {e}")
        
        if not all_data.empty:
            # Calculate comprehensive statistics
            stats = {
                'close_mean': float(all_data['close'].mean()),
                'close_std': float(all_data['close'].std()),
                'price_range_mean': float((all_data['high'] - all_data['low']).mean()),
                'price_range_std': float((all_data['high'] - all_data['low']).std()),
                'volume_mean': float(all_data['volume'].mean()),
                'volume_std': float(all_data['volume'].std()),
                'daily_volatility': float(all_data['close'].std() / np.sqrt(24)),  # ~24 bars per day
                'volume_volatility': float(all_data['volume'].std() / all_data['volume'].mean()),
                'price_momentum': float(all_data['close'].pct_change().mean()),
                'volume_momentum': float(all_data['volume'].pct_change().mean()),
                'high_low_ratio': float(((all_data['high'] - all_data['low']) / all_data['close']).mean()),
                'open_close_ratio': float(((all_data['close'] - all_data['open']) / all_data['open']).mean())
            }
            logger.info(f"Loaded {symbol} statistics from historical data: close_mean=${stats['close_mean']:.2f}")
        else:
            # Fallback statistics for symbols without historical data
            stats = {
                'close_mean': 100.0,
                'close_std': 5.0,
                'price_range_mean': 2.0,
                'price_range_std': 1.0,
                'volume_mean': 50000.0,
                'volume_std': 10000.0,
                'daily_volatility': 0.5,
                'volume_volatility': 0.2,
                'price_momentum': 0.001,
                'volume_momentum': 0.0,
                'high_low_ratio': 0.02,
                'open_close_ratio': 0.001
            }
            logger.warning(f"Using fallback statistics for {symbol} (no historical data found)")
        
        self._stats_cache[symbol] = stats
        logger.debug(f"Loaded statistics for {symbol}: {stats}")
        return stats
    
    def generate_mock_bars(
        self,
        symbol: str,
        start_time: datetime,
        bars: int,
        bar_size: str = "1 hour",
        volatility_multiplier: float = 1.0,
        volume_multiplier: float = 1.0,
        current_price: Optional[float] = None
    ) -> List[MockBarData]:
        """
        Generate realistic mock bar data.
        
        Args:
            symbol: Stock symbol
            start_time: Start timestamp for mock data
            bars: Number of bars to generate
            bar_size: Time interval for bars (e.g., '5 secs', '15 mins')
            volatility_multiplier: Multiplier for price volatility
            volume_multiplier: Multiplier for volume
            current_price: Override the starting price (useful when historical data unavailable)
            
        Returns:
            List of MockBarData objects
        """
        # Ensure timezone awareness
        if start_time.tzinfo is None:
            start_time = self.timezone.localize(start_time)
        
        # Load historical statistics
        stats = self._load_historical_stats(symbol)
        
        # Override with current price if provided
        if current_price is not None:
            stats['close_mean'] = current_price
            logger.info(f"Using provided current price for {symbol}: ${current_price:.2f}")
        
        # Calculate time intervals
        if bar_size == "5 secs":
            freq = "5s"
            bars_per_day = 4680  # 6.5 hours * 60 minutes * 60 seconds / 5 seconds
        elif bar_size == "15 mins":
            freq = "15T"
            bars_per_day = 26  # 6.5 hours * 4 bars per hour
        elif bar_size == "1 hour":
            freq = "1h"
            bars_per_day = 6.5
        else:
            freq = "5s"  # Default to 5 seconds
            bars_per_day = 4680
        
        # Adjust volatility for different timeframes
        timeframe_volatility = stats['daily_volatility'] / np.sqrt(bars_per_day)
        timeframe_volatility *= volatility_multiplier
        
        # Generate timestamps
        timestamps = pd.date_range(start=start_time, periods=bars, freq=freq, tz=self.timezone)
        
        # Initialize price series with random walk
        prices = np.zeros(bars)
        prices[0] = stats['close_mean'] + np.random.normal(0, timeframe_volatility)
        
        # Generate price movements with momentum and mean reversion
        for i in range(1, bars):
            prev_price = prices[i-1]
            
            # Add momentum component
            momentum = stats['price_momentum'] * timeframe_volatility
            
            # Add mean reversion component
            mean_reversion = (stats['close_mean'] - prev_price) * 0.01
            
            # Add random component
            random_component = np.random.normal(0, timeframe_volatility)
            
            # Combine components
            price_change = momentum + mean_reversion + random_component
            prices[i] = max(0.01, prev_price + price_change)
        
        # Generate OHLC data
        mock_bars = []
        for i, timestamp in enumerate(timestamps):
            close_price = prices[i]
            
            # Generate realistic OHLC relationships
            price_range = np.random.normal(stats['price_range_mean'], stats['price_range_std'])
            price_range = max(0.01, abs(price_range))
            
            # Determine if bar is bullish or bearish
            is_bullish = np.random.random() > 0.5
            
            if is_bullish:
                open_price = close_price - np.random.uniform(0, price_range * 0.4)
                high_price = close_price + np.random.uniform(0, price_range * 0.6)
                low_price = open_price - np.random.uniform(0, price_range * 0.4)
            else:
                open_price = close_price + np.random.uniform(0, price_range * 0.4)
                high_price = open_price + np.random.uniform(0, price_range * 0.4)
                low_price = close_price - np.random.uniform(0, price_range * 0.6)
            
            # Ensure logical OHLC relationships
            high_price = max(high_price, open_price, close_price)
            low_price = min(low_price, open_price, close_price)
            
            # Generate volume with realistic patterns
            base_volume = np.random.normal(stats['volume_mean'], stats['volume_std'])
            volume = max(0, base_volume * volume_multiplier)
            
            # Add volume spikes for significant price movements
            price_change_pct = abs(close_price - open_price) / open_price
            if price_change_pct > 0.02:  # 2% price change
                volume *= np.random.uniform(1.5, 3.0)
            
            # Calculate WAP (Weighted Average Price)
            wap = (open_price + high_price + low_price + close_price) / 4
            
            # Generate trade count (simplified)
            count = max(1, int(volume / 1000))
            
            mock_bar = MockBarData(
                time=timestamp,
                open=float(open_price),
                high=float(high_price),
                low=float(low_price),
                close=float(close_price),
                volume=float(volume),
                wap=float(wap),
                count=count
            )
            mock_bars.append(mock_bar)
        
        logger.debug(f"Generated {len(mock_bars)} mock bars for {symbol} starting at {start_time}")
        return mock_bars
    
    def generate_realtime_stream(
        self,
        symbol: str,
        start_time: datetime,
        duration_minutes: int = 60,
        bar_size: str = "5 secs",
        update_interval: float = 1.0,
        volatility_multiplier: float = 1.0,
        volume_multiplier: float = 1.0
    ) -> List[MockBarData]:
        """
        Generate a continuous stream of mock real-time bars.
        
        Args:
            symbol: Stock symbol
            start_time: Start timestamp
            duration_minutes: Duration of the stream in minutes
            bar_size: Time interval for bars
            update_interval: Seconds between bar updates
            volatility_multiplier: Multiplier for price volatility
            volume_multiplier: Multiplier for volume
            
        Returns:
            List of MockBarData objects representing the stream
        """
        # Calculate number of bars needed
        if bar_size == "5 secs":
            bars_needed = duration_minutes * 12  # 12 bars per minute
        elif bar_size == "15 mins":
            bars_needed = duration_minutes // 15
        elif bar_size == "1 hour":
            bars_needed = duration_minutes // 60
        else:
            bars_needed = duration_minutes * 12
        
        bars_needed = max(1, bars_needed)
        
        return self.generate_mock_bars(
            symbol=symbol,
            start_time=start_time,
            bars=bars_needed,
            bar_size=bar_size,
            volatility_multiplier=volatility_multiplier,
            volume_multiplier=volume_multiplier
        )
    
    def convert_to_realtime_bar(self, mock_bar: MockBarData) -> RealTimeBar:
        """
        Convert MockBarData to IBKR RealTimeBar format.
        
        Args:
            mock_bar: MockBarData object
            
        Returns:
            RealTimeBar object compatible with IBKR system
        """
        # Convert datetime to Unix timestamp
        unix_time = int(mock_bar.time.timestamp())
        
        return RealTimeBar(
            time=unix_time,
            endTime=unix_time + 5,  # 5-second bars
            open_=mock_bar.open,
            high=mock_bar.high,
            low=mock_bar.low,
            close=mock_bar.close,
            volume=Decimal(str(mock_bar.volume)),
            wap=Decimal(str(mock_bar.wap)),
            count=mock_bar.count
        )
    
    def create_mock_dataframe(self, mock_bars: List[MockBarData]) -> pd.DataFrame:
        """
        Convert list of MockBarData to pandas DataFrame.
        
        Args:
            mock_bars: List of MockBarData objects
            
        Returns:
            DataFrame with OHLCV data
        """
        data = []
        for bar in mock_bars:
            data.append({
                'date': bar.time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume
            })
        
        df = pd.DataFrame(data)
        df.set_index('date', inplace=True)
        return df
    
    async def simulate_realtime_feed(
        self,
        symbol: str,
        callback,
        start_time: Optional[datetime] = None,
        duration_minutes: int = 60,
        bar_size: str = "5 secs",
        update_interval: float = 1.0,
        volatility_multiplier: float = 1.0,
        volume_multiplier: float = 1.0
    ):
        """
        Simulate a real-time data feed by calling a callback function with mock bars.
        
        Args:
            symbol: Stock symbol
            callback: Async function to call with each new bar
            start_time: Start timestamp (defaults to now)
            duration_minutes: Duration of the simulation
            bar_size: Time interval for bars
            update_interval: Seconds between updates
            volatility_multiplier: Multiplier for price volatility
            volume_multiplier: Multiplier for volume
        """
        if start_time is None:
            start_time = datetime.now(self.timezone)
        
        # Generate all bars upfront
        mock_bars = self.generate_realtime_stream(
            symbol=symbol,
            start_time=start_time,
            duration_minutes=duration_minutes,
            bar_size=bar_size,
            volatility_multiplier=volatility_multiplier,
            volume_multiplier=volume_multiplier
        )
        
        logger.info(f"Starting real-time simulation for {symbol} with {len(mock_bars)} bars")
        
        # Simulate real-time delivery
        for i, mock_bar in enumerate(mock_bars):
            try:
                # Convert to RealTimeBar format
                realtime_bar = self.convert_to_realtime_bar(mock_bar)
                
                # Call the callback function
                await callback(realtime_bar, symbol)
                
                # Log progress
                if (i + 1) % 10 == 0:
                    logger.debug(f"Delivered {i + 1}/{len(mock_bars)} bars for {symbol}")
                
                # Wait for next update
                await asyncio.sleep(update_interval)
                
            except Exception as e:
                logger.error(f"Error in real-time simulation: {e}")
                break
        
        logger.info(f"Completed real-time simulation for {symbol}")

    def list_available_mock_seed_data(self) -> Dict[str, List[str]]:
        """
        List all available mock seed data files organized by symbol.
        
        Returns:
            Dict mapping symbol to list of available files
        """
        available_data = {}
        
        try:
            mock_seed_dir = os.path.join(self.data_folder, 'mock_seed_data')
            if os.path.exists(mock_seed_dir):
                for filename in os.listdir(mock_seed_dir):
                    if filename.endswith('.csv'):
                        # Extract symbol from filename (e.g., "pltr-1m-4hours-20250127.csv" -> "PLTR")
                        parts = filename.split('-')
                        if len(parts) >= 1:
                            symbol = parts[0].upper()
                            if symbol not in available_data:
                                available_data[symbol] = []
                            available_data[symbol].append(filename)
                            
                            # Sort files by timestamp (newest first)
                            available_data[symbol].sort(reverse=True)
            
            if available_data:
                logger.info(f"Found mock seed data for {len(available_data)} symbols")
                for symbol, files in available_data.items():
                    logger.info(f"  {symbol}: {len(files)} files available")
            else:
                logger.info("No mock seed data found")
                
        except Exception as e:
            logger.error(f"Error listing mock seed data: {e}")
        
        return available_data


# Example usage and testing functions
def create_test_mock_data():
    """Create sample mock data for testing"""
    generator = MockDataGenerator()
    
    # Generate 20 bars starting from now
    start_time = datetime.now(pytz.timezone('America/New_York'))
    mock_bars = generator.generate_mock_bars(
        symbol="PLTR",
        start_time=start_time,
        bars=20,
        bar_size="5 secs"
    )
    
    # Convert to DataFrame
    df = generator.create_mock_dataframe(mock_bars)
    
    print("Generated Mock Data:")
    print(df.head())
    print(f"\nData shape: {df.shape}")
    print(f"Price range: ${df['low'].min():.2f} - ${df['high'].max():.2f}")
    print(f"Total volume: {df['volume'].sum():,.0f}")
    
    return df


async def test_realtime_simulation():
    """Test the real-time simulation functionality"""
    generator = MockDataGenerator()
    
    async def mock_callback(bar, symbol):
        print(f"{symbol}: {bar.time} - Close: ${bar.close:.2f}, Volume: {bar.volume}")
    
    await generator.simulate_realtime_feed("TSLA", mock_callback, duration_minutes=60)


if __name__ == "__main__":
    # Test the mock data generator
    print("Testing Mock Data Generator...")
    df = create_test_mock_data()
    
    # Test real-time simulation
    print("\nTesting Real-time Simulation...")
    asyncio.run(test_realtime_simulation()) 