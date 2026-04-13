import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from .base import StrategyBase
import logging

logger = logging.getLogger(__name__)

class GroqBreakOfStructureStrategy(StrategyBase):
    """Strategy based on supply/demand zones with break of structure, confirmation, and tight stop-loss"""
    
    def __init__(self, risk_per_trade: float = 500, max_duration_hours: float = 12.0, paper_trading: bool = True,
                 stop_loss_pct: float = 1.0, target_pct: float = 2.0):
        super().__init__("Groq Break of Structure Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        self.paper_trading = paper_trading
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.ib_client = None
        
    def set_ib_client(self, ib_client):
        """Set the IBKR client for trade execution"""
        self.ib_client = ib_client
        
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
        
    async def generate_realtime_signals(self, df: pd.DataFrame) -> int:
        """Generate a single signal based on the latest data in real-time."""
        if len(df) < 3:
            return 0
        
        df = self.calculate_indicators(df)
        supply_zones, demand_zones = self.identify_zones(df)
        current_price = df['close'].iloc[-1]
        current_time = df.index[-1]
        prev_close = df['close'].iloc[-2]
        
        # Check for break of structure signals
        for zone_time, zone_high, zone_low in supply_zones:
            if ((current_time - zone_time) < timedelta(hours=24) and 
                current_price > zone_high and
                prev_close > zone_high and
                df['volume'].iloc[-1] > df['volume'].iloc[-2] * 1.2):
                logger.debug(f"Realtime short signal at {current_time}, price: {current_price:.2f}")
                return -1
                
        for zone_time, zone_high, zone_low in demand_zones:
            if ((current_time - zone_time) < timedelta(hours=24) and 
                current_price < zone_low and
                prev_close < zone_low and
                df['volume'].iloc[-1] > df['volume'].iloc[-2] * 1.2):
                logger.debug(f"Realtime long signal at {current_time}, price: {current_price:.2f}")
                return 1
                
        return 0
        
    async def generate_exit_signals(self, df: pd.DataFrame, position_type: str) -> int:
        """Generate exit signals for existing positions based on break of structure reversals."""
        if len(df) < 3:
            return 0
            
        df = self.calculate_indicators(df)
        current_price = df['close'].iloc[-1]
        prev_price = df['close'].iloc[-2]
        current_time = df.index[-1]
        
        price_change = (current_price - prev_price) / prev_price
        supply_zones, demand_zones = self.identify_zones(df)
        
        if position_type == 'long':
            # Exit long position conditions for break of structure strategy:
            # 1. Price breaks back below demand zone that was just broken
            # 2. Strong bearish momentum (>1.5% drop)
            # 3. Volume confirmation of reversal
            
            for zone_time, zone_high, zone_low in demand_zones:
                if ((current_time - zone_time) < timedelta(hours=12) and 
                    current_price > zone_low and prev_price > zone_low and
                    current_price < zone_high):  # Price pulled back into zone
                    logger.debug(f"Exit long signal: Price pulled back into demand zone at {current_price:.2f}")
                    return 1  # Exit long
                    
            # Exit on strong bearish momentum
            if price_change < -0.015:  # 1.5% drop
                logger.debug(f"Exit long signal: Strong bearish momentum {price_change:.3f}")
                return 1  # Exit long
                
            # Exit if ADX shows weakening trend
            if 'adx' in df.columns and len(df) >= 5:
                recent_adx = df['adx'].iloc[-3:].mean()
                if recent_adx < 20:  # Weak trend
                    logger.debug(f"Exit long signal: Weakening trend ADX {recent_adx:.2f}")
                    return 1  # Exit long
                    
        elif position_type == 'short':
            # Exit short position conditions:
            # 1. Price breaks back above supply zone that was just broken
            # 2. Strong bullish momentum (>1.5% gain)
            # 3. Volume confirmation of reversal
            
            for zone_time, zone_high, zone_low in supply_zones:
                if ((current_time - zone_time) < timedelta(hours=12) and 
                    current_price < zone_high and prev_price < zone_high and
                    current_price > zone_low):  # Price pulled back into zone
                    logger.debug(f"Exit short signal: Price pulled back into supply zone at {current_price:.2f}")
                    return -1  # Exit short
                    
            # Exit on strong bullish momentum
            if price_change > 0.015:  # 1.5% gain
                logger.debug(f"Exit short signal: Strong bullish momentum {price_change:.3f}")
                return -1  # Exit short
                
            # Exit if ADX shows weakening trend
            if 'adx' in df.columns and len(df) >= 5:
                recent_adx = df['adx'].iloc[-3:].mean()
                if recent_adx < 20:  # Weak trend
                    logger.debug(f"Exit short signal: Weakening trend ADX {recent_adx:.2f}")
                    return -1  # Exit short
        
        return 0  # Hold position
        
    async def execute_trade(self, symbol: str, signal: int, entry_price: float, shares: int, stop_loss: float, target: float) -> Dict:
        """Execute a trade through IBKR"""
        if not self.ib_client:
            logger.error("IBKR client not initialized")
            return {'status': 'error', 'message': 'No IBKR client'}

        entry_res = await self.ib_client.place_order(
            symbol=symbol,
            action='BUY' if signal == 1 else 'SELL',
            quantity=shares,
            order_type='LMT',
            limit_price=entry_price
        )

        if not entry_res.ok or not entry_res.order_id:
            return {
                'status': 'error',
                'message': f"{entry_res.message} [{entry_res.code}]",
                'ib_place_order': entry_res.as_dict(),
            }

        order_id = entry_res.order_id
        bracket_qty = (
            entry_res.quantity_submitted
            if entry_res.quantity_submitted is not None
            else shares
        )
        stop_res = await self.ib_client.place_order(
            symbol=symbol,
            action='SELL' if signal == 1 else 'BUY',
            quantity=bracket_qty,
            order_type='STP',
            aux_price=stop_loss,
            parent_id=order_id
        )
        target_res = await self.ib_client.place_order(
            symbol=symbol,
            action='SELL' if signal == 1 else 'BUY',
            quantity=bracket_qty,
            order_type='LMT',
            limit_price=target,
            parent_id=order_id
        )
        stop_order_id = stop_res.order_id if stop_res.ok else None
        target_order_id = target_res.order_id if target_res.ok else None
        stop_s = f"${stop_loss:.2f}" if stop_loss is not None else "n/a"
        target_s = f"${target:.2f}" if target is not None else "n/a"
        entry_s = f"${entry_price:.2f}" if entry_price is not None else "n/a"
        logger.info(
            f"Placed {'paper' if self.paper_trading else 'live'} trade: "
            f"{('Long' if signal == 1 else 'Short')} {symbol}, Entry: {entry_s}, Stop: {stop_s}, Target: {target_s}"
        )
        out = {
            'status': 'success',
            'order_id': order_id,
            'stop_order_id': stop_order_id,
            'target_order_id': target_order_id
        }
        if not stop_order_id:
            out['stop_order_error'] = stop_res.as_dict()
        if not target_order_id:
            out['target_order_error'] = target_res.as_dict()
        return out