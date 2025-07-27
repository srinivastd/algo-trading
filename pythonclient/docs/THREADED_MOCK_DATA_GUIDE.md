# Threaded Mock Data Generator Guide

## Overview

The Threaded Mock Data Generator provides a powerful way to simulate real-time market data feeds using separate threads. This allows you to test trading strategies during market off hours with realistic, reproducible data that mimics actual market behavior.

## Key Features

- **Thread-Safe**: All operations are thread-safe with proper locking mechanisms
- **Multi-Symbol Support**: Run multiple symbols simultaneously in separate threads
- **Configurable**: Adjust update intervals, volatility, volume, and other parameters
- **Strategy Integration**: Works seamlessly with existing trading strategies
- **Real-Time Simulation**: Simulates actual real-time data feeds with realistic timing
- **Reproducible**: Uses seeds for consistent, reproducible results

## Architecture

The threaded mock data system consists of three main components:

1. **ThreadedMockDataFeed**: Single symbol feed running in a separate thread
2. **MultiSymbolThreadedFeed**: Manager for multiple symbol feeds
3. **ThreadedMockTradingSystem**: Complete trading system with strategy integration

## Basic Usage

### 1. Simple Single Symbol Feed

```python
from src.utils.mock_data_threaded import ThreadedMockDataFeed
from datetime import datetime

def bar_callback(bar, symbol):
    """Callback function called for each new bar"""
    timestamp = datetime.fromtimestamp(bar.time)
    print(f"{symbol}: {timestamp} - Close: ${bar.close:.2f}")

# Create threaded feed
feed = ThreadedMockDataFeed(
    symbol="PLTR",
    callback=bar_callback,
    seed=42  # For reproducible results
)

# Configure parameters
feed.update_interval = 1.0  # 1 second between bars
feed.volatility_multiplier = 1.2  # 20% more volatile

# Start the feed
feed.start(duration_minutes=5)  # Run for 5 minutes

# Wait for completion
feed.wait_for_completion()
```

### 2. Multi-Symbol Feeds

```python
from src.utils.mock_data_threaded import MultiSymbolThreadedFeed

def bar_callback(bar, symbol):
    timestamp = datetime.fromtimestamp(bar.time)
    print(f"{symbol}: {timestamp} - Close: ${bar.close:.2f}")

# Create multi-symbol feed system
feed_system = MultiSymbolThreadedFeed(seed=42)

# Add symbols with different configurations
feed_system.add_symbol(
    symbol="PLTR",
    callback=bar_callback,
    update_interval=1.0,
    volatility_multiplier=1.0
)

feed_system.add_symbol(
    symbol="TSLA",
    callback=bar_callback,
    update_interval=0.5,  # Faster updates
    volatility_multiplier=1.5  # More volatile
)

# Start all feeds
feed_system.start_all(duration_minutes=10)

# Wait for completion
feed_system.wait_for_completion()
```

### 3. Complete Trading System

```python
from src.utils.mock_data_threaded import ThreadedMockTradingSystem

# Create trading system with strategy
trading_system = ThreadedMockTradingSystem(
    symbols=["PLTR", "TSLA"],
    strategy_name="groq_recommendations",
    seed=42
)

# Start simulation
trading_system.start_simulation(
    duration_minutes=30,
    update_interval=1.0
)

# Wait for completion
trading_system.wait_for_completion()

# Get results
results = trading_system.get_results()
print(f"Signals generated: {results['total_signals']}")
print(f"Trades executed: {results['total_trades']}")
```

## Advanced Usage

### Custom Callback with Analysis

```python
class RealTimeAnalyzer:
    def __init__(self):
        self.data = {}
        self.analysis_count = 0
    
    def analyze_bar(self, bar, symbol):
        """Custom callback with real-time analysis"""
        timestamp = datetime.fromtimestamp(bar.time)
        
        # Store data
        if symbol not in self.data:
            self.data[symbol] = []
        
        self.data[symbol].append({
            'timestamp': timestamp,
            'close': bar.close,
            'volume': bar.volume
        })
        
        # Keep only recent data
        if len(self.data[symbol]) > 50:
            self.data[symbol] = self.data[symbol][-50:]
        
        # Perform analysis every 10 bars
        self.analysis_count += 1
        if self.analysis_count % 10 == 0:
            self._perform_analysis(symbol)
    
    def _perform_analysis(self, symbol):
        """Perform real-time analysis"""
        if len(self.data[symbol]) < 10:
            return
        
        closes = [bar['close'] for bar in self.data[symbol]]
        
        # Calculate metrics
        price_change = (closes[-1] - closes[0]) / closes[0] * 100
        volatility = np.std(closes) / np.mean(closes) * 100
        
        print(f"📊 {symbol} ANALYSIS: Change: {price_change:+.2f}%, Vol: {volatility:.2f}%")

# Use custom analyzer
analyzer = RealTimeAnalyzer()
feed = ThreadedMockDataFeed("PLTR", analyzer.analyze_bar, seed=42)
feed.start(duration_minutes=5)
feed.wait_for_completion()
```

### Interactive Control

```python
# Create feed system
feed_system = MultiSymbolThreadedFeed(seed=42)

# Add symbols
feed_system.add_symbol("PLTR", bar_callback, update_interval=1.0)
feed_system.add_symbol("TSLA", bar_callback, update_interval=1.0)

# Start feeds
feed_system.start_all(duration_minutes=10)

# Let it run for a bit
time.sleep(5)

# Check what's running
running_symbols = feed_system.get_running_symbols()
print(f"Running symbols: {running_symbols}")

# Stop one symbol
feed_system.stop_symbol("TSLA")

# Continue for a bit more
time.sleep(3)

# Stop all
feed_system.stop_all()
```

