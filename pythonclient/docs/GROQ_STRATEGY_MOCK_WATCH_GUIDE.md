# Groq Strategy with Real-Time Mock Data - Watch Mode Guide

## Overview

This guide explains how to run the Groq strategy with real-time mock data in watch mode. This allows you to test your trading strategies during market off hours using realistic, simulated market data that mimics actual market behavior.

## What is Watch Mode?

Watch mode is a testing environment where:
- **No real trades are executed** - Only signals are generated and logged
- **Real-time data simulation** - Mock data streams simulate actual market conditions
- **Strategy validation** - Test your strategy logic without financial risk
- **Performance monitoring** - Track signal quality and strategy performance

## Key Benefits

- **Risk-free testing** - No real money involved
- **24/7 availability** - Test anytime, not just during market hours
- **Reproducible results** - Use seeds for consistent testing
- **Real-time simulation** - Experience actual strategy behavior
- **Multiple symbols** - Test across different stocks simultaneously

## Quick Start

### Basic Usage

```python
from examples.groq_strategy_mock_watch import GroqStrategyWatchMode

# Create watch mode system
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA"],
    strategy_name="groq_recommendations",
    seed=42
)

# Start watch mode for 10 minutes
watch_system.start_watch_mode(duration_minutes=10, update_interval=1.0)

# Wait for completion
watch_system.wait_for_completion()

# Get results
results = watch_system.get_results()
print(f"Generated {results['total_signals']} signals")
```

### Running the Example

```bash
# Run the complete example
python examples/groq_strategy_mock_watch.py

# Or run individual examples
python -c "
from examples.groq_strategy_mock_watch import example_groq_recommendations_watch
example_groq_recommendations_watch()
"
```

## Available Strategies

### 1. Groq Recommendations Strategy

```python
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA", "NVDA"],
    strategy_name="groq_recommendations",
    seed=42
)
```

**Features:**
- Generates buy/sell signals based on market conditions
- Incorporates technical indicators and market sentiment
- Real-time signal generation

### 2. Groq Break of Structure Strategy

```python
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA"],
    strategy_name="groq_breakofstructure",
    seed=42
)
```

**Features:**
- Focuses on break of structure patterns
- Risk-reward based signal generation
- Technical analysis with Groq insights
- Pattern recognition and validation

## Configuration Options

### Watch Mode Parameters

```python
watch_system.start_watch_mode(
    duration_minutes=60,    # How long to run (default: 60)
    update_interval=1.0     # Seconds between updates (default: 1.0)
)
```

### Strategy Configuration

```python
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA"],           # List of symbols to monitor
    strategy_name="groq_recommendations", # Strategy to use
    data_folder="historical_data",      # Historical data folder
    timezone="America/New_York",        # Timezone for timestamps
    seed=42                            # Random seed for reproducibility
)
```

## Understanding the Output

### Signal Detection

When a signal is detected, you'll see output like this:

```
============================================================
🎯 GROQ STRATEGY SIGNAL DETECTED
============================================================
Symbol: PLTR
Signal: BUY
Price: $54.67
Volume: 2,847,392
Strategy: groq_recommendations
Time: 2025-06-22T12:34:56-04:00
============================================================
```

### Periodic Analysis

Every 10 analysis cycles, you'll see market analysis:

```
📊 PLTR ANALYSIS:
   Current Price: $54.67
   Price Change: +2.34%
   Avg Volume: 2,847,392
   Volatility: 1.23%
   Bars in Memory: 45
```

### Summary Report

At the end of the session:

```
================================================================================
📊 GROQ STRATEGY WATCH MODE SUMMARY
================================================================================
Strategy: groq_recommendations
Symbols Monitored: PLTR, TSLA
Start Time: 2025-06-22 12:30:00 EDT
End Time: 2025-06-22 12:40:00 EDT
Duration: 0:10:00
Total Signals Generated: 3
Analysis Cycles: 600

Signal Breakdown:
  Buy Signals: 2
  Sell Signals: 1

Recent Signals:
  2025-06-22T12:35:23-04:00 - PLTR: BUY at $54.67
  2025-06-22T12:37:45-04:00 - TSLA: SELL at $245.89
  2025-06-22T12:39:12-04:00 - PLTR: BUY at $55.12
================================================================================
```

## Advanced Usage

### Custom Duration and Intervals

```python
# High-frequency testing (0.5 second updates)
watch_system.start_watch_mode(duration_minutes=5, update_interval=0.5)

# Long-term testing (5 second updates)
watch_system.start_watch_mode(duration_minutes=120, update_interval=5.0)
```

### Multiple Symbols with Different Configurations

```python
# Create watch system
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA", "NVDA", "AAPL"],
    strategy_name="groq_recommendations",
    seed=42
)

# Start with custom settings
watch_system.start_watch_mode(duration_minutes=30, update_interval=1.0)
```

### Interactive Control

```python
# Start watch mode
watch_system.start_watch_mode(duration_minutes=60, update_interval=1.0)

# Let it run for a bit
import time
time.sleep(30)  # Run for 30 seconds

# Check current status
print(f"Signals generated: {len(watch_system.signals)}")
print(f"Analysis cycles: {watch_system.analysis_count}")

# Stop early if needed
watch_system.stop_watch_mode()
```

### Getting Detailed Results

