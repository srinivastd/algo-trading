# Detailed Analysis Guide: Using the Query Script

This guide shows you how to use `src/query_recent_backtest.py` for comprehensive analysis of your trading strategy performance.

## Basic Usage

### 1. Analyze Most Recent Backtest (Any Symbol)
```bash
python3 src/query_recent_backtest.py
```

### 2. Analyze Specific Symbol
```bash
python3 src/query_recent_backtest.py --symbol PLTR
python3 src/query_recent_backtest.py --symbol NVDA
python3 src/query_recent_backtest.py --symbol QBTS
```

## Understanding the Analysis Output

### 📊 Market Conditions Analysis

#### **ADX (Average Directional Index)**
- **78.46** = 🔥 STRONG TREND
- **Interpretation**: 
  - > 25: Strong trend (excellent for trend-following strategies)
  - 20-25: Moderate trend
  - < 20: Weak trend (may cause false signals)

#### **Volatility**
- **0.9760** = High volatility
- **Interpretation**:
  - Higher volatility = larger price swings = potentially higher profits/losses
  - Your strategy performs well in volatile conditions

#### **SMA 20 vs SMA 50**
- **SMA 20: 155.44** vs **SMA 50: 153.34**
- **Trend Analysis: 📈 BULLISH**
- **Interpretation**: Short-term trend is above long-term trend = bullish momentum

#### **Volume Change**
- **0.4270** = 42.7% volume increase
- **Interpretation**: Strong volume confirmation for signals

#### **Valid Consolidation**
- **❌** = No consolidation pattern
- **Interpretation**: Market is trending, not consolidating

### 💰 Performance Metrics Analysis

#### **Return Analysis**
- **Total Profit: $143,044.40**
- **Return %: +286.09%**
- **Performance Rating: 🏆 EXCELLENT**

#### **Trade Quality**
- **Success Rate: 43.8%** (176 total trades)
- **Interpretation**: 
  - Lower success rate but high profit per winning trade
  - Strategy relies on large wins to overcome smaller losses

#### **Capital Growth**
- **Initial: $50,000** → **Final: $193,044**
- **Growth Factor: 3.86x**

### ⚙️ Strategy Parameters Analysis

#### **Risk Management**
- **Risk per Trade: $1,846.09** (3.7% of capital)
- **Stop Loss: 0.8%** (tight stops)
- **Target: 6.0%** (7.5:1 reward-to-risk ratio)

#### **Configuration**
- **Bar Size: 1 hour** (intraday strategy)
- **Duration: 1 Y** (long-term backtest)
- **Paper Trading: No** (live trading results)

## How to Use for Strategy Optimization

### 1. **Parameter Tuning Analysis**
```bash
# Compare different symbols to see which parameters work best
python3 src/query_recent_backtest.py --symbol PLTR
python3 src/query_recent_backtest.py --symbol NVDA
python3 src/query_recent_backtest.py --symbol QBTS
```

**Look for patterns:**
- Which symbols have highest success rates?
- Which have best reward-to-risk ratios?
- What market conditions (ADX, volatility) lead to best performance?

### 2. **Market Condition Analysis**
Compare results across different market conditions:

**Strong Trend Markets (ADX > 25):**
- ✅ Usually better performance
- ✅ Higher success rates
- ✅ Larger profits

**Weak Trend Markets (ADX < 20):**
- ⚠️ May have lower success rates
- ⚠️ More false signals
- ⚠️ Consider reducing position sizes

### 3. **Volatility Impact Analysis**
**High Volatility (> 0.5):**
- ✅ Larger profit potential
- ✅ Strategy performs well
- ⚠️ Higher risk of large losses

**Low Volatility (< 0.2):**
- ⚠️ Smaller profit potential
- ⚠️ May need wider stops/targets
- ⚠️ Consider different timeframes

### 4. **Trend Direction Analysis**
**Bullish Markets (SMA 20 > SMA 50):**
- ✅ Better for LONG signals
- ✅ Higher success rates on longs
- ⚠️ May struggle with SHORT signals

**Bearish Markets (SMA 20 < SMA 50):**
- ✅ Better for SHORT signals
- ✅ Higher success rates on shorts
- ⚠️ May struggle with LONG signals

## Advanced Analysis Techniques

### 1. **Performance Comparison**
```bash
# Run multiple queries and compare
python3 src/query_recent_backtest.py --symbol PLTR > pltr_analysis.txt
python3 src/query_recent_backtest.py --symbol NVDA > nvda_analysis.txt
python3 src/query_recent_backtest.py --symbol QBTS > qbts_analysis.txt
```

### 2. **Parameter Optimization**
Use the results to optimize your strategy:

**Based on PLTR results:**
- ADX: 78.46 (excellent trend strength)
- Success Rate: 43.8% (acceptable)
- Return: +286% (excellent)
- **Recommendation**: Strategy works well in strong trending markets

### 3. **Risk Management Insights**
**Current Settings:**
- Stop Loss: 0.8% (tight)
- Target: 6.0% (wide)
- Risk per Trade: $1,846 (3.7% of capital)

**Potential Optimizations:**
- Consider wider stops (1.0-1.2%) to reduce false exits
- Consider tighter targets (4.0-5.0%) to increase success rate
- Adjust risk per trade based on volatility

## Key Insights from Your Results

### 🎯 **What's Working Well:**
1. **Strong Trend Performance**: ADX 78.46 shows strategy excels in trending markets
2. **Excellent Returns**: +286% return is outstanding
3. **Good Risk Management**: 7.5:1 reward-to-risk ratio
4. **Volume Confirmation**: 42.7% volume increase validates signals

### ⚠️ **Areas for Improvement:**
1. **Success Rate**: 43.8% could be higher
2. **Trade Frequency**: 176 trades in 1 year (about 1.5 trades per week)
3. **Market Dependency**: Performance heavily dependent on trending markets

### 🔧 **Optimization Suggestions:**
1. **Add Market Filters**: Only trade when ADX > 25
2. **Adjust Stop Loss**: Consider 1.0% instead of 0.8%
3. **Volume Thresholds**: Require minimum volume increase (e.g., 50%)
4. **Trend Confirmation**: Add additional trend indicators

## Troubleshooting Common Issues

### No Results Found
```bash
# Check if symbol exists
python3 src/quick_query.py  # See all available symbols
```

### Symbol Case Sensitivity
```bash
# Use uppercase for symbols
python3 src/query_recent_backtest.py --symbol PLTR  # ✅ Correct
python3 src/query_recent_backtest.py --symbol pltr  # ❌ May not work
```

### Multiple Results for Same Symbol
The script automatically shows the **most recent** backtest for the symbol.

## Next Steps

1. **Run Analysis**: Use the detailed query script on your recent backtests
2. **Compare Results**: Analyze multiple symbols and time periods
3. **Identify Patterns**: Look for common market conditions in profitable trades
4. **Optimize Parameters**: Adjust strategy based on findings
5. **Test Changes**: Run new backtests with optimized parameters
6. **Monitor Performance**: Use the script to track improvements over time

## Example Analysis Workflow

```bash
# 1. Quick overview of recent performance
python3 src/quick_query.py

# 2. Detailed analysis of best performer
python3 src/query_recent_backtest.py --symbol PLTR

# 3. Compare with other symbols
python3 src/query_recent_backtest.py --symbol NVDA

# 4. Use AI-powered insights
python3 src/query_backtest_results.py

# 5. Manual database inspection
python3 src/inspect_chromadb.py --min-profit 50000
```

This comprehensive analysis will help you understand your strategy's strengths, weaknesses, and optimization opportunities. 