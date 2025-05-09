import pandas as pd
import numpy as np
from datetime import timedelta, time
from typing import Dict, List
from .base import StrategyBase
import logging
from pytz import timezone

logger = logging.getLogger(__name__)


class VWAPTrendTradingStrategy(StrategyBase):
    def __init__(self, risk_per_trade: float = 500, threshold: float = 0.01,
                 max_duration_hours: float = 6.5, commission_per_share: float = 0.0005):
        super().__init__("VWAP Trend Trading Strategy")
        self.risk_per_trade = risk_per_trade
        self.threshold = threshold
        self.max_duration_hours = max_duration_hours
        self.commission_per_share = commission_per_share

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate VWAP for regular trading hours (9:30 AM - 4:00 PM ET)"""
        # Convert index to ET timezone
        eastern = timezone('America/New_York')
        df.index = df.index.tz_convert(eastern)
        df['time'] = df.index.time
        df['is_rth'] = df['time'].apply(lambda t: time(9, 30) <= t <= time(16, 0))
        df_rth = df[df['is_rth']].copy()
        
        # Calculate VWAP
        df_rth['typical_price'] = (df_rth['high'] + df_rth['low'] + df_rth['close']) / 3
        df_rth['price_volume'] = df_rth['typical_price'] * df_rth['volume']  # Calculate price_volume before grouping
        df_rth['cum_volume'] = df_rth.groupby(df_rth.index.date)['volume'].cumsum()
        df_rth['cum_price_volume'] = df_rth.groupby(df_rth.index.date)['price_volume'].cumsum()
        df_rth['vwap'] = df_rth['cum_price_volume'] / df_rth['cum_volume']
        
        # Merge VWAP back to original DataFrame
        df = df.drop(columns=['vwap'], errors='ignore')
        df = df.join(df_rth[['vwap']], how='left')
        df['vwap'] = df['vwap'].ffill()
        df.drop(columns=['time', 'is_rth'], inplace=True)
        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.calculate_indicators(df)
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        position = 0

        for i in range(1, len(df)):
            current_time = df.index[i].time()
            prev_close = df['close'].iloc[i - 1]
            current_vwap = df['vwap'].iloc[i - 1]

            if not (time(9, 30) <= current_time <= time(16, 0)):
                continue

            # Entry window: 9:31 AM to 9:35 AM if no position
            if time(9, 31) <= current_time <= time(9, 35) and position == 0:
                if pd.notna(prev_close) and pd.notna(current_vwap):
                    if prev_close > current_vwap:
                        signals.iloc[i] = 1
                        position = 1
                    elif prev_close < current_vwap:
                        signals.iloc[i] = -1
                        position = -1

            # Reversal signals if holding a position
            elif position != 0:
                if pd.notna(prev_close) and pd.notna(current_vwap):
                    if position == 1 and prev_close < current_vwap:
                        signals.iloc[i] = -1
                        position = -1
                    elif position == -1 and prev_close > current_vwap:
                        signals.iloc[i] = 1
                        position = 1

            # EOD exit
            if current_time == time(16, 0) and position != 0:
                signals.iloc[i] = -position
                position = 0

        return signals

    def backtest(self, df: pd.DataFrame, initial_capital: float = 25000) -> Dict:
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        shares = 0
        trades = []

        for i in range(1, len(df)):
            current_price = df['close'].iloc[i]
            current_time = df.index[i].time()
            current_vwap = df['vwap'].iloc[i]
            signal = signals['signal'].iloc[i]

            if not (time(9, 30) <= current_time <= time(16, 0)):
                continue

            if position != 0:
                trade = trades[-1]
                entry_time = pd.to_datetime(trade['entry_date'])
                duration = (df.index[i] - entry_time).total_seconds() / 3600
                stop_loss = trade['stop_loss']
                target = trade['target']

                exit_price = None
                if position > 0 and (current_price <= stop_loss or current_price >= target or duration >= self.max_duration_hours):
                    exit_price = min(max(current_price, stop_loss), target)
                elif position < 0 and (current_price >= stop_loss or current_price <= target or duration >= self.max_duration_hours):
                    exit_price = max(min(current_price, stop_loss), target)

                if exit_price or signal == -position or current_time == time(16, 0):
                    if not exit_price:
                        exit_price = current_price
                    profit = (exit_price - trade['entry_price']) * shares if position > 0 else (trade['entry_price'] - exit_price) * shares
                    commission = 2 * shares * self.commission_per_share
                    profit -= commission
                    capital += profit
                    trade.update({
                        'exit_date': df.index[i],
                        'exit_price': exit_price,
                        'profit': profit,
                        'commission': commission
                    })
                    position = 0
                    shares = 0

            if signal != 0 and position == 0:
                risk_price = abs(current_price - (current_vwap * (1 - self.threshold if signal > 0 else 1 + self.threshold)))
                if risk_price == 0:
                    continue
                shares = int(min(self.risk_per_trade / risk_price, capital // current_price))
                if shares == 0:
                    continue

                stop_loss = current_vwap * (1 - self.threshold) if signal > 0 else current_vwap * (1 + self.threshold)
                target = current_vwap * (1 + self.threshold) if signal > 0 else current_vwap * (1 - self.threshold)

                position = signal
                trades.append({
                    'entry_date': df.index[i],
                    'entry_price': current_price,
                    'shares': shares,
                    'type': 'LONG' if signal > 0 else 'SHORT',
                    'stop_loss': stop_loss,
                    'target': target
                })

        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': len(trades),
            'trades': trades
        }