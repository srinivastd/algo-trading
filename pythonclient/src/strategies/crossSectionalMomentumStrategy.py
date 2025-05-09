import pandas as pd
import numpy as np
from datetime import timedelta
from typing import Dict, List
from .base import StrategyBase
import logging

logger = logging.getLogger(__name__)

class CrossSectionalMomentumStrategy(StrategyBase):
    """Strategy based on cross-sectional momentum across a panel of assets"""
    
    def __init__(self, risk_per_trade: float = 500, rebalance_period_days: int = 30):
        super().__init__("Cross Sectional Momentum Strategy")
        self.risk_per_trade = risk_per_trade
        self.rebalance_period_days = rebalance_period_days
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate momentum indicators for the strategy"""
        # Calculate returns for different lookback periods
        df['return_1d'] = df['close'].pct_change(periods=1)
        df['return_5d'] = df['close'].pct_change(periods=5)
        df['return_20d'] = df['close'].pct_change(periods=20)
        
        # Calculate volatility
        df['volatility'] = df['return_1d'].rolling(window=20).std()
        
        # Calculate momentum score (weighted combination of returns)
        df['momentum_score'] = (
            0.5 * df['return_20d'] +  # Long-term momentum
            0.3 * df['return_5d'] +   # Medium-term momentum
            0.2 * df['return_1d']     # Short-term momentum
        )
        
        # Calculate relative strength
        df['relative_strength'] = df['momentum_score'].rolling(window=20).mean()
        
        # Calculate volume trend
        df['volume_ma'] = df['volume'].rolling(window=20).mean()
        df['volume_trend'] = df['volume'] / df['volume_ma']
        
        # Calculate price trend
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['sma_50'] = df['close'].rolling(window=50).mean()
        df['trend'] = np.where(df['sma_20'] > df['sma_50'], 1, -1)
        
        logger.debug("Calculated momentum indicators")
        return df
        
    def calculate_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        # Assume df is a multi-index DataFrame with assets as columns and dates as index
        df['momentum'] = df['close'].pct_change(periods=20) * 100  # 20-day momentum in percentage
        df['rank'] = df['momentum'].rank(ascending=False, method='dense')
        return df
        
    def detect_cross_dependence(self, df: pd.DataFrame) -> pd.Series:
        # Simple cross-sectional dependence check using correlation of momentum
        momentum_corrs = df['momentum'].corr()
        avg_corr = momentum_corrs.mean().mean() - np.diag(momentum_corrs).mean()  # Exclude self-correlation
        return pd.Series(avg_corr, index=df.index.unique())
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        
        # Calculate momentum indicators
        df = self.calculate_indicators(df)
        
        # Generate signals based on momentum score and trend
        for i in range(20, len(df)):
            if df['momentum_score'].iloc[i] > 0 and df['trend'].iloc[i] > 0:
                if df['volume_trend'].iloc[i] > 1.2:  # Volume confirmation
                    signals.loc[signals.index[i], 'signal'] = 1
                    logger.debug(f"Long signal at {df.index[i]}, momentum: {df['momentum_score'].iloc[i]:.2f}")
            elif df['momentum_score'].iloc[i] < 0 and df['trend'].iloc[i] < 0:
                if df['volume_trend'].iloc[i] > 1.2:  # Volume confirmation
                    signals.loc[signals.index[i], 'signal'] = -1
                    logger.debug(f"Short signal at {df.index[i]}, momentum: {df['momentum_score'].iloc[i]:.2f}")
                    
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
                
                # Dynamic stop loss based on volatility
                volatility = df['volatility'].iloc[i]
                stop_loss = entry_price * (1 - 2 * volatility) if is_long else entry_price * (1 + 2 * volatility)
                target = entry_price * (1 + 4 * volatility) if is_long else entry_price * (1 - 4 * volatility)
                
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
                
                # Check for exit conditions
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