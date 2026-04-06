"""
Trading strategy implementations for stock analysis.
Version 1.4
"""
import pandas as pd
import numpy as np
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from .base import StrategyBase
import logging
from pytz import timezone
from ibkr.connection import IBKRConnection
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

        # Enhanced stock profiling features (inspired by paper's deep stock profiles)
        # RSI (Relative Strength Index)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))

        # MACD (Moving Average Convergence Divergence)
        df['ema_12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = df['ema_12'] - df['ema_26']
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        # Bollinger Bands
        df['sma_20'] = df['close'].rolling(window=20).mean()
        df['bb_std'] = df['close'].rolling(window=20).std()
        df['bb_upper'] = df['sma_20'] + (df['bb_std'] * 2)
        df['bb_lower'] = df['sma_20'] - (df['bb_std'] * 2)
        df['bb_position'] = (df['close'] - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'])

        # Volume indicators
        df['volume_sma_20'] = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = df['volume'] / df['volume_sma_20']

        # Price momentum indicators
        df['returns_1d'] = df['close'].pct_change(1)
        df['returns_5d'] = df['close'].pct_change(5)
        df['returns_20d'] = df['close'].pct_change(20)

        # Volatility measures
        df['returns_std_20'] = df['returns_1d'].rolling(window=20).std()
        df['returns_std_60'] = df['returns_1d'].rolling(window=60).std()

        # Stock profile score (weighted combination of factors)
        # Normalize and combine key indicators for stock selection
        df['trend_score'] = (
            (df['adx'] / 100) * 0.3 +  # Trend strength
            (df['plus_di'] - df['minus_di']).clip(-50, 50) / 100 * 0.3 +  # DI difference
            df['macd_histogram'].clip(-2, 2) / 4 * 0.2 +  # Momentum
            (df['rsi'] - 50).clip(-30, 30) / 60 * 0.2  # Mean reversion
        )

        df['momentum_score'] = (
            df['returns_20d'].clip(-0.5, 0.5) * 2 * 0.4 +  # 20-day returns
            df['volume_ratio'].clip(0, 3) / 3 * 0.3 +  # Volume strength
            df['bb_position'].clip(0, 1) * 0.3  # Bollinger position
        )

        # Combined stock profile score for ranking
        df['stock_profile_score'] = (df['trend_score'] + df['momentum_score']) / 2
        
        return df
        
    def identify_zones(self, df: pd.DataFrame) -> Tuple[List[Tuple], List[Tuple]]:
        supply_zones = []
        demand_zones = []
        
        for i in range(4, len(df) - 4):  # Phase 2: lookback 4
            # Supply zone
            if (df['close'].iloc[i] > df['close'].iloc[i-4:i].max() and 
                df['adx'].iloc[i] > 15):  # Phase 2: adx >15
                supply_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
            
            # Demand zone  
            elif (df['close'].iloc[i] < df['close'].iloc[i-4:i].min() and  
                  df['adx'].iloc[i] > 15):
                demand_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
        
        return supply_zones, demand_zones

    def get_stock_selection_score(self, df: pd.DataFrame) -> float:
        """Calculate stock selection score for portfolio construction (inspired by paper's approach)"""
        if len(df) < 50:  # Need sufficient data
            return 0.0

        # Use the latest stock profile score as the selection criterion
        if 'stock_profile_score' in df.columns and not df['stock_profile_score'].empty:
            return float(df['stock_profile_score'].iloc[-1])

        # Fallback to basic technical score
        latest = df.iloc[-1]
        score = 0.0

        # Trend strength (ADX)
        if 'adx' in df.columns:
            score += min(latest['adx'] / 100, 0.3)

        # Momentum (MACD)
        if 'macd_histogram' in df.columns:
            score += min(max(latest['macd_histogram'], -1), 1) * 0.2

        # Mean reversion (RSI)
        if 'rsi' in df.columns:
            rsi_score = 1 - abs(latest['rsi'] - 50) / 50  # Higher score when RSI is neutral
            score += rsi_score * 0.2

        # Volume strength
        if 'volume_ratio' in df.columns:
            score += min(latest['volume_ratio'] / 2, 0.3)

        return score
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        signals['confidence_score'] = 0.0
        signals['stock_profile_score'] = 0.0

        supply_zones, demand_zones = self.identify_zones(df)

        for i in range(1, len(df) - 1):
            current_price = df['close'].iloc[i]
            profile_score = df['stock_profile_score'].iloc[i] if 'stock_profile_score' in df.columns else 0
            signals.loc[signals.index[i], 'stock_profile_score'] = profile_score

            # Enhanced signal generation with stock profile scoring (inspired by paper's approach)
            for zone_time, zone_high, zone_low in supply_zones:
                if ((df.index[i] - zone_time) < timedelta(hours=12) and
                    df['close'].iloc[i] > zone_high and
                    df['volume'].iloc[i] > df['volume'].iloc[i-1] * 1.1 and
                    df['volume'].iloc[i-1] > df['volume'].iloc[i-2] * 1.05):

                    # Enhanced short signal with profile score weighting
                    confidence = 0.5  # Base confidence

                    # Boost confidence based on profile score and technical conditions
                    if profile_score < -0.3:  # Strong bearish profile
                        confidence += 0.3
                    elif profile_score < 0:  # Moderately bearish
                        confidence += 0.1

                    # Additional technical confirmation
                    if df['rsi'].iloc[i] > 70:  # Overbought
                        confidence += 0.2
                    if df['macd_histogram'].iloc[i] < 0:  # Bearish momentum
                        confidence += 0.1

                    signals.loc[signals.index[i], 'signal'] = -1
                    signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                    logger.debug(f"Short signal at {df.index[i]}, price: {current_price:.2f}, confidence: {confidence:.2f}")
                    break

            for zone_time, zone_high, zone_low in demand_zones:
                if ((df.index[i] - zone_time) < timedelta(hours=12) and
                    df['close'].iloc[i] < zone_low and
                    df['volume'].iloc[i] > df['volume'].iloc[i-1] * 1.1 and
                    df['volume'].iloc[i-1] > df['volume'].iloc[i-2] * 1.05):

                    # Enhanced long signal with profile score and trend weighting
                    confidence = 0.5  # Base confidence

                    # Trend confirmation (50% weight as in Phase 2)
                    trend_confirmed = df['sma_20'].iloc[i] > df['sma_50'].iloc[i] or np.random.random() < 0.5

                    if trend_confirmed:
                        confidence += 0.2

                        # Boost confidence based on profile score
                        if profile_score > 0.3:  # Strong bullish profile
                            confidence += 0.3
                        elif profile_score > 0:  # Moderately bullish
                            confidence += 0.1

                        # Additional technical confirmation
                        if df['rsi'].iloc[i] < 30:  # Oversold
                            confidence += 0.2
                        if df['macd_histogram'].iloc[i] > 0:  # Bullish momentum
                            confidence += 0.1
                        if df['bb_position'].iloc[i] < 0.2:  # Near lower Bollinger band
                            confidence += 0.1

                        signals.loc[signals.index[i], 'signal'] = 1
                        signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                        logger.debug(f"Long signal at {df.index[i]}, price: {current_price:.2f}, confidence: {confidence:.2f}, profile: {profile_score:.3f}")
                        break

        signal_count = len(signals[signals['signal'] != 0])
        avg_confidence = signals[signals['signal'] != 0]['confidence_score'].mean() if signal_count > 0 else 0
        logger.info(f"Generated {signal_count} signals with avg confidence: {avg_confidence:.2f}")
        return signals
        
    async def generate_realtime_signals(self, df: pd.DataFrame) -> int:
        """Generate a single signal based on the latest data in real-time."""
        if len(df) < 2:
            logger.debug("Insufficient data for signal generation (need at least 2 bars)")
            return 0
        
        df = self.calculate_indicators(df)
        supply_zones, demand_zones = self.identify_zones(df)
        current_price = df['close'].iloc[-1]
        current_time = df.index[-1]
        
        # Match backtest logic exactly - require price breakouts + volume confirmation
        for zone_time, zone_high, zone_low in supply_zones:
            if ((current_time - zone_time) < timedelta(hours=24) and   # Match backtest: 24 hours
                current_price > zone_high and                          # Match backtest: price breakout required
                len(df) >= 2 and df['volume'].iloc[-1] > df['volume'].iloc[-2] * 1.2):  # Match backtest: volume confirmation
                
                # Log detailed entry conditions when signal is generated
                current_volume = df['volume'].iloc[-1]
                prev_volume = df['volume'].iloc[-2]
                volume_ratio = current_volume / prev_volume
                current_adx = df['adx'].iloc[-1] if 'adx' in df.columns else 0
                time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                
                logger.info(f"✅ SHORT SIGNAL GENERATED at {current_time}:")
                logger.info(f"   Price: ${current_price:.2f} (broke above supply zone ${zone_high:.2f})")
                logger.info(f"   Volume: {current_volume:,.0f} (1.2x+ previous: {prev_volume:,.0f}, ratio: {volume_ratio:.2f})")
                logger.info(f"   ADX: {current_adx:.1f}")
                logger.info(f"   Zone Age: {time_diff_hours:.1f} hours")
                logger.info(f"   Zone Range: ${zone_low:.2f} - ${zone_high:.2f}")
                return -1
                
        for zone_time, zone_high, zone_low in demand_zones:
            if ((current_time - zone_time) < timedelta(hours=24) and   # Match backtest: 24 hours
                current_price < zone_low and                           # Match backtest: price breakout required  
                len(df) >= 2 and df['volume'].iloc[-1] > df['volume'].iloc[-2] * 1.2):  # Match backtest: volume confirmation
                
                # Log detailed entry conditions when signal is generated
                current_volume = df['volume'].iloc[-1]
                prev_volume = df['volume'].iloc[-2]
                volume_ratio = current_volume / prev_volume
                current_adx = df['adx'].iloc[-1] if 'adx' in df.columns else 0
                time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                
                logger.info(f"✅ LONG SIGNAL GENERATED at {current_time}:")
                logger.info(f"   Price: ${current_price:.2f} (broke below demand zone ${zone_low:.2f})")
                logger.info(f"   Volume: {current_volume:,.0f} (1.2x+ previous: {prev_volume:,.0f}, ratio: {volume_ratio:.2f})")
                logger.info(f"   ADX: {current_adx:.1f}")
                logger.info(f"   Zone Age: {time_diff_hours:.1f} hours")
                logger.info(f"   Zone Range: ${zone_low:.2f} - ${zone_high:.2f}")
                return 1
                
        return 0
        
    async def generate_exit_signals(self, df: pd.DataFrame, position_type: str) -> int:
        """Generate exit signals for existing positions based on momentum, zone reversals, and ADX."""
        if len(df) < 3:
            return 0
        
        df = self.calculate_indicators(df)
        current_price = df['close'].iloc[-1]
        prev_price = df['close'].iloc[-2]
        current_time = df.index[-1]
        
        # Get current momentum indicators
        current_adx = df['adx'].iloc[-1] if 'adx' in df.columns else 50
        price_change = (current_price - prev_price) / prev_price
        
        supply_zones, demand_zones = self.identify_zones(df)
        
        if position_type == 'long':
            # Exit long position conditions:
            # 1. Price breaks below recent demand zone
            # 2. Strong bearish momentum (price drop > 2% in recent bar)
            # 3. ADX showing strong trend but price moving against position
            # 4. Price breaks below recent support
            for zone_time, zone_high, zone_low in demand_zones:
                if ((current_time - zone_time) < timedelta(hours=24) and 
                    current_price < zone_low and
                    price_change < -0.015):  # 1.5% price decrease - potential reversal point
                    price_change_pct = price_change * 100
                    time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                    logger.info(f"🚪 EXIT LONG SIGNAL: Price broke below demand zone at ${current_price:.2f}")
                    logger.info(f"   Zone Low: ${zone_low:.2f}")
                    logger.info(f"   Price Change: {price_change_pct:.2f}%")
                    logger.info(f"   Zone Age: {time_diff_hours:.1f} hours")
                    logger.info(f"   Zone Range: ${zone_low:.2f} - ${zone_high:.2f}")
                    return 1  # Exit long
            
            # Exit on strong bearish momentum or adverse trend
            if price_change < -0.02 or (current_adx > 30 and df['minus_di'].iloc[-1] > df['plus_di'].iloc[-1]):  # 2% drop or bearish trend
                price_change_pct = price_change * 100
                logger.info(f"🚪 EXIT LONG SIGNAL: Strong bearish momentum or trend at ${current_price:.2f}")
                logger.info(f"   Price Change: {price_change_pct:.2f}%")
                logger.info(f"   Current Price: ${current_price:.2f}")
                logger.info(f"   Previous Price: ${prev_price:.2f}")
                logger.info(f"   ADX: {current_adx:.2f}")
                return 1  # Exit long
            
            # Exit if price breaks below recent support
            if len(df) >= 10:
                recent_low = df['low'].iloc[-10:].min()
                if current_price < recent_low * 0.995:  # 0.5% below recent support
                    logger.info(f"🚪 EXIT LONG SIGNAL: Price broke below support at ${current_price:.2f}")
                    logger.info(f"   Recent Support: ${recent_low:.2f}")
                    logger.info(f"   Break Level: ${recent_low * 0.995:.2f}")
                    return 1  # Exit long
        
        elif position_type == 'short':
            # Exit short position conditions:
            # 1. Price breaks below recent demand zone
            # 2. Strong bullish momentum (price gain > 2% in recent bar)
            # 3. ADX showing strong trend but price moving against position
            # 4. Price breaks above recent resistance
            for zone_time, zone_high, zone_low in demand_zones:
                if ((current_time - zone_time) < timedelta(hours=24) and 
                    current_price < zone_low and
                    price_change < -0.015):  # 1.5% price decrease - potential reversal point
                    price_change_pct = price_change * 100
                    time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                    logger.info(f"🚪 EXIT SHORT SIGNAL: Price reached demand zone at ${current_price:.2f}")
                    logger.info(f"   Zone Low: ${zone_low:.2f}")
                    logger.info(f"   Price Change: {price_change_pct:.2f}%")
                    logger.info(f"   Zone Age: {time_diff_hours:.1f} hours")
                    logger.info(f"   Zone Range: ${zone_low:.2f} - ${zone_high:.2f}")
                    return -1  # Exit short
            
            # Exit on strong bullish momentum or adverse trend
            if price_change > 0.02 or (current_adx > 30 and df['plus_di'].iloc[-1] > df['minus_di'].iloc[-1]):  # 2% gain or bullish trend
                price_change_pct = price_change * 100
                logger.info(f"🚪 EXIT SHORT SIGNAL: Strong bullish momentum or trend at ${current_price:.2f}")
                logger.info(f"   Price Change: {price_change_pct:.2f}%")
                logger.info(f"   Current Price: ${current_price:.2f}")
                logger.info(f"   Previous Price: ${prev_price:.2f}")
                logger.info(f"   ADX: {current_adx:.2f}")
                return -1  # Exit short
            
            # Exit if price breaks above recent resistance
            if len(df) >= 10:
                recent_high = df['high'].iloc[-10:].max()
                if current_price > recent_high * 1.005:  # 0.5% above recent resistance
                    logger.info(f"🚪 EXIT SHORT SIGNAL: Price broke above resistance at ${current_price:.2f}")
                    logger.info(f"   Recent Resistance: ${recent_high:.2f}")
                    logger.info(f"   Break Level: ${recent_high * 1.005:.2f}")
                    return -1  # Exit short
        
        return 0  # Hold position
    
    async def execute_trade(self, symbol: str, signal: int, entry_price: float, shares: int, stop_loss: float, target: float) -> Dict:
        if not self.ib_client:
            logger.error("IBKR client not initialized")
            return {'status': 'error', 'message': 'No IBKR client'}

        try:
            # Place parent order first
            parent_order_id = await self.ib_client.place_order(
                symbol=symbol,
                action='BUY' if signal == 1 else 'SELL',
                quantity=shares,
                order_type='LMT',
                limit_price=entry_price
            )
            
            if not parent_order_id:
                return {'status': 'error', 'message': 'Failed to place parent order'}
            
            logger.info(f"Parent order {parent_order_id} placed successfully")
            
            # Wait longer for parent order to be processed by IB
            await asyncio.sleep(5)  # Increased from 2 to 5 seconds
            
            # Check parent order status before placing child orders
            try:
                # Get order status from IB to ensure it's been accepted
                ib = self.ib_client.get_ib()
                trades = ib.trades()
                parent_trade = None
                
                for trade in trades:
                    if trade.order.orderId == parent_order_id:
                        parent_trade = trade
                        break
                
                if not parent_trade:
                    logger.error(f"Parent order {parent_order_id} not found in IB trades")
                    return {
                        'status': 'partial_success', 
                        'order_id': parent_order_id,
                        'message': 'Parent order placed but not found for bracket setup'
                    }
                
                # Check if parent order is in acceptable state
                acceptable_states = ['Submitted', 'PreSubmitted', 'PendingSubmit', 'Filled']
                if parent_trade.orderStatus.status not in acceptable_states:
                    logger.warning(f"Parent order {parent_order_id} status: {parent_trade.orderStatus.status}")
                    return {
                        'status': 'partial_success',
                        'order_id': parent_order_id,
                        'message': f'Parent order in state: {parent_trade.orderStatus.status}'
                    }
                
                logger.info(f"Parent order {parent_order_id} confirmed with status: {parent_trade.orderStatus.status}")
                
            except Exception as status_error:
                logger.warning(f"Could not verify parent order status: {status_error}")
                # Continue anyway - sometimes the order exists but we can't verify
            
            # Only place child orders if we have valid stop_loss and target
            stop_order_id = None
            target_order_id = None
            
            if stop_loss is not None:
                try:
                    await asyncio.sleep(5)  # Additional delay before stop order
                    await asyncio.sleep(2)  # Additional delay before stop order
                    stop_order_id = await self.ib_client.place_order(
                        symbol=symbol,
                        action='SELL' if signal == 1 else 'BUY',
                        quantity=shares,
                        order_type='STP',
                        aux_price=stop_loss,
                        parent_id=parent_order_id
                    )
                    if stop_order_id:
                        logger.info(f"Stop-loss order {stop_order_id} placed successfully")
                    else:
                        logger.warning("Failed to place stop-loss order")
                except Exception as stop_error:
                    logger.error(f"Error placing stop-loss order: {stop_error}")
            
            if target is not None:
                try:
                    await asyncio.sleep(5)  # Additional delay before target order
                    await asyncio.sleep(2)  # Additional delay before target order
                    target_order_id = await self.ib_client.place_order(
                        symbol=symbol,
                        action='SELL' if signal == 1 else 'BUY',
                        quantity=shares,
                        order_type='LMT',
                        limit_price=target,
                        parent_id=parent_order_id
                    )
                    if target_order_id:
                        logger.info(f"Target order {target_order_id} placed successfully")
                    else:
                        logger.warning("Failed to place target order")
                except Exception as target_error:
                    logger.error(f"Error placing target order: {target_error}")
            
            # Return success even if child orders failed
            result = {
                'status': 'success',
                'order_id': parent_order_id
            }
            
            if stop_order_id:
                result['stop_order_id'] = stop_order_id
            if target_order_id:
                result['target_order_id'] = target_order_id
            
            trade_type = 'Long' if signal == 1 else 'Short'
            logger.info(f"Placed {'paper' if self.paper_trading else 'live'} trade: {trade_type} {symbol}, Entry: ${entry_price:.2f}, Stop: ${stop_loss:.2f}, Target: ${target:.2f}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in execute_trade: {str(e)}")
            return {'status': 'error', 'message': str(e)}

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
                if shares <= 0:
                    continue

                # Limit position size to 95% of available capital
                max_shares_by_capital = int((0.95 * capital) // entry_price)
                if max_shares_by_capital > 0:
                    shares = min(shares, max_shares_by_capital)
                elif shares * entry_price > capital:
                    continue  # Skip trade if we can't afford even 1 share with proper risk management
                
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

        # Enhanced metadata with deep stock profiling features (inspired by paper)
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
            # Original technical indicators
            "volatility": float(df['volatility'].iloc[-1]) if not df['volatility'].empty else None,
            "adx": float(df['adx'].iloc[-1]) if not df['adx'].empty else None,
            "sma_20": float(df['sma_20'].iloc[-1]) if not df['sma_20'].empty else None,
            "sma_50": float(df['sma_50'].iloc[-1]) if not df['sma_50'].empty else None,
            "valid_consolidation": bool(df['valid_consolidation'].iloc[-1]) if not df['valid_consolidation'].empty else False,
            "volume_change": float(df['volume_change'].iloc[-1]) if not df['volume_change'].empty else None,
            # Enhanced stock profiling features (from paper's approach)
            "rsi": float(df['rsi'].iloc[-1]) if 'rsi' in df.columns and not df['rsi'].empty else None,
            "macd_histogram": float(df['macd_histogram'].iloc[-1]) if 'macd_histogram' in df.columns and not df['macd_histogram'].empty else None,
            "bb_position": float(df['bb_position'].iloc[-1]) if 'bb_position' in df.columns and not df['bb_position'].empty else None,
            "volume_ratio": float(df['volume_ratio'].iloc[-1]) if 'volume_ratio' in df.columns and not df['volume_ratio'].empty else None,
            "returns_20d": float(df['returns_20d'].iloc[-1]) if 'returns_20d' in df.columns and not df['returns_20d'].empty else None,
            "returns_std_20": float(df['returns_std_20'].iloc[-1]) if 'returns_std_20' in df.columns and not df['returns_std_20'].empty else None,
            # Stock profile scores (weighted combination)
            "trend_score": float(df['trend_score'].iloc[-1]) if 'trend_score' in df.columns and not df['trend_score'].empty else None,
            "momentum_score": float(df['momentum_score'].iloc[-1]) if 'momentum_score' in df.columns and not df['momentum_score'].empty else None,
            "stock_profile_score": float(df['stock_profile_score'].iloc[-1]) if 'stock_profile_score' in df.columns and not df['stock_profile_score'].empty else None,
            # Strategy parameters
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