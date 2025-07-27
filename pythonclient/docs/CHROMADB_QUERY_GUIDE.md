# ChromaDB Query Guide

This guide explains how to query ChromaDB for backtest results to analyze market conditions and performance metrics.

## Overview

ChromaDB stores comprehensive backtest results including:
- **Performance Metrics**: Profit, success rate, return percentage
- **Market Conditions**: ADX, volatility, SMA values, volume changes
- **Strategy Parameters**: Risk per trade, stop loss, target percentages
- **Trade Details**: Complete trade history and execution data

## Quick Query Script

### Basic Usage
```bash
# Get most recent backtest for any symbol
python3 src/quick_query.py

# Get most recent backtest for specific symbol
python3 src/quick_query.py NVDA
```

### Output Format
```
📅 2025-07-27 09:12:26
📊 qbts - Backtest result for qbts: 338125.38 profit, 39.4% success rate
💰 Profit: $338,125.38
📈 Return: +676.3%
🎯 Success: 39.4% (198 trades)
📊 ADX: 41.3 | Vol: 0.3200
⚙️  Risk: $3,718 | SL: 0.8% | Target: 6.0%
```

## Detailed Query Script

### Basic Usage
```bash
# Get detailed analysis of most recent backtest
python3 src/query_recent_backtest.py

# Get detailed analysis for specific symbol
python3 src/query_recent_backtest.py --symbol NVDA
```

### Output Sections

#### 1. Market Conditions
- **ADX**: Average Directional Index (trend strength)
- **Volatility**: Market volatility measure
- **SMA 20/50**: Simple Moving Averages
- **Volume Change**: Volume momentum
- **Trend Analysis**: Bullish/Bearish based on SMA comparison
- **Market Strength**: Strong/Moderate/Weak trend classification

#### 2. Performance Metrics
- **Capital**: Initial and final capital amounts
- **Profit**: Total profit in dollars
- **Return %**: Percentage return on investment
- **Success Rate**: Percentage of profitable trades
- **Total Trades**: Number of trades executed
- **Performance Rating**: Excellent/Good/Profitable/Loss

#### 3. Strategy Parameters
- **Risk per Trade**: Dollar amount risked per trade
- **Stop Loss %**: Stop loss percentage
- **Target %**: Profit target percentage
- **Bar Size**: Timeframe used (e.g., "1 hour")
- **Duration**: Backtest period (e.g., "1 Y")
- **Paper Trading**: Whether paper trading was used

#### 4. Trade Details
- Individual trade information (when available)
- Entry/exit prices and profits
- Trade types (LONG/SHORT)

## Existing Query Tools

### 1. `src/inspect_chromadb.py`
```bash
# Query with filters
python3 src/inspect_chromadb.py --symbol NVDA --min-profit 1000 --limit 5
```

**Features:**
- Filter by symbol
- Filter by minimum profit
- Limit number of results
- Display raw metadata

### 2. `src/query_backtest_results.py`
```bash
# AI-powered analysis using TinyLLaMA
python3 src/query_backtest_results.py
```

**Features:**
- LLM-powered analysis of recent results
- Pattern recognition for strategy optimization
- Actionable recommendations

## Data Structure

### Metadata Fields
```python
{
    "symbol": "NVDA",
    "total_profit": 338125.38,
    "success_rate": 39.4,
    "initial_capital": 50000.0,
    "final_capital": 388125.38,
    "total_trades": 198,
    "risk_per_trade": 3717.76,
    "stop_loss_pct": 0.8,
    "target_pct": 6.0,
    "volatility": 0.32,
    "adx": 41.31,
    "sma_20": 19.49,
    "sma_50": 18.85,
    "valid_consolidation": False,
    "volume_change": 0.42,
    "bar_size": "1 hour",
    "duration": "1 Y",
    "paper_trading": False,
    "trades": "[{trade_data}]"
}
```

## Use Cases

### 1. Strategy Performance Analysis
```bash
# Check recent performance
python3 src/quick_query.py

# Get detailed analysis
python3 src/query_recent_backtest.py
```

### 2. Parameter Optimization
```bash
# Compare different parameter sets
python3 src/inspect_chromadb.py --min-profit 50000
```

### 3. Market Condition Analysis
```bash
# Analyze market conditions for profitable trades
python3 src/query_recent_backtest.py
```

### 4. AI-Powered Insights
```bash
# Get LLM recommendations
python3 src/query_backtest_results.py
```

## Tips

1. **Most Recent First**: All queries automatically sort by timestamp (most recent first)
2. **Symbol Filtering**: Use `--symbol` to filter results for specific stocks
3. **Performance Focus**: Use `--min-profit` to find high-performing backtests
4. **Detailed Analysis**: Use the detailed query script for comprehensive analysis
5. **AI Insights**: Use the LLM-powered script for strategy recommendations

## Troubleshooting

### No Results Found
- Check if ChromaDB database exists in `./chroma_db/`
- Verify backtests have been run and stored
- Check symbol case sensitivity (use uppercase)

### Import Errors
- Ensure ChromaDB is installed: `pip install chromadb`
- Check sentence-transformers installation
- Verify Python environment

### Performance Issues
- Large databases may take time to query
- Use `--limit` to restrict results
- Consider filtering by symbol for faster queries 