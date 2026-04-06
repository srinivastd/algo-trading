"""
Mean Reversion Strategy for High Volatility Stocks
Designed for stocks like BMNR with high volatility and oversold conditions
"""

import pandas as pd
import numpy as np
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from .base import StrategyBase
import logging
from pytz import timezone
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

class MeanReversionStrategy(StrategyBase):
    """Mean reversion strategy optimized for high volatility stocks"""

    STRATEGY_SLUG = "mean_reversion"

    def __init__(self, risk_per_trade: float = 1000, max_duration_hours: float = 24.0, paper_trading: bool = True,
                 stop_loss_pct: float = 2.0, target_pct: float = 3.0, bar_size: str = "1 hour", duration: str = "1 M",
                 rsi_oversold: float = 30.0, rsi_overbought: float = 70.0, bb_position_threshold: float = 0.2):
        super().__init__("Mean Reversion Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        self.paper_trading = paper_trading
        self.stop_loss_pct = stop_loss_pct  # Wider stops for volatile stocks
        self.target_pct = target_pct  # Realistic targets for mean reversion
        self.ib_client = None
        self.bar_size = bar_size
        self.duration = duration
        self.rsi_oversold = rsi_oversold  # Buy when RSI < 30
        self.rsi_overbought = rsi_overbought  # Sell when RSI > 70
        self.bb_position_threshold = bb_position_threshold  # Bollinger band position for entries

        # Initialize ChromaDB
        self.chroma_client = chromadb.PersistentClient(path="./chroma_db")
        self.collection = self.chroma_client.get_or_create_collection(
            name="backtest_results",
            embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        )
        logger.info(f"Initialized {self.name} for high volatility stocks")

    def set_ib_client(self, ib_client):
        self.ib_client = ib_client

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        eastern = timezone('America/New_York')
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert(eastern)
        else:
            df.index = df.index.tz_convert(eastern)

        # Basic indicators
        df['range'] = df['high'] - df['low']
        df['volatility'] = df['range'].rolling(window=5).mean()

        # RSI for mean reversion signals
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # Bollinger Bands for mean reversion
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['bb_std'] = df['close'].rolling(window=20).std()
        df['bb_upper'] = df['sma_20'] + (df['bb_std'] * 2)
        df['bb_lower'] = df['sma_20'] - (df['bb_std'] * 2)
        df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        # Volume indicators
        df['volume_sma_20'] = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_sma_20']

        # Mean reversion score (opposite of momentum)
        df['mean_reversion_score'] = (
            (50 - abs(df['rsi'] - 50)) / 50 * 0.4 +  # Higher score when RSI is extreme
            (1 - abs(df['bb_position'] - 0.5) * 2) * 0.3 +  # Higher score when near bands
            df['volatility'].rolling(window=20).mean() / df['volatility'].rolling(window=60).mean() * 0.3
        ).clip(0, 1)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        signals['confidence_score'] = 0.0

        for i in range(1, len(df) - 1):
            current_price = df['close'].iloc[i]
            rsi = df['rsi'].iloc[i]
            bb_position = df['bb_position'].iloc[i]
            volume_ratio = df['volume_ratio'].iloc[i]

            # LONG SIGNAL: Oversold conditions + volume confirmation
            if (rsi < self.rsi_oversold and  # RSI oversold
                bb_position < self.bb_position_threshold and  # Near lower Bollinger band
                volume_ratio > 1.2):  # Above average volume

                confidence = 0.5
                if rsi < 25: confidence += 0.2  # Very oversold
                if bb_position < 0.1: confidence += 0.2  # Very close to lower band
                if volume_ratio > 1.5: confidence += 0.1  # High volume

                signals.loc[signals.index[i], 'signal'] = 1
                signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                logger.debug(f"Mean reversion LONG signal at {df.index[i]}, RSI: {rsi:.1f}, BB: {bb_position:.2f}")

            # SHORT SIGNAL: Overbought conditions + volume confirmation
            elif (rsi > self.rsi_overbought and  # RSI overbought
                  bb_position > (1 - self.bb_position_threshold) and  # Near upper Bollinger band
                  volume_ratio > 1.2):  # Above average volume

                confidence = 0.5
                if rsi > 75: confidence += 0.2  # Very overbought
                if bb_position > 0.9: confidence += 0.2  # Very close to upper band
                if volume_ratio > 1.5: confidence += 0.1  # High volume

                signals.loc[signals.index[i], 'signal'] = -1
                signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                logger.debug(f"Mean reversion SHORT signal at {df.index[i]}, RSI: {rsi:.1f}, BB: {bb_position:.2f}")

        signal_count = len(signals[signals['signal'] != 0])
        avg_confidence = signals[signals['signal'] != 0]['confidence_score'].mean() if signal_count > 0 else 0
        logger.info(f"Generated {signal_count} mean reversion signals with avg confidence: {avg_confidence:.2f}")
        return signals

    def is_stock_suitable(self, df: pd.DataFrame) -> tuple[bool, str]:
        """Check if stock is suitable for mean reversion strategy"""
        if len(df) < 50:
            return False, "Insufficient data"

        volatility = df['volatility'].iloc[-1] if 'volatility' in df.columns else 0
        rsi = df['rsi'].iloc[-1] if 'rsi' in df.columns else 50

        # High volatility is GOOD for mean reversion
        if volatility < 1.0:
            return False, f"Volatility too low ({volatility:.3f}) - needs > 1.0 for mean reversion"

        # Extreme RSI readings indicate potential for mean reversion
        if 35 < rsi < 65:
            return False, f"RSI too neutral ({rsi:.1f}) - needs oversold (<35) or overbought (>65)"

        return True, "Suitable for mean reversion"

    async def execute_trade(self, symbol: str, signal: int, entry_price: float, shares: int,
                          stop_loss: float, target: float) -> Dict:
        """Execute trade via IBKRConnection.place_order (same path as Groq strategy)."""
        if not self.ib_client:
            return {'status': 'error', 'message': 'No IBKR client'}
        if not self.paper_trading:
            return {'status': 'error', 'message': 'Paper trading disabled'}

        try:
            action = 'BUY' if signal == 1 else 'SELL'
            limit_px = entry_price * 1.001 if signal == 1 else entry_price * 0.999

            parent_order_id = await self.ib_client.place_order(
                symbol=symbol,
                action=action,
                quantity=shares,
                order_type='LMT',
                limit_price=limit_px,
            )
            if not parent_order_id:
                return {'status': 'error', 'message': 'Failed to place parent order'}

            await asyncio.sleep(5)

            exit_action = 'SELL' if signal == 1 else 'BUY'
            stop_order_id = None
            target_order_id = None

            if stop_loss is not None:
                await asyncio.sleep(2)
                stop_order_id = await self.ib_client.place_order(
                    symbol=symbol,
                    action=exit_action,
                    quantity=shares,
                    order_type='STP',
                    aux_price=stop_loss,
                    parent_id=parent_order_id,
                )
            if target is not None:
                await asyncio.sleep(2)
                target_order_id = await self.ib_client.place_order(
                    symbol=symbol,
                    action=exit_action,
                    quantity=shares,
                    order_type='LMT',
                    limit_price=target,
                    parent_id=parent_order_id,
                )

            result = {'status': 'success', 'order_id': parent_order_id}
            if stop_order_id:
                result['stop_order_id'] = stop_order_id
            if target_order_id:
                result['target_order_id'] = target_order_id
            return result

        except Exception as e:
            logger.error(f"Error executing mean reversion trade: {e}")
            return {'status': 'error', 'message': str(e)}

    async def backtest(self, df: pd.DataFrame, initial_capital: float = 50000, symbol: str = 'UNKNOWN') -> Dict:
        """Backtest mean reversion strategy"""
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []

        for i in range(1, len(df) - 1):
            if signals['signal'].iloc[i] != 0 and position == 0:
                entry_price = df['close'].iloc[i]
                is_long = signals['signal'].iloc[i] == 1

                # Calculate position size
                risk_per_share = abs(entry_price - entry_price * (1 - self.stop_loss_pct/100))
                shares = int(self.risk_per_trade // risk_per_share)

                # Limit position size
                max_shares = int((0.95 * capital) // entry_price)
                shares = min(shares, max_shares)

                if shares <= 0:
                    continue

                position = shares if is_long else -shares
                trades.append({
                    'entry_date': df.index[i],
                    'entry_price': entry_price,
                    'shares': abs(shares),
                    'type': 'LONG' if is_long else 'SHORT'
                })

            elif position != 0:
                current_trade = trades[-1]
                exit_triggered = False

                # Check stop loss and target
                if current_trade['type'] == 'LONG':
                    stop_price = current_trade['entry_price'] * (1 - self.stop_loss_pct/100)
                    target_price = current_trade['entry_price'] * (1 + self.target_pct/100)

                    if df['low'].iloc[i] <= stop_price:
                        exit_price = stop_price
                        exit_triggered = True
                    elif df['high'].iloc[i] >= target_price:
                        exit_price = target_price
                        exit_triggered = True

                else:  # SHORT
                    stop_price = current_trade['entry_price'] * (1 + self.stop_loss_pct/100)
                    target_price = current_trade['entry_price'] * (1 - self.target_pct/100)

                    if df['high'].iloc[i] >= stop_price:
                        exit_price = stop_price
                        exit_triggered = True
                    elif df['low'].iloc[i] <= target_price:
                        exit_price = target_price
                        exit_triggered = True

                if exit_triggered:
                    is_long = current_trade['type'] == 'LONG'
                    profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if is_long else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                    capital += profit

                    current_trade.update({
                        'exit_date': df.index[i],
                        'exit_price': exit_price,
                        'profit': profit
                    })

                    position = 0

        # Calculate results
        total_trades = len(trades)
        successful_trades = sum(1 for trade in trades if trade.get('profit', 0) > 0)
        success_rate = (successful_trades / total_trades * 100) if total_trades > 0 else 0
        total_profit = sum(trade.get('profit', 0) for trade in trades)

        def _last_float(col: str) -> Optional[float]:
            if col not in df.columns or df[col].empty:
                return None
            v = df[col].iloc[-1]
            try:
                return float(v) if pd.notna(v) else None
            except (TypeError, ValueError):
                return None

        meta = {
            "strategy": self.STRATEGY_SLUG,
            "symbol": symbol,
            "total_profit": total_profit,
            "success_rate": success_rate,
            "initial_capital": initial_capital,
            "final_capital": capital,
            "total_trades": total_trades,
            "trades": str(trades),
            "risk_per_trade": self.risk_per_trade,
            "stop_loss_pct": self.stop_loss_pct,
            "target_pct": self.target_pct,
            "bar_size": self.bar_size,
            "duration": self.duration,
            "paper_trading": self.paper_trading,
            "max_duration_hours": float(self.max_duration_hours),
            "rsi_oversold": self.rsi_oversold,
            "rsi_overbought": self.rsi_overbought,
            "rsi": _last_float("rsi"),
            "bb_position": _last_float("bb_position"),
            "volume_ratio": _last_float("volume_ratio"),
            "volatility": _last_float("volatility"),
        }
        result_id = f"{symbol}_{str(pd.Timestamp.now())}"
        # Chroma metadata: omit None values (schema varies by version)
        meta_for_chroma = {k: v for k, v in meta.items() if v is not None}
        try:
            self.collection.add(
                documents=[
                    f"Backtest result ({self.STRATEGY_SLUG}) for {symbol}: {total_profit} profit, {success_rate}% success rate"
                ],
                metadatas=meta_for_chroma,
                ids=[result_id],
            )
            logger.info(f"Stored backtest results for {symbol} in ChromaDB with ID {result_id}")
        except Exception as e:
            logger.warning(f"ChromaDB store failed for mean reversion backtest: {e}")

        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': total_trades,
            'success_rate': success_rate,
            'total_profit': total_profit,
            'trades': trades
        }
