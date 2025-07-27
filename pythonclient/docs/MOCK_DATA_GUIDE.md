# Mock Data Guide

## Overview

This guide explains how to use mock data for testing trading strategies when real market data is not available (e.g., during market off-hours). The mock data system provides realistic OHLCV bars that simulate real market conditions.

## Quick Start

### Simple Mock Strategy Testing

The easiest way to test your strategy with mock data is using the simple mock strategy runner:

```bash
# Quick 5-minute test
python examples/simple_mock_strategy.py --test quick

# Standard 30-minute test  
python examples/simple_mock_strategy.py --test standard

# Custom configuration
python examples/simple_mock_strategy.py --symbols PLTR TSLA --duration 15 --interval 3
```

This approach follows the exact same flow as the actual strategy in `main.py` but swaps out real market data with mock data.

## Mock Data Generator

### Basic Usage

```python
from utils.mock_data_generator import MockDataGenerator

# Initialize the generator
generator = MockDataGenerator()

# Generate mock bars for a symbol
bars = generator.generate_mock_bars(
    symbol="PLTR",
    num_bars=100,
    start_time=datetime.now()
)

# Convert to DataFrame
df = generator.bars_to_dataframe(bars)
```

### Configuration Options

```python
# Custom configuration
bars = generator.generate_mock_bars(
    symbol="TSLA",
    num_bars=50,
    start_time=datetime.now(),
    volatility=0.02,  # 2% volatility
    trend=0.001,      # Slight upward trend
    volume_range=(1000000, 5000000)  # Volume range
)
```

## Mock Data Features

### Realistic Price Movement
- **Volatility**: Configurable price volatility
- **Trends**: Upward, downward, or sideways trends
- **Gaps**: Realistic price gaps between bars
- **Correlations**: Price movements based on historical patterns

### Volume Simulation
- **Volume Patterns**: Realistic volume distributions
- **Time-based**: Higher volume during market hours
- **Price Correlation**: Volume spikes with price movements

### Multiple Timeframes
- **1-minute bars**: High-frequency data
- **5-minute bars**: Standard intraday data
- **1-hour bars**: Swing trading data
- **Daily bars**: Long-term analysis

## Integration with Strategies

### Direct Integration

```python
from utils.mock_data_generator import MockDataGenerator
from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy

# Initialize components
generator = MockDataGenerator()
strategy = GroqRecommendationsStrategy()

# Generate mock data
bars = generator.generate_mock_bars("PLTR", 100)
df = generator.bars_to_dataframe(bars)

# Run strategy analysis
df_with_indicators = strategy.calculate_indicators(df)
signals = strategy.generate_signals(df_with_indicators)

print(f"Generated {len(signals)} signals")
```

### Real-time Simulation

```python
import asyncio
from datetime import datetime, timedelta

async def simulate_realtime():
    generator = MockDataGenerator()
    strategy = GroqRecommendationsStrategy()
    
    start_time = datetime.now()
    end_time = start_time + timedelta(minutes=30)
    
    while datetime.now() < end_time:
        # Generate one bar
        bars = generator.generate_mock_bars("PLTR", 1, datetime.now())
        df = generator.bars_to_dataframe(bars)
        
        # Process with strategy
        df_with_indicators = strategy.calculate_indicators(df)
        signal = await strategy.generate_realtime_signals(df_with_indicators)
        
        if signal != 0:
            print(f"Signal: {signal} at {datetime.now()}")
        
        await asyncio.sleep(5)  # 5-second intervals

# Run simulation
asyncio.run(simulate_realtime())
```

## Testing Scenarios

### 1. Quick Validation
```bash
python examples/simple_mock_strategy.py --test quick
```
- 5-minute test
- Single symbol (PLTR)
- 3-second intervals
- Perfect for development and debugging

### 2. Standard Testing
```bash
python examples/simple_mock_strategy.py --test standard
```
- 30-minute test
- Multiple symbols (PLTR, TSLA, NVDA)
- 5-second intervals
- Comprehensive strategy validation

### 3. Extended Testing
```bash
python examples/simple_mock_strategy.py --test extended
```
- 2-hour test
- 5 symbols (PLTR, TSLA, NVDA, AAPL, MSFT)
- 10-second intervals
- Long-term pattern analysis

### 4. Custom Testing
```bash
python examples/simple_mock_strategy.py \
  --symbols PLTR TSLA NVDA \
  --duration 45 \
  --interval 3
```
- Custom symbols and duration
- Flexible configuration
- Tailored to your needs

## Mock Data Configuration

