"""
Supply and Demand Surge Strategy
Implements a strategy based on supply/demand zones and volume surges.
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from .base import StrategyBase

class SupplyDemandSurgeStrategy(StrategyBase):
    """Supply and Demand Surge Strategy with Volume Confirmation"""
    
    def __init__(self, risk_per_trade: float = 1000):
        super().__init__("Supply and Demand Surge Strategy")
        self.risk_per_trade = risk_per_trade
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate technical indicators"""
        # Calculate volatility and consolidation
        df['range'] = df['high'] - df['low']
        df['volatility'] = df['range'].rolling(window=5).mean()
        df['volume_change'] = df['volume'].pct_change().abs()
        df['consolidation'] = (df['range'] < df['volatility'].shift(1)) & (df['volume_change'] < 0.1)
        
        # Calculate additional indicators
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        df['price_ma'] = df['close'].rolling(window=20).mean()
        
        return df
        
    def identify_zones(self, df: pd.DataFrame) -> Tuple[List[Tuple], List[Tuple]]:
        """Identify supply and demand zones"""
        supply_zones = []
        demand_zones = []
        
        for i in range(5, len(df) - 5):
            if df['consolidation'].iloc[i] and df['close'].iloc[i] > df['close'].iloc[i-5:i].max():
                # Potential supply zone after a peak
                supply_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
            elif df['consolidation'].iloc[i] and df['close'].iloc[i] < df['close'].iloc[i-5:i].min():
                # Potential demand zone after a dip
                demand_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
                
        return supply_zones, demand_zones
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate trading signals"""
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0  # 0: no signal, 1: buy, -1: sell
        
        supply_zones, demand_zones = self.identify_zones(df)
        
        for i in range(1, len(df) - 1):
            current_price = df['close'].iloc[i]
            
            # Check for breakout from consolidation near zones
            for zone_time, zone_high, zone_low in supply_zones:
                if (abs(df.index[i] - zone_time) < timedelta(hours=5) and 
                    df['high'].iloc[i] > zone_high and 
                    df['volume'].iloc[i] > df['volume'].iloc[i-1]):
                    signals.loc[signals.index[i], 'signal'] = -1  # Short signal
                    break
                    
            for zone_time, zone_high, zone_low in demand_zones:
                if (abs(df.index[i] - zone_time) < timedelta(hours=5) and 
                    df['low'].iloc[i] < zone_low and 
                    df['volume'].iloc[i] > df['volume'].iloc[i-1]):
                    signals.loc[signals.index[i], 'signal'] = 1  # Long signal
                    break
                    
        return signals
        
    def backtest(self, df: pd.DataFrame, initial_capital: float = 100000) -> Dict:
        """Run backtest on historical data"""
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []
        
        for i in range(1, len(df) - 1):
            if signals['signal'].iloc[i] != 0 and position == 0:
                # Entry
                entry_price = df['close'].iloc[i]
                is_long = signals['signal'].iloc[i] == 1
                
                if is_long:
                    # Long trade
                    stop_loss = entry_price * 0.98  # 2% below entry
                    target = entry_price * 1.04  # 4% above entry (2:1 reward-risk)
                else:
                    # Short trade
                    stop_loss = entry_price * 1.02  # 2% above entry
                    target = entry_price * 0.96  # 4% below entry (2:1 reward-risk)
                
                risk_per_share = abs(entry_price - stop_loss)
                if risk_per_share <= 0:
                    continue
                    
                shares = int(self.risk_per_trade // risk_per_share)
                if shares <= 0:
                    continue
                    
                position = shares if is_long else -shares
                trades.append({
                    'entry_date': df.index[i],
                    'entry_price': entry_price,
                    'shares': abs(shares),
                    'stop_loss': stop_loss,
                    'target': target,
                    'type': 'LONG' if is_long else 'SHORT'
                })
                
            elif position != 0:
                # Check exit conditions
                current_trade = trades[-1]
                current_price = df['close'].iloc[i]
                
                if current_trade['type'] == 'LONG':
                    if current_price <= current_trade['stop_loss'] or current_price >= current_trade['target']:
                        exit_price = current_trade['stop_loss'] if current_price <= current_trade['stop_loss'] else current_trade['target']
                        profit = (exit_price - current_trade['entry_price']) * current_trade['shares']
                        capital += profit
                        position = 0
                        trades[-1].update({
                            'exit_date': df.index[i],
                            'exit_price': exit_price,
                            'profit': profit
                        })
                else:  # SHORT
                    if current_price >= current_trade['stop_loss'] or current_price <= current_trade['target']:
                        exit_price = current_trade['stop_loss'] if current_price >= current_trade['stop_loss'] else current_trade['target']
                        profit = (current_trade['entry_price'] - exit_price) * current_trade['shares']
                        capital += profit
                        position = 0
                        trades[-1].update({
                            'exit_date': df.index[i],
                            'exit_price': exit_price,
                            'profit': profit
                        })
        
        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': len(trades),
            'trades': trades
        } 