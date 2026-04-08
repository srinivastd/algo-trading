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

        # ATR for dynamic stops
        df['atr'] = df['range'].rolling(window=14).mean()
        df['atr_pct'] = (df['atr'] / df['close']) * 100

        # RSI for mean reversion signals
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi'] = (100 - (100 / (1 + rs))).fillna(50)

        # RSI momentum (for confirmation)
        df['rsi_slope'] = df['rsi'].diff(3)

        # Bollinger Bands for mean reversion
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['bb_std'] = df['close'].rolling(window=20).std()
        df['bb_upper'] = df['sma_20'] + (df['bb_std'] * 2)
        df['bb_lower'] = df['sma_20'] - (df['bb_std'] * 2)
        bb_width = (df['bb_upper'] - df['bb_lower']).replace(0, np.nan)
        df['bb_position'] = ((df['close'] - df['bb_lower']) / bb_width).fillna(0.5)
        df['bb_width_pct'] = (bb_width / df['sma_20']) * 100

        # Volume indicators
        df['volume_sma_20'] = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = (df['volume'] / df['volume_sma_20'].replace(0, np.nan)).fillna(0)
        df['volume_slope'] = df['volume_ratio'].diff(3)

        # Volatility regime
        vol_short = df['volatility'].rolling(window=20).mean()
        vol_long = df['volatility'].rolling(window=60).mean()
        df['vol_ratio'] = (vol_short / vol_long.replace(0, np.nan)).fillna(1.0)

        # ADX for trend strength (market regime detection)
        # +DM and -DM
        df['plus_dm'] = df['high'].diff()
        df['minus_dm'] = -df['low'].diff()
        df['plus_dm'] = df['plus_dm'].where((df['plus_dm'] > df['minus_dm']) & (df['plus_dm'] > 0), 0)
        df['minus_dm'] = df['minus_dm'].where((df['minus_dm'] > df['plus_dm']) & (df['minus_dm'] > 0), 0)

        # TR
        df['tr1'] = df['high'] - df['low']
        df['tr2'] = abs(df['high'] - df['close'].shift(1))
        df['tr3'] = abs(df['low'] - df['close'].shift(1))
        df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

        # Smoothed values
        df['atr_14'] = df['tr'].rolling(window=14).mean()
        df['plus_di'] = (df['plus_dm'].rolling(window=14).mean() / df['atr_14'].replace(0, np.nan) * 100).fillna(0)
        df['minus_di'] = (df['minus_dm'].rolling(window=14).mean() / df['atr_14'].replace(0, np.nan) * 100).fillna(0)
        df['dx'] = (abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di']).replace(0, np.nan) * 100).fillna(0)
        df['adx'] = df['dx'].rolling(window=14).mean()

        # Market regime: low ADX = ranging (good for mean reversion), high ADX = trending (bad)
        df['ranging_market'] = df['adx'] < 25

        # Mean reversion score (opposite of momentum)
        # Higher score when:
        # - RSI is extreme (oversold/overbought)
        # - Price is near bands
        # - Market is ranging (low ADX)
        # - Volume is above average
        df['mean_reversion_score'] = (
            (50 - abs(df['rsi'] - 50)) / 50 * 0.35 +  # Higher score when RSI is extreme
            (1 - abs(df['bb_position'] - 0.5) * 2) * 0.25 +  # Higher score when near bands
            df['vol_ratio'].clip(0.5, 2.0) / 2.0 * 0.20 +  # Moderate volatility is good
            (df['ranging_market'].astype(float) * 0.2)  # Bonus for ranging market
        ).clip(0, 1)

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        signals['confidence_score'] = 0.0
        signals['stop_loss_pct'] = self.stop_loss_pct
        signals['target_pct'] = self.target_pct

        for i in range(10, len(df) - 1):  # Start later to have enough data for ADX
            current_price = df['close'].iloc[i]
            rsi = df['rsi'].iloc[i]
            bb_position = df['bb_position'].iloc[i]
            volume_ratio = df['volume_ratio'].iloc[i]
            adx = df['adx'].iloc[i]
            rsi_slope = df['rsi_slope'].iloc[i]
            volume_slope = df['volume_slope'].iloc[i]
            atr_pct = df['atr_pct'].iloc[i]

            # Skip if in strong trend (ADX > 30 indicates strong trend - bad for mean reversion)
            if pd.notna(adx) and adx > 30:
                continue

            # Dynamic thresholds based on volatility
            adjusted_rsi_oversold = self.rsi_oversold - (atr_pct * 0.5 if pd.notna(atr_pct) else 0)
            adjusted_rsi_overbought = self.rsi_overbought + (atr_pct * 0.5 if pd.notna(atr_pct) else 0)

            # LONG SIGNAL: Oversold conditions with confirmation
            # Confirmation: RSI starting to turn up OR volume spike
            if (rsi < adjusted_rsi_oversold and  # RSI oversold
                bb_position < self.bb_position_threshold and  # Near lower Bollinger band
                volume_ratio > 1.0):  # Above average volume (relaxed)

                # Entry confirmation - need evidence of reversal
                reversal_confirmed = False
                confidence = 0.4

                # RSI starting to turn up (divergence from price)
                if pd.notna(rsi_slope) and rsi_slope > 0:
                    reversal_confirmed = True
                    confidence += 0.25

                # Volume spike on this candle (institutional interest)
                if volume_ratio > 1.5:
                    reversal_confirmed = True
                    confidence += 0.15

                # Very extreme RSI (high probability setup)
                if rsi < 20:
                    confidence += 0.15

                # Very close to lower band
                if bb_position < 0.05:
                    confidence += 0.1

                if reversal_confirmed:
                    signals.loc[signals.index[i], 'signal'] = 1
                    signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                    # Dynamic stops based on ATR
                    if pd.notna(atr_pct):
                        signals.loc[signals.index[i], 'stop_loss_pct'] = max(atr_pct * 1.5, 1.0)
                        signals.loc[signals.index[i], 'target_pct'] = max(atr_pct * 3.0, 2.0)
                    logger.debug(f"Mean reversion LONG signal at {df.index[i]}, RSI: {rsi:.1f}, BB: {bb_position:.2f}, ADX: {adx:.1f}")

            # SHORT SIGNAL: Overbought conditions with confirmation
            elif (rsi > adjusted_rsi_overbought and  # RSI overbought
                  bb_position > (1 - self.bb_position_threshold) and  # Near upper Bollinger band
                  volume_ratio > 1.0):  # Above average volume (relaxed)

                # Entry confirmation - need evidence of reversal
                reversal_confirmed = False
                confidence = 0.4

                # RSI starting to turn down (divergence from price)
                if pd.notna(rsi_slope) and rsi_slope < 0:
                    reversal_confirmed = True
                    confidence += 0.25

                # Volume spike on this candle
                if volume_ratio > 1.5:
                    reversal_confirmed = True
                    confidence += 0.15

                # Very extreme RSI
                if rsi > 80:
                    confidence += 0.15

                # Very close to upper band
                if bb_position > 0.95:
                    confidence += 0.1

                if reversal_confirmed:
                    signals.loc[signals.index[i], 'signal'] = -1
                    signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                    # Dynamic stops based on ATR
                    if pd.notna(atr_pct):
                        signals.loc[signals.index[i], 'stop_loss_pct'] = max(atr_pct * 1.5, 1.0)
                        signals.loc[signals.index[i], 'target_pct'] = max(atr_pct * 3.0, 2.0)
                    logger.debug(f"Mean reversion SHORT signal at {df.index[i]}, RSI: {rsi:.1f}, BB: {bb_position:.2f}, ADX: {adx:.1f}")

        signal_count = len(signals[signals['signal'] != 0])
        avg_confidence = signals[signals['signal'] != 0]['confidence_score'].mean() if signal_count > 0 else 0
        logger.info(f"Generated {signal_count} mean reversion signals with avg confidence: {avg_confidence:.2f}")
        return signals

    def is_stock_suitable(self, df: pd.DataFrame) -> tuple[bool, str]:
        """Check if stock is suitable for mean reversion strategy"""
        if len(df) < 50:
            return False, "Insufficient data"

        # Need ADX to determine if market is ranging
        adx = df['adx'].iloc[-1] if 'adx' in df.columns else None
        volatility = df['volatility'].iloc[-1] if 'volatility' in df.columns else 0
        rsi = df['rsi'].iloc[-1] if 'rsi' in df.columns else 50
        atr_pct = df['atr_pct'].iloc[-1] if 'atr_pct' in df.columns else None

        # Check if we have enough data for ADX calculation
        if adx is None or pd.isna(adx):
            return False, "ADX not available - need more data"

        # Mean reversion works best in ranging markets (ADX < 25)
        # ADX > 30 indicates strong trend - bad for mean reversion
        if adx > 30:
            return False, f"Strong trending market (ADX: {adx:.1f}) - mean reversion performs poorly in trends"

        # Very low volatility also bad - need some movement
        if atr_pct is not None and pd.notna(atr_pct) and atr_pct < 0.5:
            return False, f"Volatility too low (ATR%: {atr_pct:.2f}%) - needs > 0.5% for mean reversion"

        # Extreme RSI readings indicate potential for mean reversion
        if 40 < rsi < 60:
            return False, f"RSI too neutral ({rsi:.1f}) - needs oversold (<40) or overbought (>60)"

        return True, f"Suitable for mean reversion (ADX: {adx:.1f}, ranging market)"

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
        """Backtest mean reversion strategy with time-based exits and trailing stops"""
        signals = self.generate_signals(df)
        capital = initial_capital
        position = 0
        trades = []
        current_trade = None

        for i in range(1, len(df) - 1):
            if signals['signal'].iloc[i] != 0 and position == 0:
                entry_price = df['close'].iloc[i]
                is_long = signals['signal'].iloc[i] == 1
                confidence = signals['confidence_score'].iloc[i]

                # Get dynamic stop/target from signal or use defaults
                signal_stop_pct = signals['stop_loss_pct'].iloc[i] if 'stop_loss_pct' in signals.columns else self.stop_loss_pct
                signal_target_pct = signals['target_pct'].iloc[i] if 'target_pct' in signals.columns else self.target_pct

                # Volatility-adjusted position sizing
                # Reduce size for high volatility, increase for high confidence
                atr_pct = df['atr_pct'].iloc[i] if 'atr_pct' in df.columns else signal_stop_pct
                vol_adjustment = 1.0
                if pd.notna(atr_pct):
                    # Reduce position size for very volatile stocks
                    if atr_pct > 5:
                        vol_adjustment = 0.5
                    elif atr_pct > 3:
                        vol_adjustment = 0.7

                confidence_adjustment = confidence  # Scale by confidence (0.5-1.0)

                # Calculate position size based on actual dollar risk
                risk_per_share = entry_price * (signal_stop_pct / 100)
                adjusted_risk = self.risk_per_trade * vol_adjustment * confidence_adjustment
                shares = int(adjusted_risk // risk_per_share) if risk_per_share > 0 else 0

                # Limit position size to 20% of capital max (reduced from 95%)
                max_shares = int((0.20 * capital) // entry_price)
                shares = min(shares, max_shares)

                if shares <= 0:
                    continue

                position = shares if is_long else -shares
                current_trade = {
                    'entry_date': df.index[i],
                    'entry_price': entry_price,
                    'shares': abs(shares),
                    'type': 'LONG' if is_long else 'SHORT',
                    'stop_pct': signal_stop_pct,
                    'target_pct': signal_target_pct,
                    'confidence': confidence,
                    'best_price': entry_price,  # For trailing stop tracking
                    'atr_pct': atr_pct if pd.notna(atr_pct) else signal_stop_pct
                }
                trades.append(current_trade)

            elif position != 0 and current_trade is not None:
                exit_triggered = False
                exit_price = None
                exit_reason = None

                # Update best price for trailing stop
                if current_trade['type'] == 'LONG':
                    current_trade['best_price'] = max(current_trade['best_price'], df['high'].iloc[i])
                else:
                    current_trade['best_price'] = min(current_trade['best_price'], df['low'].iloc[i])

                # Calculate dynamic stops
                if current_trade['type'] == 'LONG':
                    # Initial stop
                    stop_price = current_trade['entry_price'] * (1 - current_trade['stop_pct']/100)
                    target_price = current_trade['entry_price'] * (1 + current_trade['target_pct']/100)

                    # Trailing stop: lock in 50% of profits once we have 1.5R profit
                    profit_pct = (df['high'].iloc[i] - current_trade['entry_price']) / current_trade['entry_price'] * 100
                    if profit_pct > current_trade['target_pct'] * 0.5:
                        trailing_stop = current_trade['best_price'] * 0.98  # 2% below best price
                        stop_price = max(stop_price, trailing_stop)

                    if df['low'].iloc[i] <= stop_price:
                        exit_price = stop_price
                        exit_triggered = True
                        exit_reason = 'stop_loss'
                    elif df['high'].iloc[i] >= target_price:
                        exit_price = target_price
                        exit_triggered = True
                        exit_reason = 'target'

                else:  # SHORT
                    # Initial stop
                    stop_price = current_trade['entry_price'] * (1 + current_trade['stop_pct']/100)
                    target_price = current_trade['entry_price'] * (1 - current_trade['target_pct']/100)

                    # Trailing stop for shorts
                    profit_pct = (current_trade['entry_price'] - df['low'].iloc[i]) / current_trade['entry_price'] * 100
                    if profit_pct > current_trade['target_pct'] * 0.5:
                        trailing_stop = current_trade['best_price'] * 1.02  # 2% above best price (for short)
                        stop_price = min(stop_price, trailing_stop)

                    if df['high'].iloc[i] >= stop_price:
                        exit_price = stop_price
                        exit_triggered = True
                        exit_reason = 'stop_loss'
                    elif df['low'].iloc[i] <= target_price:
                        exit_price = target_price
                        exit_triggered = True
                        exit_reason = 'target'

                # Time-based exit
                if not exit_triggered and self.max_duration_hours > 0:
                    hours_held = (df.index[i] - current_trade['entry_date']).total_seconds() / 3600
                    if hours_held >= self.max_duration_hours:
                        exit_price = df['close'].iloc[i]
                        exit_triggered = True
                        exit_reason = 'time_exit'

                if exit_triggered:
                    is_long = current_trade['type'] == 'LONG'
                    profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if is_long else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                    capital += profit

                    current_trade.update({
                        'exit_date': df.index[i],
                        'exit_price': exit_price,
                        'profit': profit,
                        'exit_reason': exit_reason,
                        'hours_held': (df.index[i] - current_trade['entry_date']).total_seconds() / 3600
                    })

                    position = 0
                    current_trade = None

        # Calculate results
        total_trades = len(trades)
        successful_trades = sum(1 for trade in trades if trade.get('profit', 0) > 0)
        success_rate = (successful_trades / total_trades * 100) if total_trades > 0 else 0
        total_profit = sum(trade.get('profit', 0) for trade in trades)

        # Exit reason analysis
        stop_loss_exits = sum(1 for trade in trades if trade.get('exit_reason') == 'stop_loss')
        target_exits = sum(1 for trade in trades if trade.get('exit_reason') == 'target')
        time_exits = sum(1 for trade in trades if trade.get('exit_reason') == 'time_exit')
        avg_hours_held = sum(trade.get('hours_held', 0) for trade in trades) / total_trades if total_trades > 0 else 0

        # Calculate profit factor
        gross_profit = sum(trade.get('profit', 0) for trade in trades if trade.get('profit', 0) > 0)
        gross_loss = abs(sum(trade.get('profit', 0) for trade in trades if trade.get('profit', 0) < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0

        # Max drawdown calculation
        running_capital = initial_capital
        max_capital = initial_capital
        max_drawdown = 0
        for trade in trades:
            running_capital += trade.get('profit', 0)
            if running_capital > max_capital:
                max_capital = running_capital
            drawdown = (max_capital - running_capital) / max_capital * 100
            if drawdown > max_drawdown:
                max_drawdown = drawdown

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
            "adx": _last_float("adx"),
            "atr_pct": _last_float("atr_pct"),
            "profit_factor": profit_factor,
            "max_drawdown_pct": max_drawdown,
            "stop_loss_exits": stop_loss_exits,
            "target_exits": target_exits,
            "time_exits": time_exits,
            "avg_hours_held": avg_hours_held,
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
