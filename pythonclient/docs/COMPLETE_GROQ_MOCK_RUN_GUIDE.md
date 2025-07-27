# Simple Mock Strategy Guide

## Overview

This guide provides instructions for running mock trading sessions with the Groq recommendations strategy using a **simple, intuitive approach** that follows the exact same flow as the actual strategy in `main.py`, but swaps out real market data with mock data.

## What is the Simple Mock Strategy Approach?

The simple mock strategy approach:

- **Follows the exact same flow** as the actual strategy in `main.py`
- **Swaps out real market data** with mock data
- **Uses the same strategy logic** and signal generation
- **Simulates the same trading process** without IB connection
- **Provides intuitive testing** that mirrors real trading behavior

## Quick Start

### Basic Mock Strategy Run

```bash
# Quick 5-minute test
python examples/simple_mock_strategy.py --test quick

# Standard 30-minute test
python examples/simple_mock_strategy.py --test standard

# Extended 2-hour test
python examples/simple_mock_strategy.py --test extended
```

### Custom Mock Strategy Run

```bash
# Custom symbols and duration
python examples/simple_mock_strategy.py --symbols PLTR TSLA NVDA --duration 15 --interval 3
```

## How It Works

The simple mock strategy follows the **exact same 4-step process** as the actual strategy:

### Step 1: Generate Mock Market Data
```python
# Replaces real IB market data with mock data
await self._generate_mock_data(current_time)
```

### Step 2: Check Existing Positions
```python
# Same logic as main.py for position monitoring
await self._check_existing_positions(current_time)
```

### Step 3: Generate New Signals
```python
# Same signal generation logic as main.py
await self._generate_new_signals(current_time)
```

### Step 4: Execute Trades
```python
# Same trade execution logic as main.py (simulated)
await self._execute_trades(current_time)
```

## Test Types

### 1. Quick Test (5 minutes)
```bash
python examples/simple_mock_strategy.py --test quick
```

**Configuration**:
- Duration: 5 minutes
- Symbols: PLTR only
- Check interval: 3 seconds
- Strategy: groq_recommendations

**Expected Output**:
```
🚀 SIMPLE MOCK STRATEGY RUN
================================================================================
Strategy: groq_recommendations
Symbols: PLTR
Duration: 5 minutes
Check interval: 3 seconds
Start time: 2025-06-24 10:30:00 EDT
================================================================================

🎯 GROQ STRATEGY SIGNAL DETECTED
============================================================
Symbol: PLTR
Signal: BUY
Price: $53.64
Volume: 15,379,511
Strategy: groq_recommendations
Time: 2025-06-24T10:32:15.123456-04:00
============================================================
```

### 2. Standard Test (30 minutes)
```bash
python examples/simple_mock_strategy.py --test standard
```

**Configuration**:
- Duration: 30 minutes
- Symbols: PLTR, TSLA, NVDA
- Check interval: 5 seconds
- Strategy: groq_recommendations

### 3. Extended Test (2 hours)
```bash
python examples/simple_mock_strategy.py --test extended
```

**Configuration**:
- Duration: 120 minutes
- Symbols: PLTR, TSLA, NVDA, AAPL, MSFT
- Check interval: 10 seconds
- Strategy: groq_recommendations

## Custom Configuration

### Custom Symbols and Duration
```bash
python examples/simple_mock_strategy.py \
  --symbols PLTR TSLA NVDA AAPL \
  --duration 45 \
  --interval 5
```

### High-Frequency Testing
```bash
python examples/simple_mock_strategy.py \
  --symbols PLTR TSLA \
  --duration 10 \
  --interval 2
```

### Single Symbol Testing
```bash
python examples/simple_mock_strategy.py \
  --symbols PLTR \
  --duration 15 \
  --interval 3
```

## Expected Output

### Signal Detection
```
🎯 GROQ STRATEGY SIGNAL DETECTED
============================================================
Symbol: PLTR
Signal: BUY
Price: $53.64
Volume: 15,379,511
Strategy: groq_recommendations
Time: 2025-06-24T10:32:15.123456-04:00
============================================================
```

