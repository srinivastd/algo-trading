#!/usr/bin/env python3
"""
Simple Mock Strategy Runner

This script follows the exact same flow as the actual strategy in main.py,
but swaps out real market data with mock data. This provides a much more
intuitive approach for testing strategies.
"""

import asyncio
import logging
import sys
import os
from datetime import datetime, timedelta
import pytz
import pandas as pd
import json
import time

# Add the src directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
from utils.mock_data_generator import MockDataGenerator

# Set up logging
logging.basicConfig(
    level=logging.WARNING,  # Reduce verbosity from INFO to WARNING
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create a separate logger for mock data with file output
mock_logger = logging.getLogger('mock_data')
mock_logger.setLevel(logging.INFO)

# Create mock_data_logs directory at the same level as market_data_logs
mock_logs_dir = os.path.join(os.path.dirname(__file__), '..', 'mock_data_logs')
os.makedirs(mock_logs_dir, exist_ok=True)

# Create timestamped filename for mock data logs
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
mock_log_filename = os.path.join(mock_logs_dir, f"mock_data_log_{timestamp}.txt")

# Create file handler for mock data logs
mock_file_handler = logging.FileHandler(mock_log_filename)
mock_file_handler.setFormatter(logging.Formatter('%(asctime)s - MOCK DATA - %(message)s'))
mock_logger.addHandler(mock_file_handler)
mock_logger.propagate = False  # Prevent duplicate logging

# Log the start of mock data logging
mock_logger.info(f"Mock data logging started - File: {mock_log_filename}")

class SimpleMockStrategyRunner:
    """
    Simple mock strategy runner that follows the exact same flow as main.py
    but uses mock data instead of real market data.
    """
    
    def __init__(self, symbols, strategy_name="groq_recommendations", check_interval=5, verbose_mock_data=True, 
                 volatility_multiplier=2.5, volume_multiplier=3.5, mock_log_to_file=True, 
                 download_historical_data=False, historical_duration="1 M", historical_bar_size="4 hours"):
        """
        Initialize the mock strategy runner.
        
        Args:
            symbols: List of stock symbols to monitor
            strategy_name: Name of the strategy to use
            check_interval: Seconds between checks
            verbose_mock_data: Whether to show detailed mock data logs
            volatility_multiplier: Multiplier for price volatility to generate stronger trends
            volume_multiplier: Multiplier for volume to generate volume spikes
            mock_log_to_file: Whether to save mock data logs to file (True) or console (False)
        """
        self.symbols = [symbol.upper() for symbol in symbols]
        self.strategy_name = strategy_name
        self.check_interval = check_interval
        self.verbose_mock_data = verbose_mock_data
        self.volatility_multiplier = volatility_multiplier
        self.volume_multiplier = volume_multiplier
        self.mock_log_to_file = mock_log_to_file
        self.download_historical_data = download_historical_data
        self.historical_duration = historical_duration
        self.historical_bar_size = historical_bar_size
        
        # Initialize strategy (same as main.py) - Use current entry/exit criteria before git stash
        if strategy_name == "groq_recommendations":
            self.strategy = GroqRecommendationsStrategy(
                backtest_mode=True,
                # Current criteria (before git stash) - More aggressive for testing
                lookback=3,                     # Current: 3 bars minimum (increased from 2)
                adx_threshold=15.0,             # Current: ADX > 15 (increased from 5)
                volume_increase_pct=1.2,        # Current: Volume 1.2x previous bar (20% increase)
                time_window_hours=24.0,         # Current: 24 hour window
                trend_check=False               # Current: No trend requirements
            )
        else:
            raise ValueError(f"Strategy {strategy_name} not supported")
        
        # Current entry/exit criteria being mimicked (before git stash):
        # - Zone identification: 3-period lookback, ADX > 15
        # - Entry signals: 24h window, price breakout + 20% volume increase
        # - Exit signals: Zone reversal + momentum + support/resistance breaks
        # - No trend requirements, moderately aggressive signal generation
        
        # Initialize mock data generator
        self.mock_generator = MockDataGenerator()
        
        # Initialize data storage (same as main.py)
        self.data_dict = {}
        for symbol in self.symbols:
            # Create empty DataFrame with proper structure
            self.data_dict[symbol] = pd.DataFrame(
                columns=['open', 'high', 'low', 'close', 'volume'],
                dtype=float
            )
        
        # Track signals and trades
        self.signals = []
        self.trades = []
        
        # Configure mock data logging
        if not self.verbose_mock_data:
            mock_logger.setLevel(logging.WARNING)
        
        logger.info(f"Initialized SimpleMockStrategyRunner for {self.symbols} with {strategy_name}")
        logger.info(f"Mock data settings: volatility_multiplier={volatility_multiplier}, volume_multiplier={volume_multiplier}")
        
        if self.download_historical_data:
            print(f"📥 Historical data download enabled: {historical_duration} {historical_bar_size} bars")
        else:
            print("📊 Using fallback statistics for mock data generation")
        
        # Configure mock data output (file vs console)
        if self.mock_log_to_file:
            print(f"📁 Mock data logs will be saved to: {mock_log_filename}")
            print(f"📂 Mock data logs folder: {mock_logs_dir}")
        else:
            # Add console handler for mock data
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(asctime)s - MOCK DATA - %(message)s'))
            mock_logger.addHandler(console_handler)
            print("📺 Mock data logs will be displayed in console")
    
    async def run_mock_strategy(self, duration_minutes=30):
        """
        Run the mock strategy following the exact same flow as main.py.
        
        Args:
            duration_minutes: How long to run the strategy
        """
        logger.info(f"Starting mock strategy run for {duration_minutes} minutes")
        logger.info(f"Symbols: {self.symbols}")
        logger.info(f"Strategy: {self.strategy_name}")
        logger.info(f"Check interval: {self.check_interval} seconds")
        
        start_time = datetime.now(pytz.timezone('America/New_York'))
        end_time = start_time + timedelta(minutes=duration_minutes)
        
        print("\n" + "="*80)
        print("🚀 SIMPLE MOCK STRATEGY RUN")
        print("="*80)
        print(f"Strategy: {self.strategy_name}")
        print(f"Symbols: {', '.join(self.symbols)}")
        print(f"Duration: {duration_minutes} minutes")
        print(f"Check interval: {self.check_interval} seconds")
        print(f"Start time: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print("="*80)
        
        cycle_count = 0
        
        try:
            # Download historical data if enabled
            if self.download_historical_data:
                print(f"\n📥 Downloading historical data from IBKR...")
                print(f"Symbols: {', '.join(self.symbols)}")
                print(f"Duration: {self.historical_duration}")
                print(f"Bar Size: {self.historical_bar_size}")
                
                try:
                    download_results = await self.mock_generator.download_historical_data_from_ibkr(
                        symbols=self.symbols,
                        duration=self.historical_duration,
                        bar_size=self.historical_bar_size
                    )
                    
                    successful_downloads = [symbol for symbol, success in download_results.items() if success]
                    failed_downloads = [symbol for symbol, success in download_results.items() if not success]
                    
                    if successful_downloads:
                        print(f"✅ Successfully downloaded data for: {', '.join(successful_downloads)}")
                    if failed_downloads:
                        print(f"❌ Failed to download data for: {', '.join(failed_downloads)}")
                        print("   Using fallback statistics for failed symbols")
                    
                    # Show available mock seed data
                    print(f"\n📁 Mock seed data saved to: {os.path.join(self.mock_generator.data_folder, 'mock_seed_data')}")
                    available_data = self.mock_generator.list_available_mock_seed_data()
                    if available_data:
                        print("📊 Available mock seed data:")
                        for symbol, files in available_data.items():
                            if files:
                                latest_file = files[0]  # Most recent file
                                print(f"   {symbol}: {latest_file}")
                    
                except Exception as e:
                    print(f"⚠️  Error downloading historical data: {e}")
                    print("   Using fallback statistics for all symbols")
            
            while datetime.now(pytz.timezone('America/New_York')) < end_time:
                cycle_count += 1
                current_time = datetime.now(pytz.timezone('America/New_York'))
                
                # STEP 1: Generate mock market data (replaces real market data from IB)
                await self._generate_mock_data(current_time)
                
                # STEP 2: Check existing positions for exit signals (same as main.py)
                await self._check_existing_positions(current_time)
                
                # STEP 3: Generate new signals (same as main.py)
                await self._generate_new_signals(current_time)
                
                # STEP 4: Execute trades (same as main.py)
                await self._execute_trades(current_time)
                
                # Wait for next cycle
                await asyncio.sleep(self.check_interval)
                
                # Progress update every 10 cycles
                if cycle_count % 10 == 0:
                    elapsed = (current_time - start_time).total_seconds() / 60
                    remaining = duration_minutes - elapsed
                    print(f"⏱️  Progress: {elapsed:.1f}/{duration_minutes} minutes ({remaining:.1f} remaining)")
        
        except KeyboardInterrupt:
            logger.info("Mock strategy run stopped by user")
        
        finally:
            await self._print_summary(start_time, cycle_count)
    
    async def _generate_mock_data(self, current_time):
        """Generate mock market data for all symbols (replaces real market data)."""
        for symbol in self.symbols:
            try:
                # Generate mock bar data
                mock_bars = self.mock_generator.generate_mock_bars(
                    symbol=symbol,
                    bars=1,  # One bar per cycle
                    start_time=current_time,
                    volatility_multiplier=self.volatility_multiplier,
                    volume_multiplier=self.volume_multiplier
                )
                
                if mock_bars and len(mock_bars) > 0:
                    bar = mock_bars[0]
                    
                    # Convert to DataFrame format (same as main.py)
                    new_data = pd.DataFrame({
                        'open': [float(bar.open)],
                        'high': [float(bar.high)],
                        'low': [float(bar.low)],
                        'close': [float(bar.close)],
                        'volume': [float(bar.volume)]
                    }, index=[bar.time])
                    
                    # Ensure both DataFrames have the same columns and dtypes
                    if self.data_dict[symbol].empty:
                        # If empty, just assign the new data
                        self.data_dict[symbol] = new_data
                    else:
                        # Ensure same column order and dtypes
                        expected_columns = ['open', 'high', 'low', 'close', 'volume']
                        self.data_dict[symbol] = self.data_dict[symbol][expected_columns]
                        new_data = new_data[expected_columns]
                        
                        # Ensure dtypes match
                        for col in expected_columns:
                            new_data[col] = new_data[col].astype(float)
                        
                        # Concatenate and clean up
                        self.data_dict[symbol] = pd.concat(
                            [self.data_dict[symbol], new_data], 
                            ignore_index=False
                        ).drop_duplicates().sort_index()
                    
                    # Keep only last 1000 bars to prevent memory issues
                    if len(self.data_dict[symbol]) > 1000:
                        self.data_dict[symbol] = self.data_dict[symbol].tail(1000)
                    
                    # Enhanced mock tick data logging
                    mock_logger.info(
                        f"📊 {symbol} TICK: "
                        f"O:${bar.open:.2f} H:${bar.high:.2f} L:${bar.low:.2f} "
                        f"C:${bar.close:.2f} V:{bar.volume:,.0f} "
                        f"WAP:${bar.wap:.2f} Count:{bar.count} "
                        f"Time:{bar.time.strftime('%H:%M:%S')}"
                    )
                    
                    # Calculate price change and volume analysis
                    if len(self.data_dict[symbol]) > 1:
                        prev_close = self.data_dict[symbol]['close'].iloc[-2]
                        price_change = bar.close - prev_close
                        price_change_pct = (price_change / prev_close) * 100
                        volume_ma = self.data_dict[symbol]['volume'].rolling(5).mean().iloc[-1]
                        volume_ratio = bar.volume / volume_ma if volume_ma > 0 else 1.0
                        
                        # Log additional analysis
                        mock_logger.info(
                            f"📈 {symbol} ANALYSIS: "
                            f"Δ:${price_change:+.2f} ({price_change_pct:+.2f}%) "
                            f"VolRatio:{volume_ratio:.1f}x "
                            f"Bars:{len(self.data_dict[symbol])}"
                        )
                        
                        # Validation: Check if data looks realistic
                        if len(self.data_dict[symbol]) >= 20:  # After we have enough data
                            self._validate_mock_data(symbol)
                
            except Exception as e:
                logger.error(f"Error generating mock data for {symbol}: {e}")
    
    async def _check_existing_positions(self, current_time):
        """Check existing positions for exit signals (same as main.py)."""
        # In mock mode, we don't have real positions, but we can simulate
        # This follows the exact same logic as main.py
        for symbol in self.symbols:
            if symbol in self.data_dict and len(self.data_dict[symbol]) >= 2:
                df = self.strategy.calculate_indicators(self.data_dict[symbol].copy())
                
                # Generate exit signal (same as main.py)
                exit_signal = await self.strategy.generate_exit_signals(df, 'long')  # Assume long for mock
                
                if exit_signal != 0:
                    logger.debug(f"Exit signal {exit_signal} generated for {symbol}")
                    
                    # Log exit signal
                    exit_data = {
                        'timestamp': current_time.isoformat(),
                        'symbol': symbol,
                        'action': 'exit_signal',
                        'signal': int(exit_signal),
                        'current_price': float(df['close'].iloc[-1])
                    }
                    self.signals.append(exit_data)
    
    async def _generate_new_signals(self, current_time):
        """Generate new trading signals (same as main.py)."""
        for symbol in self.symbols:
            if symbol in self.data_dict and len(self.data_dict[symbol]) >= 2:
                try:
                    # Calculate indicators (same as main.py)
                    df = self.strategy.calculate_indicators(self.data_dict[symbol].copy())
                    
                    # Generate real-time signal (same as main.py)
                    signal = await self.strategy.generate_realtime_signals(df)
                    
                    if signal != 0:
                        logger.info(f"🎯 SIGNAL: {symbol} - {'BUY' if signal == 1 else 'SELL'} at ${df['close'].iloc[-1]:.2f}")
                        
                        # Log signal (same format as main.py)
                        signal_data = {
                            'timestamp': current_time.isoformat(),
                            'symbol': symbol,
                            'signal': int(signal),
                            'current_price': float(df['close'].iloc[-1]),
                            'current_volume': float(df['volume'].iloc[-1]),
                            'bars_analyzed': len(df),
                            'strategy': self.strategy_name
                        }
                        self.signals.append(signal_data)
                        
                        # Print signal details
                        print("\n" + "="*60)
                        print("🎯 GROQ STRATEGY SIGNAL DETECTED")
                        print("="*60)
                        print(f"Symbol: {symbol}")
                        print(f"Signal: {'BUY' if signal == 1 else 'SELL'}")
                        print(f"Price: ${df['close'].iloc[-1]:.2f}")
                        print(f"Volume: {df['volume'].iloc[-1]:,.0f}")
                        print(f"Strategy: {self.strategy_name}")
                        print(f"Time: {current_time.isoformat()}")
                        print("="*60)
                
                except Exception as e:
                    logger.error(f"Error generating signals for {symbol}: {e}")
    
    async def _execute_trades(self, current_time):
        """Execute trades (same as main.py)."""
        # In mock mode, we simulate trade execution
        # This follows the exact same logic as main.py
        for signal_data in self.signals:
            if signal_data.get('action') != 'exit_signal':  # Skip exit signals
                symbol = signal_data['symbol']
                signal = signal_data['signal']
                entry_price = signal_data['current_price']
                
                # Calculate trade parameters (same as main.py)
                is_long = signal == 1
                stop_loss = entry_price * (1 - self.strategy.stop_loss_pct/100) if is_long else entry_price * (1 + self.strategy.stop_loss_pct/100)
                target = entry_price * (1 + self.strategy.target_pct/100) if is_long else entry_price * (1 - self.strategy.target_pct/100)
                
                risk_per_share = abs(entry_price - stop_loss)
                shares = int(self.strategy.risk_per_trade / risk_per_share) if risk_per_share > 0 else 100
                if shares <= 0:
                    shares = 100
                
                # Simulate trade execution
                trade_result = {
                    'status': 'success',
                    'order_id': f'mock_{len(self.trades) + 1}',
                    'entry_time': current_time,
                    'symbol': symbol,
                    'action': 'BUY' if signal == 1 else 'SELL',
                    'shares': shares,
                    'entry_price': entry_price,
                    'stop_loss': stop_loss,
                    'target': target
                }
                
                self.trades.append(trade_result)
                logger.debug(f"Mock trade executed: {trade_result['action']} {shares} shares of {symbol} at ${entry_price:.2f}")
                
                # Remove processed signal
                self.signals.remove(signal_data)
    
    async def _print_summary(self, start_time, cycle_count):
        """Print summary of the mock strategy run."""
        end_time = datetime.now(pytz.timezone('America/New_York'))
        duration = (end_time - start_time).total_seconds() / 60
        
        print("\n" + "="*80)
        print("📊 SIMPLE MOCK STRATEGY SUMMARY")
        print("="*80)
        print(f"Strategy: {self.strategy_name}")
        print(f"Symbols Monitored: {', '.join(self.symbols)}")
        print(f"Start Time: {start_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"End Time: {end_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"Duration: {duration:.1f} minutes")
        print(f"Analysis Cycles: {cycle_count}")
        print(f"Total Signals Generated: {len(self.signals) + len([t for t in self.trades if t.get('action') != 'exit_signal'])}")
        print(f"Total Trades Executed: {len(self.trades)}")
        
        # Signal breakdown
        buy_signals = len([s for s in self.signals if s.get('signal') == 1])
        sell_signals = len([s for s in self.signals if s.get('signal') == -1])
        print(f"\nSignal Breakdown:")
        print(f"  Buy Signals: {buy_signals}")
        print(f"  Sell Signals: {sell_signals}")
        
        # Recent signals
        if self.signals:
            print(f"\nRecent Signals:")
            for signal in self.signals[-5:]:  # Last 5 signals
                timestamp = signal['timestamp'][:19]  # Remove timezone info for display
                print(f"  {timestamp} - {signal['symbol']}: {'BUY' if signal['signal'] == 1 else 'SELL'} at ${signal['current_price']:.2f}")
        
        print("="*80)
        
        # Add mock data validation summary
        print("\n" + "="*80)
        print("🔍 MOCK DATA VALIDATION SUMMARY")
        print("="*80)
        for symbol in self.symbols:
            if symbol in self.data_dict and len(self.data_dict[symbol]) > 0:
                df = self.data_dict[symbol]
                strategy_df = self.strategy.calculate_indicators(df.copy())
                
                # Calculate statistics
                avg_price = df['close'].mean()
                price_range = df['high'].max() - df['low'].min()
                avg_volume = df['volume'].mean()
                volume_std = df['volume'].std()
                avg_adx = strategy_df['adx'].mean() if 'adx' in strategy_df.columns else 0
                max_adx = strategy_df['adx'].max() if 'adx' in strategy_df.columns else 0
                
                # Check strategy requirements
                volume_requirement_met = (df['volume'] / df['volume'].rolling(20).mean() >= self.strategy.volume_increase_pct).sum()
                adx_requirement_met = (strategy_df['adx'] >= self.strategy.adx_threshold).sum() if 'adx' in strategy_df.columns else 0
                
                print(f"\n{symbol} Data Quality:")
                print(f"  Bars Generated: {len(df)}")
                print(f"  Avg Price: ${avg_price:.2f}")
                print(f"  Price Range: ${price_range:.2f}")
                print(f"  Avg Volume: {avg_volume:,.0f}")
                print(f"  Volume Std: {volume_std:,.0f}")
                print(f"  Avg ADX: {avg_adx:.1f}")
                print(f"  Max ADX: {max_adx:.1f}")
                print(f"  Volume Requirements Met: {volume_requirement_met}/{len(df)} bars")
                print(f"  ADX Requirements Met: {adx_requirement_met}/{len(df)} bars")
                
                if volume_requirement_met == 0:
                    print(f"  ⚠️  No volume spikes detected - consider increasing volume_multiplier")
                if adx_requirement_met == 0:
                    print(f"  ⚠️  No strong trends detected - consider increasing volatility_multiplier")
                    
                # Show backtesting criteria
                print(f"  📊 Backtesting Criteria:")
                print(f"     - Volume: {self.strategy.volume_increase_pct}x average (1.2x)")
                print(f"     - ADX: > {self.strategy.adx_threshold} (15.0)")
                print(f"     - Signal Strength: >= 0.7 (70%)")
                print(f"     - Zone Window: {self.strategy.time_window_hours}h (24h)")
                print(f"     - Min Bars: {self.strategy.lookback} (3)")
        
        print("="*80)
        
        # Log completion and show file location
        mock_logger.info(f"Mock strategy run completed - Duration: {duration:.1f} minutes, Cycles: {cycle_count}")
        print(f"\n📁 Mock data logs saved to: {mock_log_filename}")
        print(f"📂 Mock data logs folder: {mock_logs_dir}")
        
        return {
            'strategy': self.strategy_name,
            'symbols': self.symbols,
            'duration_minutes': duration,
            'analysis_cycles': cycle_count,
            'total_signals': len(self.signals) + len([t for t in self.trades if t.get('action') != 'exit_signal']),
            'total_trades': len(self.trades),
            'signals': self.signals,
            'trades': self.trades
        }

    def _validate_mock_data(self, symbol):
        """Validate mock data quality and compare with strategy requirements."""
        df = self.data_dict[symbol]
        
        # Calculate basic statistics
        current_price = df['close'].iloc[-1]
        price_volatility = df['close'].pct_change().std() * 100
        volume_ma = df['volume'].rolling(20).mean().iloc[-1]
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / volume_ma if volume_ma > 0 else 1.0
        
        # Calculate strategy indicators
        strategy_df = self.strategy.calculate_indicators(df.copy())
        current_adx = strategy_df['adx'].iloc[-1] if 'adx' in strategy_df.columns else 0
        current_volume_ma = strategy_df['volume_ma'].iloc[-1] if 'volume_ma' in strategy_df.columns else volume_ma
        
        # Log validation every 50 bars to avoid spam
        if len(df) % 50 == 0:
            mock_logger.info(
                f"🔍 {symbol} VALIDATION: "
                f"Price:${current_price:.2f} "
                f"Volatility:{price_volatility:.2f}% "
                f"Volume:{current_volume:,.0f} "
                f"VolRatio:{volume_ratio:.1f}x "
                f"ADX:{current_adx:.1f} "
                f"Bars:{len(df)}"
            )
            
            # Check if data meets strategy requirements
            if volume_ratio >= self.strategy.volume_increase_pct:
                mock_logger.info(f"✅ {symbol} Volume requirement met: {volume_ratio:.1f}x >= {self.strategy.volume_increase_pct}x (Backtesting: 1.2x)")
            else:
                mock_logger.info(f"❌ {symbol} Volume requirement not met: {volume_ratio:.1f}x < {self.strategy.volume_increase_pct}x (Backtesting: 1.2x)")
                
            if current_adx >= self.strategy.adx_threshold:
                mock_logger.info(f"✅ {symbol} ADX requirement met: {current_adx:.1f} >= {self.strategy.adx_threshold} (Backtesting: >15)")
            else:
                mock_logger.info(f"❌ {symbol} ADX requirement not met: {current_adx:.1f} < {self.strategy.adx_threshold} (Backtesting: >15)")
                
            # Additional backtesting criteria info
            mock_logger.info(f"📊 {symbol} Backtesting Criteria: Signal Strength >= 0.7, Zone Window: 24h, Min Bars: 3")


async def quick_test():
    """Quick 5-minute test with current entry/exit criteria."""
    runner = SimpleMockStrategyRunner(
        symbols=["PLTR"],
        strategy_name="groq_recommendations",
        check_interval=3,
        verbose_mock_data=True,
        volatility_multiplier=2.5,  # Higher volatility since ADX threshold is now 15
        volume_multiplier=3.0,      # Lower volume since requirement is only 1.2x
        mock_log_to_file=True,      # Save to file
        download_historical_data=True,  # Download real historical data
        historical_duration="1 M",   # 1 month of data
        historical_bar_size="4 hours" # 4-hour bars
    )
    return await runner.run_mock_strategy(duration_minutes=5)


async def standard_test():
    """Standard 15-minute test with current entry/exit criteria."""
    runner = SimpleMockStrategyRunner(
        symbols=["PLTR", "TSLA", "NVDA"],
        strategy_name="groq_recommendations",
        check_interval=5,
        verbose_mock_data=True,
        volatility_multiplier=3.0,  # Higher volatility for ADX > 15
        volume_multiplier=3.5,      # Moderate volume for 1.2x requirement
        mock_log_to_file=True,
        download_historical_data=True,
        historical_duration="1 M",
        historical_bar_size="4 hours"
    )
    return await runner.run_mock_strategy(duration_minutes=15)


async def extended_test():
    """Extended 30-minute test with current entry/exit criteria."""
    runner = SimpleMockStrategyRunner(
        symbols=["PLTR", "NVDA", "TSLA"],
        strategy_name="groq_recommendations",
        check_interval=5,
        verbose_mock_data=True,
        volatility_multiplier=3.5,  # Higher volatility for more signals
        volume_multiplier=4.0,      # Higher volume for more signals
        mock_log_to_file=True,
        download_historical_data=True,
        historical_duration="1 M",
        historical_bar_size="4 hours"
    )
    return await runner.run_mock_strategy(duration_minutes=30)


async def main():
    """Main function to run different test scenarios."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Simple Mock Strategy Runner')
    parser.add_argument('--test', choices=['quick', 'standard', 'extended'], 
                       default='quick', help='Test type to run')
    parser.add_argument('--symbols', nargs='+', default=['PLTR'], 
                       help='Symbols to monitor')
    parser.add_argument('--duration', type=int, default=5, 
                       help='Duration in minutes')
    parser.add_argument('--interval', type=int, default=5, 
                       help='Check interval in seconds')
    parser.add_argument('--verbose', action='store_true', default=True,
                       help='Show detailed mock data logs')
    parser.add_argument('--volatility', type=float, default=2.0,
                       help='Volatility multiplier for mock data (default: 2.0)')
    parser.add_argument('--volume', type=float, default=3.0,
                       help='Volume multiplier for mock data (default: 3.0)')
    parser.add_argument('--console-logs', action='store_true', default=False,
                       help='Display mock data logs in console instead of file')
    parser.add_argument('--download-historical', action='store_true', default=False,
                       help='Download historical data from IBKR for realistic mock data')
    parser.add_argument('--historical-duration', type=str, default='1 M',
                       help='Duration of historical data to download (default: 1 M)')
    parser.add_argument('--historical-bar-size', type=str, default='4 hours',
                       help='Bar size of historical data to download (default: 4 hours)')
    
    args = parser.parse_args()
    
    if args.test == 'quick':
        results = await quick_test()
    elif args.test == 'standard':
        results = await standard_test()
    elif args.test == 'extended':
        results = await extended_test()
    else:
        # Custom run
        runner = SimpleMockStrategyRunner(
            symbols=args.symbols,
            strategy_name="groq_recommendations",
            check_interval=args.interval,
            verbose_mock_data=args.verbose,
            volatility_multiplier=args.volatility,
            volume_multiplier=args.volume,
            mock_log_to_file=not args.console_logs,  # File by default, console if --console-logs
            download_historical_data=args.download_historical,
            historical_duration=args.historical_duration,
            historical_bar_size=args.historical_bar_size
        )
        results = await runner.run_mock_strategy(duration_minutes=args.duration)
    
    return results


if __name__ == "__main__":
    asyncio.run(main()) 