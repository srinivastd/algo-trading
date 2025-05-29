"""
Trading strategy implementations for stock analysis.
Version 1.4
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from .base import StrategyBase
import logging
from pytz import timezone
from ibkr.connection import IBKRConnection
import asyncio
from ib_insync import MarketOrder, StopOrder, LimitOrder
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

class GroqRecommendationsStrategy(StrategyBase):
    """Strategy based on supply/demand zones with optimized ADX and duration, with paper trading support"""
    
    def __init__(self, risk_per_trade: float = 3000, max_duration_hours: float = 12.0, paper_trading: bool = True,
                 stop_loss_pct: float = 1.0, target_pct: float = 4.0, bar_size: str = "1 hour", duration: str = "1 M"):
        super().__init__("Groq Recommendations Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        self.paper_trading = paper_trading
        self.stop_loss_pct = stop_loss_pct  # Stop loss percentage
        self.target_pct = target_pct  # Target profit percentage
        self.ib_client = None  # Will be set by main.py
        self.bar_size = bar_size  # Time frame of bars (e.g., "1 hour")
        self.duration = duration  # Backtest duration (e.g., "1 M")
        # Initialize ChromaDB client and collection
        self.chroma_client = chromadb.PersistentClient(path="./chroma_db")
        self.collection = self.chroma_client.get_or_create_collection(
            name="backtest_results",
            embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        )
        logger.info(f"Initialized {self.name} with risk_per_trade=${risk_per_trade}, max_duration={max_duration_hours}h, "
                   f"stop_loss={stop_loss_pct}%, target={target_pct}%, paper_trading={paper_trading}, "
                   f"bar_size={bar_size}, duration={duration}")

    def set_ib_client(self, ib_client):
        """Set the IBKR client for trading"""
        self.ib_client = ib_client
        logger.info("IBKR client set for trading")

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        eastern = timezone('America/New_York')
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert(eastern)
        else:
            df.index = df.index.tz_convert(eastern)
        
        df['range'] = df['high'] - df['low']
        df['volatility'] = df['range'].rolling(window=5).mean()
        df['volume_change'] = df['volume'].pct_change().abs()
        
        df['consolidation'] = (df['range'] < 1.2 * df['volatility'].shift(1)) & (df['volume_change'] < 0.2)
        df['consolidation_count'] = df['consolidation'].rolling(window=2).sum()
        df['valid_consolidation'] = df['consolidation_count'] >= 2
        
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
                df['sma_20'].iloc[i] > df['sma_50'].iloc[i] * 1.01 and
                df['adx'].iloc[i] > 20):
                supply_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
                logger.debug(f"Supply zone at {df.index[i]}, high: {df['high'].iloc[i]:.2f}")
            
            elif (df['valid_consolidation'].iloc[i] and 
                  df['close'].iloc[i] < df['close'].iloc[i-5:i].min() and
                  df['sma_20'].iloc[i] < df['sma_50'].iloc[i] * 0.99 and
                  df['adx'].iloc[i] > 20):
                demand_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
                logger.debug(f"Demand zone at {df.index[i]}, low: {df['low'].iloc[i]:.2f}")
                
        logger.info(f"Found {len(supply_zones)} supply, {len(demand_zones)} demand zones")
        return supply_zones, demand_zones
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        
        supply_zones, demand_zones = self.identify_zones(df)
        
        for i in range(1, len(df) - 1):
            current_price = df['close'].iloc[i]
            
            for zone_time, zone_high, zone_low in supply_zones:
                if ((df.index[i] - zone_time) < timedelta(hours=24) and 
                    df['close'].iloc[i] > zone_high and
                    df['volume'].iloc[i] > df['volume'].iloc[i-1] * 1.2):
                    signals.loc[signals.index[i], 'signal'] = -1
                    logger.debug(f"Short signal at {df.index[i]}, price: {current_price:.2f}")
                    break
                    
            for zone_time, zone_high, zone_low in demand_zones:
                if ((df.index[i] - zone_time) < timedelta(hours=24) and 
                    df['close'].iloc[i] < zone_low and
                    df['volume'].iloc[i] > df['volume'].iloc[i-1] * 1.2):
                    signals.loc[signals.index[i], 'signal'] = 1
                    logger.debug(f"Long signal at {df.index[i]}, price: {current_price:.2f}")
                    break
                    
        logger.info(f"Generated {len(signals[signals['signal'] != 0])} signals")
        return signals
        
    async def generate_realtime_signals(self, df: pd.DataFrame) -> int:
        """Generate a single signal based on the latest data in real-time."""
        if len(df) < 5:
            return 0
        
        df = self.calculate_indicators(df)
        supply_zones, demand_zones = self.identify_zones(df)
        current_price = df['close'].iloc[-1]
        current_time = df.index[-1]
        
        for zone_time, zone_high, zone_low in supply_zones:
            if ((current_time - zone_time) < timedelta(hours=24) and 
                current_price > zone_high and
                df['volume'].iloc[-1] > df['volume'].iloc[-2] * 1.2):
                logger.debug(f"Realtime short signal at {current_time}, price: {current_price:.2f}")
                return -1
                
        for zone_time, zone_high, zone_low in demand_zones:
            if ((current_time - zone_time) < timedelta(hours=24) and 
                current_price < zone_low and
                df['volume'].iloc[-1] > df['volume'].iloc[-2] * 1.2):
                logger.debug(f"Realtime long signal at {current_time}, price: {current_price:.2f}")
                return 1
                
        return 0
        
    async def execute_trade(self, symbol: str, signal: int, entry_price: float, shares: int, stop_loss: float, target: float) -> Dict:
        if not self.ib_client:
            logger.error("IBKR client not initialized")
            return {'status': 'error', 'message': 'No IBKR client'}

        order_id = await self.ib_client.place_order(
            symbol=symbol,
            action='BUY' if signal == 1 else 'SELL',
            quantity=shares,
            order_type='LMT',
            limit_price=entry_price
        )
        
        if order_id:
            # Place stop-loss and target orders
            stop_order_id = await self.ib_client.place_order(
                symbol=symbol,
                action='SELL' if signal == 1 else 'BUY',
                quantity=shares,
                order_type='STP',
                aux_price=stop_loss,
                parent_id=order_id
            )
            target_order_id = await self.ib_client.place_order(
                symbol=symbol,
                action='SELL' if signal == 1 else 'BUY',
                quantity=shares,
                order_type='LMT',
                limit_price=target,
                parent_id=order_id
            )
            logger.info(f"Placed {'paper' if self.paper_trading else 'live'} trade: {('Long' if signal == 1 else 'Short')} {symbol}, Entry: ${entry_price:.2f}, Stop: ${stop_loss:.2f}, Target: ${target:.2f}")
            return {
                'status': 'success',
                'order_id': order_id,
                'stop_order_id': stop_order_id,
                'target_order_id': target_order_id
            }
        else:
            return {
                'status': 'error',
                'message': 'Failed to place order'
            }

    async def backtest(self, df: pd.DataFrame, initial_capital: float = 100000, symbol: str = 'MSFT') -> Dict:
        """Run a backtest on historical data and store results in ChromaDB"""
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []
        active_orders = {}

        for i in range(1, len(df) - 1):
            if signals['signal'].iloc[i] != 0 and position == 0:
                entry_price = df['close'].iloc[i]
                entry_time = df.index[i]
                is_long = signals['signal'].iloc[i] == 1
                
                # Calculate stop loss and target based on configured percentages
                stop_loss = entry_price * (1 - self.stop_loss_pct/100) if is_long else entry_price * (1 + self.stop_loss_pct/100)
                target = entry_price * (1 + self.target_pct/100) if is_long else entry_price * (1 - self.target_pct/100)
                
                risk_per_share = abs(entry_price - stop_loss)
                if risk_per_share <= 0:
                    continue
                    
                shares = int(self.risk_per_trade // risk_per_share)
                if shares <= 0 or (shares * entry_price) > (0.95 * capital):
                    shares = int((0.95 * capital) // entry_price)
                    if shares <= 0:
                        continue
                    risk_per_share = entry_price * 0.01
                    self.risk_per_trade = shares * risk_per_share
                
                # Attempt paper trading if enabled and IB client is available
                if self.ib_client and self.paper_trading:
                    trade_result = await self.execute_trade(symbol, signals['signal'].iloc[i], entry_price, shares, stop_loss, target)
                    if trade_result['status'] == 'success':
                        position = shares if is_long else -shares
                        trades.append({
                            'entry_date': entry_time,
                            'entry_price': entry_price,
                            'shares': abs(shares),
                            'stop_loss': stop_loss,
                            'target': target,
                            'type': 'LONG' if is_long else 'SHORT',
                            'order_id': trade_result['order_id']
                        })
                        active_orders[trade_result['order_id']] = {
                            'shares': shares,
                            'is_long': is_long,
                            'stop_order_id': trade_result['stop_order_id'],
                            'target_order_id': trade_result['target_order_id']
                        }
                        logger.debug(f"Entered {trades[-1]['type']} at ${entry_price:.2f}, shares: {shares} via paper trading")
                    else:
                        logger.warning(f"Paper trade failed: {trade_result['message']}. Falling back to simulation.")
                        # Fall back to simulation if paper trading fails
                        position = shares if is_long else -shares
                        trades.append({
                            'entry_date': entry_time,
                            'entry_price': entry_price,
                            'shares': abs(shares),
                            'stop_loss': stop_loss,
                            'target': target,
                            'type': 'LONG' if is_long else 'SHORT'
                        })
                        logger.debug(f"Simulated entry {trades[-1]['type']} at ${entry_price:.2f}, shares: {shares}")
                else:
                    # Default to simulation
                    position = shares if is_long else -shares
                    trades.append({
                        'entry_date': entry_time,
                        'entry_price': entry_price,
                        'shares': abs(shares),
                        'stop_loss': stop_loss,
                        'target': target,
                        'type': 'LONG' if is_long else 'SHORT'
                    })
                    logger.debug(f"Simulated entry {trades[-1]['type']} at ${entry_price:.2f}, shares: {shares}")

            elif position != 0:
                current_trade = trades[-1]
                current_price = df['close'].iloc[i]
                current_time = df.index[i]
                time_held = (current_time - current_trade['entry_date']).total_seconds() / 3600

                # Handle paper trading exits if order_id exists
                if 'order_id' in current_trade and self.ib_client and self.paper_trading:
                    order_status = await self.ib_client.get_order_status(current_trade['order_id'])
                    if order_status['filled']:
                        exit_price = order_status['avg_fill_price']
                        is_long = current_trade['type'] == 'LONG'
                        profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if is_long else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                        capital += profit
                        position = 0
                        current_trade.update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                        logger.debug(f"Exited {current_trade['type']} at ${exit_price:.2f}, profit: ${profit:.2f} via paper trading")
                        del active_orders[current_trade['order_id']]
                    elif time_held >= self.max_duration_hours:
                        await self.ib_client.cancel_order(current_trade['order_id'])
                        exit_price = current_price
                        is_long = current_trade['type'] == 'LONG'
                        profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if is_long else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                        capital += profit
                        position = 0
                        current_trade.update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                        logger.debug(f"Exited {current_trade['type']} due to duration at ${exit_price:.2f}, profit: ${profit:.2f}")
                        del active_orders[current_trade['order_id']]
                else:
                    # Default to simulation
                    if time_held >= self.max_duration_hours:
                        exit_price = current_price
                        profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if current_trade['type'] == 'LONG' else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                        capital += profit
                        position = 0
                        current_trade.update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                        logger.debug(f"Exited {current_trade['type']} after {time_held:.2f}h at ${exit_price:.2f}, profit: ${profit:.2f}")
                        continue
                    
                    if current_trade['type'] == 'LONG':
                        if current_price <= current_trade['stop_loss'] or current_price >= current_trade['target']:
                            exit_price = current_trade['stop_loss'] if current_price <= current_trade['stop_loss'] else current_trade['target']
                            profit = (exit_price - current_trade['entry_price']) * current_trade['shares']
                            capital += profit
                            position = 0
                            current_trade.update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                            logger.debug(f"Exited LONG at ${exit_price:.2f}, profit: ${profit:.2f}")
                    else:
                        if current_price >= current_trade['stop_loss'] or current_price <= current_trade['target']:
                            exit_price = current_trade['stop_loss'] if current_price >= current_trade['stop_loss'] else current_trade['target']
                            profit = (current_trade['entry_price'] - exit_price) * current_trade['shares']
                            capital += profit
                            position = 0
                            current_trade.update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit})
                            logger.debug(f"Exited SHORT at ${exit_price:.2f}, profit: ${profit:.2f}")

        # Close any open position at the end
        if position != 0:
            current_trade = trades[-1]
            exit_price = df['close'].iloc[-1]
            profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if current_trade['type'] == 'LONG' else (current_trade['entry_price'] - exit_price) * current_trade['shares']
            capital += profit
            current_trade.update({
                'exit_date': df.index[-1],
                'exit_price': exit_price,
                'profit': profit
            })
            logger.debug(f"Closed final position at ${exit_price:.2f}, profit: ${profit:.2f}")

        # Calculate success rate and total profit for ChromaDB storage
        total_trades = len(trades)
        successful_trades = sum(1 for trade in trades if trade.get('profit', 0) > 0)
        success_rate = (successful_trades / total_trades * 100) if total_trades > 0 else 0
        total_profit = sum(trade.get('profit', 0) for trade in trades)

        # Enhanced metadata with additional features
        metadatas = [{
            "symbol": symbol,
            "total_profit": total_profit,
            "success_rate": success_rate,
            "initial_capital": initial_capital,
            "final_capital": capital,
            "total_trades": total_trades,
            "trades": str(trades),  # Detailed trade data
            "risk_per_trade": self.risk_per_trade,
            "stop_loss_pct": self.stop_loss_pct,
            "target_pct": self.target_pct,
            "volatility": float(df['volatility'].iloc[-1]) if not df['volatility'].empty else None,
            "adx": float(df['adx'].iloc[-1]) if not df['adx'].empty else None,
            "sma_20": float(df['sma_20'].iloc[-1]) if not df['sma_20'].empty else None,
            "sma_50": float(df['sma_50'].iloc[-1]) if not df['sma_50'].empty else None,
            "valid_consolidation": bool(df['valid_consolidation'].iloc[-1]) if not df['valid_consolidation'].empty else False,
            "volume_change": float(df['volume_change'].iloc[-1]) if not df['volume_change'].empty else None,
            "bar_size": self.bar_size,
            "duration": self.duration,
            "paper_trading": self.paper_trading
        }]

        # Store results in ChromaDB
        result_id = f"{symbol}_{str(pd.Timestamp.now())}"
        self.collection.add(
            documents=[f"Backtest result for {symbol}: {total_profit} profit, {success_rate}% success rate"],
            metadatas=metadatas[0],  # Pass the first (and only) metadata dictionary
            ids=[result_id]
        )
        logger.info(f"Stored backtest results for {symbol} in ChromaDB with ID {result_id}")

        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': total_trades,
            'trades': trades
        }