```python
# Get comprehensive results
results = watch_system.get_results()

print(f"Strategy: {results['strategy']}")
print(f"Symbols: {results['symbols']}")
print(f"Total signals: {results['total_signals']}")
print(f"Analysis cycles: {results['analysis_cycles']}")

# Access individual signals
for signal in results['signals']:
    print(f"{signal['timestamp']} - {signal['symbol']}: "
          f"Signal {signal['signal']} at ${signal['price']:.2f}")
```

## Integration with Existing Systems

### With Your Trading System

```python
class MyTradingSystem:
    def __init__(self):
        self.watch_system = GroqStrategyWatchMode(
            symbols=["PLTR", "TSLA"],
            strategy_name="groq_recommendations"
        )
    
    def test_strategy(self):
        """Test strategy in watch mode"""
        self.watch_system.start_watch_mode(duration_minutes=30)
        self.watch_system.wait_for_completion()
        
        results = self.watch_system.get_results()
        return self.analyze_results(results)
    
    def analyze_results(self, results):
        """Analyze watch mode results"""
        # Your analysis logic here
        pass
```

### With Backtesting

```python
# Compare watch mode with backtest results
def compare_modes():
    # Run backtest
    backtest_results = run_backtest("PLTR", "2024-01-01", "2024-01-31")
    
    # Run watch mode
    watch_system = GroqStrategyWatchMode(["PLTR"], "groq_recommendations")
    watch_system.start_watch_mode(duration_minutes=30)
    watch_system.wait_for_completion()
    watch_results = watch_system.get_results()
    
    # Compare results
    compare_signals(backtest_results, watch_results)
```

## Best Practices

### 1. Use Reproducible Seeds

```python
# Always use seeds for consistent testing
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA"],
    strategy_name="groq_recommendations",
    seed=42  # Same seed = same data
)
```

### 2. Appropriate Duration and Intervals

```python
# For quick testing
watch_system.start_watch_mode(duration_minutes=5, update_interval=1.0)

# For thorough testing
watch_system.start_watch_mode(duration_minutes=60, update_interval=1.0)

# For high-frequency testing
watch_system.start_watch_mode(duration_minutes=10, update_interval=0.5)
```

### 3. Monitor Performance

```python
# Check signal quality
results = watch_system.get_results()
buy_signals = [s for s in results['signals'] if s['signal'] == 1]
sell_signals = [s for s in results['signals'] if s['signal'] == -1]

print(f"Buy signals: {len(buy_signals)}")
print(f"Sell signals: {len(sell_signals)}")
print(f"Signal frequency: {len(results['signals']) / (results['duration_minutes'] / 60):.2f} per hour")
```

### 4. Error Handling

```python
try:
    watch_system.start_watch_mode(duration_minutes=30)
    watch_system.wait_for_completion()
except KeyboardInterrupt:
    print("Stopping watch mode early...")
    watch_system.stop_watch_mode()
except Exception as e:
    print(f"Error in watch mode: {e}")
    watch_system.stop_watch_mode()
```

## Troubleshooting

### Common Issues

1. **No Signals Generated**
   - Check if strategy is properly initialized
   - Verify symbol names are correct
   - Ensure sufficient data is available (minimum 20 bars)
   - Check strategy parameters

2. **High CPU Usage**
   - Increase update_interval
   - Reduce number of symbols
   - Check for infinite loops in strategy

3. **Memory Issues**
   - Reduce duration_minutes
   - Monitor data accumulation
   - Check for memory leaks in strategy

4. **Strategy Import Errors**
   - Verify strategy files exist
   - Check import paths
   - Ensure all dependencies are installed

### Debug Mode

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# This will show detailed operations
watch_system = GroqStrategyWatchMode(["PLTR"], "groq_recommendations")
watch_system.start_watch_mode(duration_minutes=5)
```

## Examples

### Example 1: Quick Strategy Test

```python
# Quick 5-minute test
watch_system = GroqStrategyWatchMode(["PLTR"], "groq_recommendations", seed=42)
watch_system.start_watch_mode(duration_minutes=5, update_interval=1.0)
watch_system.wait_for_completion()
```

### Example 2: Multi-Symbol Testing

```python
# Test multiple symbols
watch_system = GroqStrategyWatchMode(
    symbols=["PLTR", "TSLA", "NVDA", "AAPL"],
    strategy_name="groq_recommendations",
    seed=42
)
watch_system.start_watch_mode(duration_minutes=30, update_interval=1.0)
watch_system.wait_for_completion()
```

### Example 3: Strategy Comparison

```python
# Compare different strategies
strategies = ["groq_recommendations", "groq_breakofstructure"]

for strategy in strategies:
    print(f"\nTesting {strategy}...")
    watch_system = GroqStrategyWatchMode(["PLTR"], strategy, seed=42)
    watch_system.start_watch_mode(duration_minutes=10)
    watch_system.wait_for_completion()
    
    results = watch_system.get_results()
    print(f"Signals generated: {results['total_signals']}")
```

## Conclusion

The Groq Strategy Watch Mode with real-time mock data provides a powerful, risk-free environment for testing your trading strategies. It allows you to validate strategy logic, monitor performance, and gain confidence in your trading approach before deploying with real capital.

Key benefits:
- **Risk-free testing** with realistic market simulation
- **24/7 availability** for strategy validation
- **Reproducible results** for consistent testing
- **Real-time signal monitoring** for immediate feedback
- **Comprehensive logging** for performance analysis

Use this system to refine your strategies, test new ideas, and build confidence in your trading approach. 