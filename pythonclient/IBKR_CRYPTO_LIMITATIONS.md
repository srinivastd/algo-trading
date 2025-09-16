# IBKR Crypto Trading Limitations & Fixes

## 🚨 Critical IBKR Crypto Restrictions

### 1. **NO SHORT SELLING** ❌
- **Error**: `Error 201: Short sales are not allowed for this asset`
- **Limitation**: IBKR does not allow short selling of cryptocurrencies
- **Impact**: Only LONG positions are allowed for crypto
- **Fix**: Strategy now skips short signals for crypto symbols

### 2. **NO STOP ORDERS** ❌  
- **Error**: `Error 10292: Stop Buy order is not allowed for this instrument`
- **Limitation**: IBKR does not support stop-loss or stop orders for crypto
- **Impact**: Cannot use STP order type for crypto
- **Fix**: Strategy now skips stop-loss orders for crypto

### 3. **PRICE PRECISION REQUIREMENTS** ⚠️
- **Error**: `Warning 110: The price does not conform to the minimum price variation`
- **Limitation**: Crypto prices must conform to minimum tick size (0.01 for ETH)
- **Impact**: Prices like `4126.896000000001` are rejected
- **Fix**: All crypto prices are now rounded to nearest tick (0.01)

### 4. **TIME IN FORCE (TIF) RESTRICTIONS** ❌
- **Error**: `Error 201: The crypto buy order must be Minutes or IOC`
- **Limitation**: IBKR crypto orders cannot use `GTC` (Good Till Canceled)
- **Required**: Must use `IOC` (Immediate Or Cancel) or `Minutes`
- **Fix**: Crypto orders now use `TIF=IOC`, stock orders use `TIF=GTC`

### 5. **POSITION SIZE LIMITS** ❌
- **Error**: `Error 201: Order rejected - if executed, your order would cause your crypto account(s) to exceed the lesser of: 30% of your total account equity; and USD 3 million`
- **Limitation**: Crypto positions cannot exceed 30% of total account equity OR $3M
- **Impact**: Large crypto orders get rejected
- **Fix**: Added crypto-aware position sizing with safety margins

## ✅ Allowed Orders for ETH

| Order Type | Supported | Notes |
|------------|-----------|-------|
| **Market (MKT)** | ✅ | Buy only, TIF=IOC |
| **Limit (LMT)** | ✅ | Buy only, TIF=IOC |
| **Stop (STP)** | ❌ | Not supported |
| **Stop-Limit** | ❌ | Not supported |
| **Short Sales** | ❌ | Not allowed |
| **GTC Orders** | ❌ | Must use IOC or Minutes |

## ✅ Code Fixes Applied

### 1. **Crypto-Only Long Signals**
- `generate_signals()` now skips short signals for crypto
- `generate_realtime_signals()` now skips short signals for crypto
- Only demand zone breakouts (long signals) are generated for crypto

### 2. **Price Rounding**
- Added `CryptoUtils.round_crypto_price()` function
- All entry, stop, and target prices rounded to 0.01 precision
- Prevents price precision errors

### 3. **Order Type Restrictions**
- `execute_trade()` blocks short selling attempts for crypto
- Stop-loss orders are skipped for crypto symbols
- Only limit and market orders are placed for crypto

### 4. **Time In Force (TIF) Fix**
- Crypto orders: `TIF=IOC` (Immediate Or Cancel)
- Stock orders: `TIF=GTC` (Good Till Canceled)
- Prevents "must be Minutes or IOC" error

### 6. **Position Size Management**
- Real-time account equity checking for crypto limits
- Automatic position size reduction to stay under 25% (safe margin)
- Crypto risk multiplier: 50% of normal risk per trade
- Warning system for large crypto positions

### 7. **24/7 Trading Support**
- Historical data: `useRTH=False` for crypto
- Real-time data: `useRTH=False` for crypto  
- Watch mode: No market hours restrictions for crypto
- Orders: `outsideRth=True` for crypto

## 📋 ETH Trading Strategy Summary

### What Works:
✅ **Long positions only** (BUY ETH)  
✅ **Limit orders** with proper price precision  
✅ **Market orders** for immediate execution  
✅ **24/7 trading** (no market hour restrictions)  
✅ **Target orders** for profit taking  

### What Doesn't Work:
❌ **Short positions** (SELL ETH you don't own)  
❌ **Stop-loss orders** (must manage risk manually)  
❌ **Stop orders** of any kind  

### Risk Management for Crypto:
Since stop-loss orders aren't available, you must:
1. **Monitor positions manually**
2. **Use limit orders to exit** at predetermined levels
3. **Set alerts** for price movements
4. **Consider smaller position sizes** due to lack of automatic stop-loss

## 🔄 Migration Impact

### Before Fix:
- Strategy attempted short sales → Rejected by IBKR
- Strategy attempted stop orders → Rejected by IBKR  
- Prices had excessive precision → Rejected by IBKR
- No trades executed, only error messages

### After Fix:
- Strategy generates long signals only for crypto ✅
- No stop orders attempted for crypto ✅
- Prices properly rounded to 0.01 precision ✅
- Actual crypto trades can execute ✅

## 🚀 Ready Commands

### ETH Long-Only Backtesting:
```bash
python3 src/main.py ETH --strategy groq --backtest-only --duration "3 M"
```

### ETH Paper Trading (24/7):
```bash
python3 src/main.py ETH --strategy groq --paper --watch --duration "3 D"
```

### ETH Watch Mode (Signals Only):
```bash
python3 src/main.py ETH --strategy groq --watch --duration "1 D"
```

**Note**: Remember to restart any running processes to pick up these fixes! 