## Configuration Options

### ThreadedMockDataFeed Parameters

- **symbol**: Stock symbol (e.g., "PLTR")
- **callback**: Function to call with each new bar
- **data_folder**: Folder containing historical CSV files
- **timezone**: Timezone for timestamps (default: "America/New_York")
- **seed**: Random seed for reproducible results

### Feed Configuration

- **update_interval**: Seconds between bar updates (default: 1.0)
- **bar_size**: Time interval for bars ("5 secs", "15 mins", "1 hour")
- **volatility_multiplier**: Multiplier for price volatility (default: 1.0)
- **volume_multiplier**: Multiplier for volume (default: 1.0)

### ThreadedMockTradingSystem Parameters

- **symbols**: List of stock symbols to trade
- **strategy_name**: Strategy to use ("groq_recommendations", "groq_breakofstructure")
- **data_folder**: Folder containing historical CSV files
- **timezone**: Timezone for timestamps
- **seed**: Random seed for reproducible results

## Threading Model

### Thread Safety

All components use proper threading mechanisms:

- **Locks**: Thread-safe operations with `threading.Lock()`
- **Daemon Threads**: All threads are daemon threads that terminate when main thread ends
- **Graceful Shutdown**: Proper cleanup and thread joining on stop

### Thread Architecture

```
Main Thread
├── ThreadedMockDataFeed Thread (Symbol 1)
├── ThreadedMockDataFeed Thread (Symbol 2)
├── ThreadedMockDataFeed Thread (Symbol N)
└── Analysis Thread (Trading System)
```

### Performance Considerations

- **Memory Usage**: Each symbol feed maintains its own data cache
- **CPU Usage**: Minimal CPU usage with sleep intervals between updates
- **Scalability**: Can handle multiple symbols efficiently
- **Resource Management**: Automatic cleanup of completed threads

## Integration with Existing Systems

### With IBKR Trading System

The threaded mock data generator can be used as a drop-in replacement for real IBKR data:

```python
# Instead of real IBKR connection
# ib.reqRealTimeBars(...)

# Use threaded mock data
feed = ThreadedMockDataFeed("PLTR", your_callback_function)
feed.start(duration_minutes=60)
```

### With Trading Strategies

Strategies work seamlessly with threaded mock data:

```python
# Strategy receives RealTimeBar objects (same as IBKR)
def strategy_callback(bar, symbol):
    # Your strategy logic here
    signal = your_strategy.analyze(bar)
    if signal != 0:
        execute_trade(symbol, signal)
```

## Error Handling

### Exception Handling

All threaded operations include proper exception handling:

```python
try:
    feed.start(duration_minutes=10)
    feed.wait_for_completion()
except Exception as e:
    print(f"Feed error: {e}")
    feed.stop()  # Clean shutdown
```

### Graceful Shutdown

```python
# Stop feeds gracefully
feed_system.stop_all()

# Wait for threads to finish
feed_system.wait_for_completion(timeout=10.0)
```

## Testing and Validation

### Reproducible Results

Use seeds for consistent testing:

```python
# Same seed = same data
feed1 = ThreadedMockDataFeed("PLTR", callback, seed=42)
feed2 = ThreadedMockDataFeed("PLTR", callback, seed=42)
# Both feeds will generate identical data
```

### Performance Testing

```python
import time

start_time = time.time()
feed.start(duration_minutes=5)
feed.wait_for_completion()
end_time = time.time()

print(f"Simulation took {end_time - start_time:.2f} seconds")
```

## Best Practices

### 1. Resource Management

```python
# Always clean up resources
try:
    feed.start(duration_minutes=10)
    feed.wait_for_completion()
finally:
    feed.stop()
```

### 2. Callback Design

```python
def efficient_callback(bar, symbol):
    # Keep callbacks lightweight
    # Avoid heavy computations in callbacks
    # Use separate analysis threads for heavy work
    pass
```

### 3. Configuration

```python
# Use appropriate update intervals
# 1.0 second for testing
# 0.1 second for high-frequency simulation
# 5.0 seconds for long-term testing
```

### 4. Monitoring

```python
# Monitor thread status
if feed.is_running():
    print("Feed is active")
else:
    print("Feed has stopped")
```

## Troubleshooting

### Common Issues

1. **Thread Not Starting**
   - Check if callback function is properly defined
   - Verify symbol name is valid
   - Ensure data folder exists

2. **High CPU Usage**
   - Increase update_interval
   - Reduce number of concurrent symbols
   - Optimize callback functions

3. **Memory Issues**
   - Limit data retention in callbacks
   - Use appropriate bar sizes
   - Monitor data accumulation

4. **Thread Hanging**
   - Set timeout on wait_for_completion()
   - Check for infinite loops in callbacks
   - Verify proper exception handling

### Debug Mode

Enable debug logging for troubleshooting:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# This will show detailed thread operations
feed = ThreadedMockDataFeed("PLTR", callback)
feed.start(duration_minutes=1)
```

## Examples

See the `examples/threaded_mock_data_example.py` file for comprehensive examples of all usage patterns.

## Conclusion

The Threaded Mock Data Generator provides a powerful, flexible, and thread-safe way to simulate real-time market data for testing trading strategies. It integrates seamlessly with existing systems and provides realistic, reproducible data for comprehensive testing and validation. 