### Volatility Settings
```python
# Low volatility (stable market)
bars = generator.generate_mock_bars("PLTR", 100, volatility=0.01)

# High volatility (volatile market)
bars = generator.generate_mock_bars("PLTR", 100, volatility=0.05)

# Normal volatility (typical market)
bars = generator.generate_mock_bars("PLTR", 100, volatility=0.02)
```

### Trend Settings
```python
# Bullish trend
bars = generator.generate_mock_bars("PLTR", 100, trend=0.002)

# Bearish trend
bars = generator.generate_mock_bars("PLTR", 100, trend=-0.002)

# Sideways trend
bars = generator.generate_mock_bars("PLTR", 100, trend=0.0)
```

### Volume Settings
```python
# High volume
bars = generator.generate_mock_bars("PLTR", 100, volume_range=(5000000, 20000000))

# Low volume
bars = generator.generate_mock_bars("PLTR", 100, volume_range=(100000, 500000))

# Normal volume
bars = generator.generate_mock_bars("PLTR", 100, volume_range=(1000000, 5000000))
```

## Best Practices

### 1. Use Realistic Parameters
```python
# Good: Realistic volatility
bars = generator.generate_mock_bars("PLTR", 100, volatility=0.02)

# Avoid: Extreme volatility
bars = generator.generate_mock_bars("PLTR", 100, volatility=0.50)
```

### 2. Test Multiple Scenarios
```python
# Test different market conditions
scenarios = [
    {"volatility": 0.01, "trend": 0.001},  # Stable bull market
    {"volatility": 0.03, "trend": -0.001}, # Volatile bear market
    {"volatility": 0.02, "trend": 0.0},    # Sideways market
]

for scenario in scenarios:
    bars = generator.generate_mock_bars("PLTR", 100, **scenario)
    # Test strategy with this scenario
```

### 3. Validate Results
```python
# Check that mock data is realistic
df = generator.bars_to_dataframe(bars)

print(f"Price range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
print(f"Average volume: {df['volume'].mean():,.0f}")
print(f"Price change: {((df['close'].iloc[-1] / df['close'].iloc[0]) - 1) * 100:.2f}%")
```

### 4. Use Appropriate Timeframes
```python
# For high-frequency testing
bars = generator.generate_mock_bars("PLTR", 1000, timeframe="1min")

# For swing trading
bars = generator.generate_mock_bars("PLTR", 100, timeframe="1hour")

# For long-term analysis
bars = generator.generate_mock_bars("PLTR", 50, timeframe="1day")
```

## Troubleshooting

### Common Issues

1. **No Signals Generated**
   ```bash
   # Check if strategy is working
   python examples/simple_mock_strategy.py --test quick
   ```

2. **Unrealistic Data**
   ```python
   # Use more conservative parameters
   bars = generator.generate_mock_bars("PLTR", 100, volatility=0.01)
   ```

3. **Memory Issues**
   ```python
   # Reduce number of bars
   bars = generator.generate_mock_bars("PLTR", 50)
   ```

### Performance Optimization

```python
# For large datasets, process in chunks
chunk_size = 1000
total_bars = 10000

for i in range(0, total_bars, chunk_size):
    bars = generator.generate_mock_bars("PLTR", min(chunk_size, total_bars - i))
    # Process this chunk
```

## Integration Examples

### Backtesting with Mock Data
```python
from utils.mock_data_generator import MockDataGenerator
from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy

# Generate historical mock data
generator = MockDataGenerator()
bars = generator.generate_mock_bars("PLTR", 1000)
df = generator.bars_to_dataframe(bars)

# Run backtest
strategy = GroqRecommendationsStrategy()
results = strategy.backtest(df)

print(f"Backtest results: {results['return_pct']:.2f}% return")
```

### Real-time Strategy Testing
```python
import asyncio
from examples.simple_mock_strategy import SimpleMockStrategyRunner

async def test_strategy():
    runner = SimpleMockStrategyRunner(
        symbols=["PLTR", "TSLA"],
        strategy_name="groq_recommendations",
        check_interval=5
    )
    
    results = await runner.run_mock_strategy(duration_minutes=30)
    print(f"Generated {results['total_signals']} signals")

asyncio.run(test_strategy())
```

## Conclusion

The mock data system provides a powerful way to test trading strategies when real market data is not available. The simple mock strategy approach offers an intuitive way to validate your strategy logic and build confidence before live trading.

Key benefits:
- **Realistic simulation** of market conditions
- **Easy integration** with existing strategies
- **Flexible configuration** for different scenarios
- **Intuitive testing** that mirrors real trading
- **Comprehensive validation** of strategy performance

Start with quick tests and gradually increase complexity as you validate your strategy's performance. 