"""
Breakout Pullback Strategy
Implements a breakout pullback strategy with candlestick confirmation.
"""
import pandas as pd
from typing import Dict
from ta.momentum import RSIIndicator
from .base import StrategyBase

class BreakoutPullbackStrategy(StrategyBase):
    """Breakout Pullback Strategy with Candlestick Confirmation"""
    
    def __init__(self, risk_per_trade: float = 1000):
        super().__init__("Breakout Pullback Strategy")
        self.risk_per_trade = risk_per_trade
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate technical indicators"""
        df['rsi'] = RSIIndicator(df['close']).rsi()
        df['ma_20'] = df['close'].rolling(window=20).mean()
        df['ma_50'] = df['close'].rolling(window=50).mean()
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        return df
        
    def is_bullish_engulfing(self, df: pd.DataFrame, i: int) -> bool:
        """Check for bullish engulfing pattern"""
        if i > 0 and df['open'].iloc[i] < df['close'].iloc[i-1] and df['close'].iloc[i] > df['open'].iloc[i-1]:
            return df['close'].iloc[i] > df['open'].iloc[i]
        return False
        
    def is_hammer(self, df: pd.DataFrame, i: int) -> bool:
        """Check for hammer pattern"""
        body = abs(df['close'].iloc[i] - df['open'].iloc[i])
        lower_wick = df['low'].iloc[i] - min(df['open'].iloc[i], df['close'].iloc[i])
        upper_wick = max(df['open'].iloc[i], df['close'].iloc[i]) - df['high'].iloc[i]
        return body > 0 and lower_wick > 2 * body and upper_wick < body / 2
        
    def is_consolidation(self, df: pd.DataFrame, start: int, end: int) -> bool:
        """Check for consolidation pattern"""
        range_high = df['high'][start:end].max()
        range_low = df['low'][start:end].min()
        return (range_high - range_low) / df['close'].iloc[start] < 0.02
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate trading signals"""
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0  # 0: no signal, 1: buy, -1: sell
        
        for i in range(5, len(df)-1):
            if (self.is_consolidation(df, i-5, i) and 
                df['close'].iloc[i] > df['high'].iloc[i-5:i].max() and 
                df['close'].iloc[i] > df['ma_50'].iloc[i] and 
                (self.is_bullish_engulfing(df, i) or self.is_hammer(df, i)) and 
                30 <= df['rsi'].iloc[i] <= 70 and 
                df['volume'].iloc[i] > 1.5 * df['volume_ma'].iloc[i]):
                
                signals.iloc[i]['signal'] = 1
                
        return signals
        
    def backtest(self, df: pd.DataFrame, initial_capital: float = 100000) -> Dict:
        """Run backtest on historical data"""
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []
        
        for i in range(len(df)):
            if signals['signal'].iloc[i] == 1 and position == 0:
                # Entry
                entry_price = df['close'].iloc[i]
                stop_loss = df['low'].iloc[i-5:i].min()
                target = entry_price + (entry_price - stop_loss) * 2
                
                risk_per_share = entry_price - stop_loss
                if risk_per_share <= 0:
                    continue
                    
                shares = int(self.risk_per_trade // risk_per_share)
                if shares <= 0:
                    continue
                    
                position = shares
                trades.append({
                    'entry_date': df.index[i],
                    'entry_price': entry_price,
                    'shares': shares,
                    'stop_loss': stop_loss,
                    'target': target
                })
                
            elif position > 0:
                # Check exit conditions
                current_trade = trades[-1]
                if (df['high'].iloc[i] >= current_trade['target'] or 
                    df['low'].iloc[i] <= current_trade['stop_loss']):
                    exit_price = (current_trade['target'] if df['high'].iloc[i] >= current_trade['target']
                                else current_trade['stop_loss'])
                    profit = (exit_price - current_trade['entry_price']) * current_trade['shares']
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