import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from .base import StrategyBase
import logging

logger = logging.getLogger(__name__)

class GroqBreakOfStructureStrategy(StrategyBase):
    """Strategy based on supply/demand zones with break of structure, confirmation, and tight stop-loss"""
    
    def __init__(self, risk_per_trade: float = 500, max_duration_hours: float = 12.0):
        super().__init__("Groq Break of Structure Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df['range'] = df['high'] - df['low']
        df['consolidation'] = df['range'] < df['range'].shift(1)
        df['consolidation_count'] = df['consolidation'].rolling(window=2).sum()
        df['valid_consolidation'] = df['consolidation_count'] >= 2
        
        # Add SMA for trend direction
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['sma_50'] = df['close'].rolling(window=50).mean()
        
        # Calculate ADX (14-period)
        df['high_low'] = df['high'] - df['low']
        df['high_prev_close'] = abs(df['high'] - df['close'].shift(1))
        df['low_prev_close'] = abs(df['low'] - df['close'].shift(1))
        df['tr'] = df[['high_low', 'high_prev_close', 'low_prev_close']].max(axis=1)
        df['atr'] = df['tr'].rolling(window=14).mean()
        df['plus_dm'] = np.where((df['high'] > df['high'].shift(1)) & (df['high'] - df['high'].shift(1) > df['low'].shift(1) - df['low']), df['high'] - df['high'].shift(1), 0)
        df['minus_dm'] = np.where((df['low'] < df['low'].shift(1)) & (df['low'].shift(1) - df['low'] > df['high'] - df['high'].shift(1)), df['low'].shift(1) - df['low'], 0)
        df['plus_di'] = 100 * df['plus_dm'].rolling(window=14).mean() / df['atr']
        df['minus_di'] = 100 * df['minus_dm'].rolling(window=14).mean() / df['atr']
        df['dx'] = 100 * np.abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'])
        df['adx'] = df['dx'].rolling(window=14).mean()
        
        return df
        
    def identify_zones(self, df: pd.DataFrame) -> Tuple[List[Tuple], List[Tuple]]:
        supply_zones = []
        demand_zones = []
        
        for i in range(5, len(df) - 5):
            if (df['valid_consolidation'].iloc[i] and 
                df['close'].iloc[i] > df['close'].iloc[i-5:i].max() and
                df['high'].iloc[i] > df['high'].iloc[i-10:i-5].max() and
                df['sma_20'].iloc[i] > df['sma_50'].iloc[i] and  # Ensure uptrend
                df['adx'].iloc[i] > 20):  # Ensure trend strength
                supply_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
                logger.debug(f"Supply zone at {df.index[i]}, high: {df['high'].iloc[i]:.2f}")
            
            elif (df['valid_consolidation'].iloc[i] and 
                  df['close'].iloc[i] < df['close'].iloc[i-5:i].min() and
                  df['low'].iloc[i] < df['low'].iloc[i-10:i-5].min() and
                  df['sma_20'].iloc[i] < df['sma_50'].iloc[i] and  # Ensure downtrend
                  df['adx'].iloc[i] > 20):
                demand_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
                logger.debug(f"Demand zone at {df.index[i]}, low: {df['low'].iloc[i]:.2f}")
                
        logger.info(f"Found {len(supply_zones)} supply, {len(demand_zones)} demand zones")
        return supply_zones, demand_zones
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        
        supply_zones, demand_zones = self.identify_zones(df)
        
        for i in range(2, len(df) - 1):
            current_price = df['close'].iloc[i]
            prev_close = df['close'].iloc[i-1]
            
            for zone_time, zone_high, zone_low in supply_zones:
                if (abs(df.index[i] - zone_time) < timedelta(hours=24) and 
                    df['close'].iloc[i] > zone_high and
                    prev_close > zone_high and
                    df['volume'].iloc[i] > df['volume'].iloc[i-1] * 1.2):  # Volume confirmation
                    signals.loc[signals.index[i], 'signal'] = -1
                    logger.debug(f"Short at {df.index[i]}, price: {current_price:.2f}")
                    break
                    
            for zone_time, zone_high, zone_low in demand_zones:
                if (abs(df.index[i] - zone_time) < timedelta(hours=24) and 
                    df['close'].iloc[i] < zone_low and
                    prev_close < zone_low and
                    df['volume'].iloc[i] > df['volume'].iloc[i-1] * 1.2):
                    signals.loc[signals.index[i], 'signal'] = 1
                    logger.debug(f"Long at {df.index[i]}, price: {current_price:.2f}")
                    break
                    
        logger.info(f"Generated {len(signals[signals['signal'] != 0])} signals")
        return signals
        
    def backtest(self, df: pd.DataFrame, initial_capital: float = 100000) -> Dict:
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []
        
        for i in range(1, len(df) - 1):
            if signals['signal'].iloc[i] != 0 and position == 0:
                entry_price = df['close'].iloc[i]
                entry_time = df.index[i]
                is_long = signals['signal'].iloc[i] == 1
                
                stop_loss = entry_price * 0.99 if is_long else entry_price * 1.01  # Adjusted to 1%
                target = entry_price * 1.02 if is_long else entry_price * 0.98  # 2:1 reward-to-risk
                
                risk_per_share = abs(entry_price - stop_loss)
                if risk_per_share <= 0:
                    continue
                    
                shares = int(self.risk_per_trade // risk_per_share)
                if shares <= 0:
                    continue
                    
                position = shares if is_long else -shares
                trades.append({
                    'entry_date': entry_time,
                    'entry_price': entry_price,
                    'shares': abs(shares),
                    'stop_loss': stop_loss,
                    'target': target,
                    'type': 'LONG' if is_long else 'SHORT'
                })
                logger.debug(f"Entered {trades[-1]['type']} at {entry_price:.2f}, shares: {shares}")
                
            elif position != 0:
                current_trade = trades[-1]
                current_price = df['close'].iloc[i]
                current_time = df.index[i]
                
                time_held = (current_time - current_trade['entry_date']).total_seconds() / 3600
                if time_held >= self.max_duration_hours:
                    exit_price = current_price
                    profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if current_trade['type'] == 'LONG' else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                    capital += profit
                    position = 0
                    trades[-1].update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                    logger.debug(f"Exited {current_trade['type']} after {self.max_duration_hours}h at {exit_price:.2f}, profit: ${profit:.2f}")
                    continue
                
                if current_trade['type'] == 'LONG':
                    if current_price <= current_trade['stop_loss'] or current_price >= current_trade['target']:
                        exit_price = current_trade['stop_loss'] if current_price <= current_trade['stop_loss'] else current_trade['target']
                        profit = (exit_price - current_trade['entry_price']) * current_trade['shares']
                        capital += profit
                        position = 0
                        trades[-1].update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                        logger.debug(f"Exited LONG at {exit_price:.2f}, profit: ${profit:.2f}")
                else:
                    if current_price >= current_trade['stop_loss'] or current_price <= current_trade['target']:
                        exit_price = current_trade['stop_loss'] if current_price >= current_trade['stop_loss'] else current_trade['target']
                        profit = (current_trade['entry_price'] - exit_price) * current_trade['shares']
                        capital += profit
                        position = 0
                        trades[-1].update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                        logger.debug(f"Exited SHORT at {exit_price:.2f}, profit: ${profit:.2f}")
        
        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': len(trades),
            'trades': trades
        }