### Progress Updates
```
⏱️  Progress: 2.5/5 minutes (2.5 remaining)
⏱️  Progress: 5.0/5 minutes (0.0 remaining)
```

### Final Summary
```
📊 SIMPLE MOCK STRATEGY SUMMARY
================================================================================
Strategy: groq_recommendations
Symbols Monitored: PLTR
Start Time: 2025-06-24 10:30:00 EDT
End Time: 2025-06-24 10:35:00 EDT
Duration: 5.0 minutes
Analysis Cycles: 100
Total Signals Generated: 8
Total Trades Executed: 8

Signal Breakdown:
  Buy Signals: 6
  Sell Signals: 2

Recent Signals:
  2025-06-24 10:32:15 - PLTR: BUY at $53.64
  2025-06-24 10:33:45 - PLTR: BUY at $53.89
  2025-06-24 10:34:12 - PLTR: SELL at $54.12
================================================================================
```

## Key Advantages

### 1. **Identical Flow to Real Strategy**
- Same 4-step process as `main.py`
- Same signal generation logic
- Same trade execution logic
- Same position monitoring

### 2. **Intuitive Testing**
- No complex threading or async patterns
- Simple, linear execution
- Easy to understand and debug
- Follows familiar strategy patterns

### 3. **Realistic Simulation**
- Uses actual strategy indicators
- Generates realistic mock data
- Simulates real market conditions
- Provides accurate signal testing

### 4. **Easy Customization**
- Simple command-line interface
- Flexible symbol selection
- Adjustable durations and intervals
- Quick parameter changes

## Usage Examples

### Development Workflow
```bash
# 1. Quick test during development
python examples/simple_mock_strategy.py --test quick

# 2. Standard test before committing
python examples/simple_mock_strategy.py --test standard

# 3. Extended test for validation
python examples/simple_mock_strategy.py --test extended
```

### Strategy Validation
```bash
# Test with multiple symbols
python examples/simple_mock_strategy.py \
  --symbols PLTR TSLA NVDA AAPL MSFT \
  --duration 60 \
  --interval 5
```

### Performance Testing
```bash
# High-frequency testing
python examples/simple_mock_strategy.py \
  --symbols PLTR \
  --duration 10 \
  --interval 1
```

## Troubleshooting

### Common Issues

1. **No Signals Generated**
   ```bash
   # Check if strategy is working
   python examples/simple_mock_strategy.py --test quick
   # If no signals, check strategy configuration
   ```

2. **High CPU Usage**
   ```bash
   # Use longer intervals
   python examples/simple_mock_strategy.py --interval 10
   ```

3. **Memory Issues**
   ```bash
   # Reduce number of symbols
   python examples/simple_mock_strategy.py --symbols PLTR
   ```

### Performance Optimization

```bash
# For long runs, use longer intervals
python examples/simple_mock_strategy.py --interval 10 --duration 120

# For high-frequency testing, use shorter intervals
python examples/simple_mock_strategy.py --interval 1 --duration 10
```

## Best Practices

### 1. **Start with Quick Tests**
```bash
# Always start with quick test
python examples/simple_mock_strategy.py --test quick
```

### 2. **Use Appropriate Intervals**
```bash
# Short intervals for high-frequency testing
python examples/simple_mock_strategy.py --interval 1

# Longer intervals for extended runs
python examples/simple_mock_strategy.py --interval 10
```

### 3. **Monitor Signal Quality**
- Check signal frequency
- Verify buy/sell balance
- Monitor signal consistency
- Validate strategy performance

### 4. **Test Different Scenarios**
```bash
# Single symbol testing
python examples/simple_mock_strategy.py --symbols PLTR

# Multi-symbol testing
python examples/simple_mock_strategy.py --symbols PLTR TSLA NVDA

# Extended testing
python examples/simple_mock_strategy.py --test extended
```

## Conclusion

The simple mock strategy approach provides an intuitive way to test your Groq strategy that:

- **Mirrors real trading behavior** exactly
- **Uses the same strategy logic** as production
- **Provides realistic testing** with mock data
- **Offers easy customization** and debugging
- **Follows familiar patterns** from the actual strategy

This approach is much more intuitive than complex threading solutions and provides a direct path to validating your strategy before live trading. 