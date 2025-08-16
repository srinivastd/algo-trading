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
from ib_insync import MarketOrder, StopOrder, LimitOrder, Stock
import chromadb
from chromadb.utils import embedding_functions

logger = logging.getLogger(__name__)

class GroqRecommendationsStrategy(StrategyBase):
    """Strategy based on supply/demand zones with optimized ADX and duration, with paper trading support"""
    
    def __init__(self, risk_per_trade: float = 3000, max_duration_hours: float = 16.0, paper_trading: bool = True,
                 stop_loss_pct: float = 0.75, target_pct: float = 3.0, bar_size: str = "1 hour", duration: str = "1 M",
                 use_accumulate_distribute: bool = False, min_shares_for_accumulate: int = 100):
        super().__init__("Groq Recommendations Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        self.paper_trading = paper_trading
        self.stop_loss_pct = stop_loss_pct  # Stop loss percentage
        self.target_pct = target_pct  # Target profit percentage
        self.ib_client = None  # Will be set by main.py
        self.bar_size = bar_size  # Time frame of bars (e.g., "1 hour")
        self.duration = duration  # Backtest duration (e.g., "1 M")
        
        # =================== STRATEGY CONFIGURATION ===================
        # Zone Creation Parameters
        self.zone_lookback_bars = 5              # Bars to look back for zone creation
        self.zone_creation_adx_min = 18          # Minimum ADX for zone creation (kept same)
        self.zone_creation_volume_multiplier = 1.5  # Volume must be 1.5x mean for zone creation (reverted from 1.3)
        
        # Entry Signal Parameters
        self.entry_adx_min = 20                  # Minimum ADX for entry signals (kept same)
        self.entry_volume_multiplier = 1.3       # Current volume vs previous volume (reverted from 1.1)
        self.entry_volume_trend_multiplier = 1.15  # Volume trend confirmation (reverted from 1.05)
        self.entry_zone_max_age_hours = 10       # Maximum age of zones for entry signals (kept same)
        self.entry_min_history_bars = 3          # Minimum bars needed for entry signals (kept same)
        
        # Exit Signal Parameters
        self.exit_adx_min = 20                   # Minimum ADX for trend reversal exits (kept same)
        self.exit_volume_multiplier = 1.2        # Volume confirmation for zone breakouts/breakdowns (reverted from 1.15)
        self.exit_zone_max_age_hours = 12        # Maximum age of zones for exit signals (kept same)
        self.exit_quick_threshold_pct = 0.01     # Quick exit on 1% adverse move (kept same)
        
        # Technical Indicator Parameters
        self.sma_short_period = 20               # Short SMA period
        self.sma_long_period = 50                # Long SMA period
        self.adx_period = 14                     # ADX calculation period
        self.atr_period = 14                     # ATR calculation period
        self.volatility_window = 5               # Volatility calculation window
        
        # =================== ACCUMULATE/DISTRIBUTE CONFIGURATION ===================
        # Order Execution Parameters
        self.use_accumulate_distribute = use_accumulate_distribute  # Enable/disable accumulate/distribute orders
        self.min_shares_for_accumulate = min_shares_for_accumulate  # Minimum shares to use accumulate orders
        self.default_volatility = 0.02           # Default volatility (2%)
        self.default_risk_tolerance = 0.5        # Default risk tolerance (moderate)
        self.volatility_multiplier_stop = 1.5    # Volatility multiplier for stop-loss orders
        self.volatility_multiplier_target = 0.8  # Volatility multiplier for target orders
        self.risk_tolerance_stop = 0.3           # Risk tolerance for stop-loss (conservative)
        self.risk_tolerance_target = 0.7         # Risk tolerance for targets (aggressive)
        
        # Smart Order Selection Thresholds
        self.high_volatility_threshold = 0.03    # Use accumulate for volatility > 3%
        self.strong_trend_threshold = 0.7        # Use accumulate for trend strength > 0.7
        self.high_volume_threshold = 1.5         # Use accumulate for volume > 1.5x average
        self.large_position_threshold = 100      # Use accumulate for positions > 100 shares
        # =============================================================
        
        # Initialize ChromaDB client and collection
        self.chroma_client = chromadb.PersistentClient(path="./chroma_db")
        self.collection = self.chroma_client.get_or_create_collection(
            name="backtest_results",
            embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        )
        logger.info(f"Initialized {self.name} with:")
        logger.info(f"  Risk Management: risk_per_trade=${risk_per_trade}, max_duration={max_duration_hours}h")
        logger.info(f"  Risk Targets: stop_loss={stop_loss_pct}%, target={target_pct}%")
        logger.info(f"  Zone Config: ADX_min={self.zone_creation_adx_min}, lookback={self.zone_lookback_bars}, vol_mult={self.zone_creation_volume_multiplier}x")
        logger.info(f"  Entry Config: ADX_min={self.entry_adx_min}, vol_mult={self.entry_volume_multiplier}x, max_age={self.entry_zone_max_age_hours}h")
        logger.info(f"  Exit Config: ADX_min={self.exit_adx_min}, vol_mult={self.exit_volume_multiplier}x, quick_exit={self.exit_quick_threshold_pct*100}%")
        logger.info(f"  Trading: paper_trading={paper_trading}, bar_size={bar_size}, duration={duration}")
        logger.info(f"  Accumulate/Distribute: enabled={use_accumulate_distribute}, min_shares={min_shares_for_accumulate}")

    def set_ib_client(self, ib_client):
        """Set the IBKR client for trading"""
        self.ib_client = ib_client
        logger.info("IBKR client set for trading")

    def apply_symbol_overrides(self, symbol: str) -> None:
        """Apply per-symbol configuration overrides without changing core logic.

        Args:
            symbol: The trading symbol (e.g., "PLTR").
        """
        try:
            symbol_upper = symbol.upper() if symbol else ""
        except Exception:
            symbol_upper = ""

        if symbol_upper == "PLTR":
            # Risk and trade management
            self.risk_per_trade = 1500
            self.max_duration_hours = 12
            self.stop_loss_pct = 0.9
            self.target_pct = 5.0

            # Zone creation
            self.zone_creation_adx_min = 18
            self.zone_creation_volume_multiplier = 1.4

            # Entry signal
            self.entry_adx_min = 18
            self.entry_volume_multiplier = 1.2
            self.entry_volume_trend_multiplier = 1.10
            self.entry_zone_max_age_hours = 8

            # Exit signal
            self.exit_adx_min = 18
            self.exit_volume_multiplier = 1.15

            # Accumulate/Distribute behavior
            self.use_accumulate_distribute = True
            self.min_shares_for_accumulate = 120
            self.high_volatility_threshold = 0.035
            self.strong_trend_threshold = 0.7

            logger.info("Applied PLTR-specific parameter overrides")

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        eastern = timezone('America/New_York')
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert(eastern)
        else:
            df.index = df.index.tz_convert(eastern)
        
        df['range'] = df['high'] - df['low']
        df['volatility'] = df['range'].rolling(window=self.volatility_window).mean()
        df['volume_change'] = df['volume'].pct_change().abs()
        
        df['consolidation'] = (df['range'] < 1.2 * df['volatility'].shift(1)) & (df['volume_change'] < 0.2)
        df['consolidation_count'] = df['consolidation'].rolling(window=2).sum()
        df['valid_consolidation'] = df['consolidation_count'] >= 2
        
        df['sma_20'] = df['close'].rolling(window=self.sma_short_period).mean()
        df['sma_50'] = df['close'].rolling(window=self.sma_long_period).mean()
        
        # Calculate ADX using configured period
        df['high_low'] = df['high'] - df['low']
        df['high_prev_close'] = abs(df['high'] - df['close'].shift(1))
        df['low_prev_close'] = abs(df['low'] - df['close'].shift(1))
        df['tr'] = df[['high_low', 'high_prev_close', 'low_prev_close']].max(axis=1)
        df['atr'] = df['tr'].rolling(window=self.atr_period).mean()
        df['plus_dm'] = np.where((df['high'] > df['high'].shift(1)) & (df['high'] - df['high'].shift(1) > df['low'].shift(1) - df['low']), df['high'] - df['high'].shift(1), 0)
        df['minus_dm'] = np.where((df['low'] < df['low'].shift(1)) & (df['low'].shift(1) - df['low'] > df['high'] - df['high'].shift(1)), df['low'].shift(1) - df['low'], 0)
        df['plus_di'] = 100 * df['plus_dm'].rolling(window=self.adx_period).mean() / df['atr']
        df['minus_di'] = 100 * df['minus_dm'].rolling(window=self.adx_period).mean() / df['atr']
        df['dx'] = 100 * np.abs(df['plus_di'] - df['minus_di']) / (df['plus_di'] + df['minus_di'])
        df['adx'] = df['dx'].rolling(window=self.adx_period).mean()
        
        return df
        
    def identify_zones(self, df: pd.DataFrame) -> Tuple[List[Tuple], List[Tuple]]:
        supply_zones = []
        demand_zones = []
        
        for i in range(self.zone_lookback_bars, len(df) - 4):  # Use configured lookback
            # Supply zone - TIGHTENED CONDITIONS
            if (df['close'].iloc[i] > df['close'].iloc[i-self.zone_lookback_bars:i].max() and 
                df['adx'].iloc[i] > self.zone_creation_adx_min and  # Use configured ADX minimum
                df['volume'].iloc[i] > df['volume'].iloc[i-3:i].mean() * self.zone_creation_volume_multiplier):  # Use configured volume multiplier
                supply_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
            
            # Demand zone - TIGHTENED CONDITIONS
            elif (df['close'].iloc[i] < df['close'].iloc[i-self.zone_lookback_bars:i].min() and  
                  df['adx'].iloc[i] > self.zone_creation_adx_min and  # Use configured ADX minimum
                  df['volume'].iloc[i] > df['volume'].iloc[i-3:i].mean() * self.zone_creation_volume_multiplier):  # Use configured volume multiplier
                demand_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
                
        return supply_zones, demand_zones
        
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        
        supply_zones, demand_zones = self.identify_zones(df)
        
        for i in range(self.entry_min_history_bars, len(df) - 1):  # Use configured minimum history
            current_price = df['close'].iloc[i]
            current_volume = df['volume'].iloc[i]
            prev_volume = df['volume'].iloc[i-1]
            
            # STRICTER SHORT SIGNAL CONDITIONS
            for zone_time, zone_high, zone_low in supply_zones:
                if ((df.index[i] - zone_time) < timedelta(hours=self.entry_zone_max_age_hours) and  # Use configured max age
                    df['close'].iloc[i] > zone_high and
                    current_volume > prev_volume * self.entry_volume_multiplier and  # Use configured volume multiplier
                    df['volume'].iloc[i-1] > df['volume'].iloc[i-2] * self.entry_volume_trend_multiplier and  # Use configured trend multiplier
                    df['adx'].iloc[i] > self.entry_adx_min and  # Use configured ADX minimum
                    df['minus_di'].iloc[i] > df['plus_di'].iloc[i]):  # Bearish momentum confirmation
                    signals.loc[signals.index[i], 'signal'] = -1
                    logger.debug(f"Short signal at {df.index[i]}, price: {current_price:.2f}")
                    break
                    
            # STRICTER LONG SIGNAL CONDITIONS  
            for zone_time, zone_high, zone_low in demand_zones:
                if ((df.index[i] - zone_time) < timedelta(hours=self.entry_zone_max_age_hours) and  # Use configured max age
                    df['close'].iloc[i] < zone_low and
                    current_volume > prev_volume * self.entry_volume_multiplier and  # Use configured volume multiplier
                    df['volume'].iloc[i-1] > df['volume'].iloc[i-2] * self.entry_volume_trend_multiplier and  # Use configured trend multiplier
                    df['adx'].iloc[i] > self.entry_adx_min and  # Use configured ADX minimum
                    df['plus_di'].iloc[i] > df['minus_di'].iloc[i] and  # Bullish momentum confirmation
                    df['sma_20'].iloc[i] > df['sma_50'].iloc[i]):  # Strong trend filter - removed random 50%
                    signals.loc[signals.index[i], 'signal'] = 1
                    logger.debug(f"Long signal at {df.index[i]}, price: {current_price:.2f}")
                    break
                    
        logger.info(f"Generated {len(signals[signals['signal'] != 0])} signals")
        return signals
        
    async def generate_realtime_signals(self, df: pd.DataFrame) -> int:
        """Generate a single signal based on the latest data in real-time."""
        if len(df) < self.entry_min_history_bars + 2:  # Use configured minimum + buffer
            logger.debug(f"Insufficient data for signal generation (need at least {self.entry_min_history_bars + 2} bars)")
            return 0
        
        df = self.calculate_indicators(df)
        supply_zones, demand_zones = self.identify_zones(df)
        current_price = df['close'].iloc[-1]
        current_time = df.index[-1]
        current_volume = df['volume'].iloc[-1]
        prev_volume = df['volume'].iloc[-2]
        
        # MATCH BACKTEST: STRICTER SHORT SIGNAL CONDITIONS
        for zone_time, zone_high, zone_low in supply_zones:
            if ((current_time - zone_time) < timedelta(hours=self.entry_zone_max_age_hours) and   # Use configured max age
                current_price > zone_high and                          # Match backtest: price breakout required
                current_volume > prev_volume * self.entry_volume_multiplier and                # Use configured volume multiplier
                len(df) >= 3 and df['volume'].iloc[-2] > df['volume'].iloc[-3] * self.entry_volume_trend_multiplier and  # Use configured trend multiplier
                df['adx'].iloc[-1] > self.entry_adx_min and                            # Use configured ADX minimum
                df['minus_di'].iloc[-1] > df['plus_di'].iloc[-1]):     # Bearish momentum
                
                # Log detailed entry conditions when signal is generated
                volume_ratio = current_volume / prev_volume
                current_adx = df['adx'].iloc[-1] if 'adx' in df.columns else 0
                time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                
                logger.info(f"✅ SHORT SIGNAL GENERATED at {current_time}:")
                logger.info(f"   Price: ${current_price:.2f} (broke above supply zone ${zone_high:.2f})")
                logger.info(f"   Volume: {current_volume:,.0f} ({self.entry_volume_multiplier}x+ previous: {prev_volume:,.0f}, ratio: {volume_ratio:.2f})")
                logger.info(f"   ADX: {current_adx:.1f} (>{self.entry_adx_min})")
                logger.info(f"   Zone Age: {time_diff_hours:.1f} hours (<{self.entry_zone_max_age_hours}h)")
                logger.info(f"   Zone Range: ${zone_low:.2f} - ${zone_high:.2f}")
                return -1
                
        # MATCH BACKTEST: STRICTER LONG SIGNAL CONDITIONS
        for zone_time, zone_high, zone_low in demand_zones:
            if ((current_time - zone_time) < timedelta(hours=self.entry_zone_max_age_hours) and   # Use configured max age
                current_price < zone_low and                           # Match backtest: price breakout required  
                current_volume > prev_volume * self.entry_volume_multiplier and                # Use configured volume multiplier
                len(df) >= 3 and df['volume'].iloc[-2] > df['volume'].iloc[-3] * self.entry_volume_trend_multiplier and  # Use configured trend multiplier
                df['adx'].iloc[-1] > self.entry_adx_min and                            # Use configured ADX minimum
                df['plus_di'].iloc[-1] > df['minus_di'].iloc[-1] and   # Bullish momentum
                df['sma_20'].iloc[-1] > df['sma_50'].iloc[-1]):        # Trend filter
                
                # Log detailed entry conditions when signal is generated
                volume_ratio = current_volume / prev_volume
                current_adx = df['adx'].iloc[-1] if 'adx' in df.columns else 0
                time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                
                logger.info(f"✅ LONG SIGNAL GENERATED at {current_time}:")
                logger.info(f"   Price: ${current_price:.2f} (broke below demand zone ${zone_low:.2f})")
                logger.info(f"   Volume: {current_volume:,.0f} ({self.entry_volume_multiplier}x+ previous: {prev_volume:,.0f}, ratio: {volume_ratio:.2f})")
                logger.info(f"   ADX: {current_adx:.1f} (>{self.entry_adx_min})")
                logger.info(f"   Zone Age: {time_diff_hours:.1f} hours (<{self.entry_zone_max_age_hours}h)")
                logger.info(f"   Zone Range: ${zone_low:.2f} - ${zone_high:.2f}")
                logger.info(f"   Trend: SMA20(${df['sma_20'].iloc[-1]:.2f}) > SMA50(${df['sma_50'].iloc[-1]:.2f})")
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
            # EXIT LONG: More protective conditions
            # 1. Immediate exit on strong bearish momentum (configured quick exit threshold)
            if price_change < -self.exit_quick_threshold_pct:  # Use configured quick exit threshold
                price_change_pct = price_change * 100
                logger.info(f"🚪 EXIT LONG SIGNAL: Quick exit on {self.exit_quick_threshold_pct*100}%+ drop at ${current_price:.2f}")
                logger.info(f"   Price Change: {price_change_pct:.2f}%")
                return 1
            
            # 2. Exit if breaking below demand zones with volume
            for zone_time, zone_high, zone_low in demand_zones:
                if ((current_time - zone_time) < timedelta(hours=self.exit_zone_max_age_hours) and  # Use configured max age
                    current_price < zone_low and
                    df['volume'].iloc[-1] > df['volume'].iloc[-2] * self.exit_volume_multiplier):  # Use configured volume multiplier
                    time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                    logger.info(f"🚪 EXIT LONG SIGNAL: Volume breakdown below demand zone at ${current_price:.2f}")
                    logger.info(f"   Zone Low: ${zone_low:.2f}")
                    logger.info(f"   Zone Age: {time_diff_hours:.1f} hours")
                    return 1
            
            # 3. Exit on trend reversal (configured ADX minimum and bearish DI crossover)
            if (current_adx > self.exit_adx_min and  # Use configured ADX minimum
                df['minus_di'].iloc[-1] > df['plus_di'].iloc[-1] and
                df['minus_di'].iloc[-2] <= df['plus_di'].iloc[-2]):  # Fresh crossover
                logger.info(f"🚪 EXIT LONG SIGNAL: Bearish momentum crossover at ${current_price:.2f}")
                logger.info(f"   ADX: {current_adx:.2f} (>{self.exit_adx_min})")
                logger.info(f"   Minus DI: {df['minus_di'].iloc[-1]:.2f} > Plus DI: {df['plus_di'].iloc[-1]:.2f}")
                return 1
            
            # 4. Exit if moving averages turn bearish
            if (df['sma_20'].iloc[-1] < df['sma_50'].iloc[-1] and
                df['sma_20'].iloc[-2] >= df['sma_50'].iloc[-2]):  # Fresh crossover
                logger.info(f"🚪 EXIT LONG SIGNAL: SMA bearish crossover at ${current_price:.2f}")
                return 1
        
        elif position_type == 'short':
            # EXIT SHORT: More protective conditions
            # 1. Immediate exit on strong bullish momentum (configured quick exit threshold)
            if price_change > self.exit_quick_threshold_pct:  # Use configured quick exit threshold
                price_change_pct = price_change * 100
                logger.info(f"🚪 EXIT SHORT SIGNAL: Quick exit on {self.exit_quick_threshold_pct*100}%+ gain at ${current_price:.2f}")
                logger.info(f"   Price Change: {price_change_pct:.2f}%")
                return -1
            
            # 2. Exit if breaking above supply zones with volume
            for zone_time, zone_high, zone_low in supply_zones:
                if ((current_time - zone_time) < timedelta(hours=self.exit_zone_max_age_hours) and  # Use configured max age
                    current_price > zone_high and
                    df['volume'].iloc[-1] > df['volume'].iloc[-2] * self.exit_volume_multiplier):  # Use configured volume multiplier
                    time_diff_hours = (current_time - zone_time).total_seconds() / 3600
                    logger.info(f"🚪 EXIT SHORT SIGNAL: Volume breakout above supply zone at ${current_price:.2f}")
                    logger.info(f"   Zone High: ${zone_high:.2f}")
                    logger.info(f"   Zone Age: {time_diff_hours:.1f} hours")
                    return -1
            
            # 3. Exit on trend reversal (configured ADX minimum and bullish DI crossover)
            if (current_adx > self.exit_adx_min and  # Use configured ADX minimum
                df['plus_di'].iloc[-1] > df['minus_di'].iloc[-1] and
                df['plus_di'].iloc[-2] <= df['minus_di'].iloc[-2]):  # Fresh crossover
                logger.info(f"🚪 EXIT SHORT SIGNAL: Bullish momentum crossover at ${current_price:.2f}")
                logger.info(f"   ADX: {current_adx:.2f} (>{self.exit_adx_min})")
                logger.info(f"   Plus DI: {df['plus_di'].iloc[-1]:.2f} > Minus DI: {df['minus_di'].iloc[-1]:.2f}")
                return -1
            
            # 4. Exit if moving averages turn bullish
            if (df['sma_20'].iloc[-1] > df['sma_50'].iloc[-1] and
                df['sma_20'].iloc[-2] <= df['sma_50'].iloc[-2]):  # Fresh crossover
                logger.info(f"🚪 EXIT SHORT SIGNAL: SMA bullish crossover at ${current_price:.2f}")
                return -1
        
        return 0  # Hold position
    
    async def execute_trade(self, symbol: str, signal: int, entry_price: float, shares: int, stop_loss: float, target: float) -> Dict:
        """Execute a trade using standard LIMIT and STOP orders (Accumulate/Distribute disabled)"""
        if not self.ib_client:
            logger.error("IBKR client not initialized")
            return {'status': 'error', 'message': 'No IBKR client'}

        try:
             # Always use regular LIMIT order for parent
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
                order_status = await self.ib_client.get_order_status(parent_order_id)
                if not order_status['filled']:
                    logger.info(f"Parent order {parent_order_id} status: {order_status}")
            except Exception as status_error:
                logger.warning(f"Could not get order status: {status_error}")
            
            # Place stop-loss order (standard STOP)
            stop_order_id = None
            if stop_loss is not None:
                try:
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
            
            # Place target order (standard LIMIT)
            target_order_id = None
            if target is not None:
                try:
                    await asyncio.sleep(5)  # Additional delay before target order
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
                'order_id': parent_order_id,
                'order_type': 'LMT'
            }
            
            if stop_order_id:
                result['stop_order_id'] = stop_order_id
            if target_order_id:
                result['target_order_id'] = target_order_id
            
            trade_type = 'Long' if signal == 1 else 'Short'
            order_type_str = 'LIMIT'
            logger.info(f"Placed {'paper' if self.paper_trading else 'live'} {order_type_str} trade: {trade_type} {symbol}, "
                      f"Entry: ${entry_price:.2f}, Stop: ${stop_loss:.2f}, Target: ${target:.2f}")
            
            return result
            
        except Exception as e:
            logger.error(f"Error in execute_trade: {str(e)}")
            return {'status': 'error', 'message': str(e)}

    def calculate_current_volatility(self, entry_price: float, stop_loss: float, target: float) -> float:
        """
        Calculate current volatility based on entry, stop, and target prices.
        
        Args:
            entry_price: Entry price
            stop_loss: Stop loss price
            target: Target price
            
        Returns:
            Volatility as a decimal (e.g., 0.02 for 2%)
        """
        try:
            # Calculate price ranges
            entry_to_stop = abs(entry_price - stop_loss) / entry_price
            entry_to_target = abs(target - entry_price) / entry_price
            
            # Use the larger range as volatility indicator
            volatility = max(entry_to_stop, entry_to_target)
            
            # Cap volatility at reasonable levels
            volatility = min(volatility, 0.10)  # Max 10%
            volatility = max(volatility, 0.005)  # Min 0.5%
            
            logger.info(f"Calculated volatility: {volatility:.3f} ({volatility*100:.1f}%)")
            return volatility
            
        except Exception as e:
            logger.warning(f"Error calculating volatility, using default: {e}")
            return 0.02  # Default 2% volatility

    async def execute_smart_trade(self, symbol: str, signal: int, entry_price: float, shares: int, 
                                stop_loss: float, target: float, market_conditions: Dict = None) -> Dict:
        """
        Execute a trade with smart order selection based on market conditions.
        
        Args:
            symbol: Trading symbol
            signal: 1 for long, -1 for short
            entry_price: Entry price
            shares: Number of shares
            stop_loss: Stop loss price
            target: Target price
            market_conditions: Dict with market condition indicators
            
        Returns:
            Trade execution result
        """
        if market_conditions is None:
            market_conditions = {}
        
        # Determine optimal order type based on market conditions
        volatility = market_conditions.get('volatility', self.default_volatility)
        volume_ratio = market_conditions.get('volume_ratio', 1.0)
        trend_strength = market_conditions.get('trend_strength', 0.5)
        
        # Use accumulate/distribute for:
        # 1. Large positions (configurable threshold)
        # 2. High volatility (configurable threshold)
        # 3. Strong trends (configurable threshold)
        # 4. High volume (configurable threshold)
        use_accumulate = (
            self.use_accumulate_distribute and (
                shares >= self.large_position_threshold or 
                volatility > self.high_volatility_threshold or 
                trend_strength > self.strong_trend_threshold or 
                volume_ratio > self.high_volume_threshold
            )
        )
        
        if use_accumulate:
            logger.info(f"Smart trade: Using ACCUMULATE/DISTRIBUTE (vol: {volatility:.1%}, "
                      f"trend: {trend_strength:.1f}, volume: {volume_ratio:.1f}x)")
            
            # Adjust risk tolerance based on market conditions
            risk_tolerance = self.default_risk_tolerance  # Default moderate
            if trend_strength > 0.8:
                risk_tolerance = self.risk_tolerance_target  # More aggressive in strong trends
            elif volatility > 0.05:
                risk_tolerance = self.risk_tolerance_stop  # More conservative in high volatility
            
            return await self.ib_client.place_smart_accumulate_order(
                symbol=symbol,
                action='BUY' if signal == 1 else 'SELL',
                total_quantity=shares,
                current_price=entry_price,
                volatility=volatility,
                risk_tolerance=risk_tolerance
            )
        else:
            logger.info(f"Smart trade: Using LIMIT order (vol: {volatility:.1%}, "
                      f"trend: {trend_strength:.1f}, volume: {volume_ratio:.1f}x)")
            
            return await self.ib_client.place_order(
                symbol=symbol,
                action='BUY' if signal == 1 else 'SELL',
                quantity=shares,
                order_type='LMT',
                limit_price=entry_price
            )

    async def backtest(self, df: pd.DataFrame, initial_capital: float = 100000, symbol: str = 'MSFT') -> Dict:
        """Run a backtest on historical data and store results in ChromaDB"""
        logger.info(f"Starting backtest for {symbol} with ${initial_capital:,.2f} initial capital")
        # Apply any symbol-specific parameter overrides (PLTR tuned config)
        self.apply_symbol_overrides(symbol)
        
        # Add realistic trading conditions
        slippage_pct = 0.05  # 5 basis points slippage
        commission_per_share = 0.005  # $0.005 per share
        min_trade_delay_seconds = 2  # Minimum delay between signal and execution
        
        capital = initial_capital
        position = 0
        trades = []

        # Calculate indicators
        df = self.calculate_indicators(df)
        
        # Generate signals
        signals = self.generate_signals(df)
        
                # Track execution timing
        last_execution_time = None
        current_trade_index = None  # Track the index of current active trade
        
        for i in range(len(df)):
            current_time = df.index[i]
            current_price = df['close'].iloc[i]
            
            # Check if enough time has passed since last execution
            if last_execution_time is not None:
                time_since_last = (current_time - last_execution_time).total_seconds()
                if time_since_last < min_trade_delay_seconds:
                    continue
            
            signal = signals['signal'].iloc[i]
            
            if signal != 0 and position == 0:
                # Calculate realistic execution price with slippage
                if signal == 1:  # LONG
                    execution_price = current_price * (1 + slippage_pct / 100)
                else:  # SHORT
                    execution_price = current_price * (1 - slippage_pct / 100)
                
                # Calculate position size with risk management
                risk_amount = self.risk_per_trade
                stop_loss_pct = self.stop_loss_pct / 100
                shares = int(risk_amount / (execution_price * stop_loss_pct))
                
                # Calculate commission
                commission = shares * commission_per_share
                
                # Check if we have enough capital
                total_cost = (shares * execution_price) + commission
                if total_cost > capital:
                    shares = int((capital - commission) / execution_price)
                    total_cost = (shares * execution_price) + commission
                
                if shares > 0:
                    # Execute trade
                    capital -= total_cost
                    position = shares if signal == 1 else -shares
                    
                    # Calculate stop loss and target prices
                    stop_loss = execution_price * (1 - stop_loss_pct) if signal == 1 else execution_price * (1 + stop_loss_pct)
                    target = execution_price * (1 + self.target_pct / 100) if signal == 1 else execution_price * (1 - self.target_pct / 100)
                    
                    trade = {
                        'entry_date': current_time,
                        'entry_price': execution_price,
                        'type': 'LONG' if signal == 1 else 'SHORT',
                        'shares': shares,
                        'stop_loss': stop_loss,
                        'target': target,
                        'commission': commission
                    }
                    trades.append(trade)
                    current_trade_index = i  # Store the index of this trade
                    last_execution_time = current_time
                    
                    logger.info(f"Opened {trade['type']} position: {shares} shares at ${execution_price:.2f}, "
                              f"Stop: ${stop_loss:.2f}, Target: ${target:.2f}, Commission: ${commission:.2f}")
            
            elif position != 0:
                # Check exit conditions
                current_trade = trades[-1]
                time_held = (current_time - current_trade['entry_date']).total_seconds() / 3600
                
                # Calculate realistic exit price with slippage
                if current_trade['type'] == 'LONG':
                    exit_price = current_price * (1 - slippage_pct / 100)  # Slippage on exit for long
                else:
                    exit_price = current_price * (1 + slippage_pct / 100)  # Slippage on exit for short
                
                # Check stop loss and target
                should_exit = False
                exit_reason = ""
                
                if current_trade['type'] == 'LONG':
                    if exit_price <= current_trade['stop_loss']:
                        should_exit = True
                        exit_reason = "Stop Loss"
                    elif exit_price >= current_trade['target']:
                        should_exit = True
                        exit_reason = "Target"
                else:  # SHORT
                    if exit_price >= current_trade['stop_loss']:
                        should_exit = True
                        exit_reason = "Stop Loss"
                    elif exit_price <= current_trade['target']:
                        should_exit = True
                        exit_reason = "Target"
                
                # Check time-based exit
                if time_held >= self.max_duration_hours:
                    should_exit = True
                    exit_reason = "Time Limit"
                
                if should_exit:
                    # Calculate profit/loss with commission
                    exit_commission = current_trade['shares'] * commission_per_share
                    if current_trade['type'] == 'LONG':
                        profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] - exit_commission
                    else:
                        profit = (current_trade['entry_price'] - exit_price) * current_trade['shares'] - exit_commission
                    
                    capital += (current_trade['shares'] * exit_price) + profit
                    position = 0
                    
                    current_trade.update({
                        'exit_date': current_time,
                        'exit_price': exit_price,
                        'profit': profit,
                        'exit_reason': exit_reason,
                        'time_held_hours': time_held,
                        'exit_commission': exit_commission
                    })
                    
                    logger.info(f"Closed {current_trade['type']} position: {current_trade['shares']} shares at ${exit_price:.2f}, "
                              f"Profit: ${profit:.2f}, Reason: {exit_reason}, Time held: {time_held:.1f}h")
                    
                    current_trade_index = None  # Clear the current trade index
                    last_execution_time = current_time

        # Close any open position at the end
        if position != 0:
            current_trade = trades[-1]
            exit_price = df['close'].iloc[-1]
            exit_commission = current_trade['shares'] * commission_per_share
            
            if current_trade['type'] == 'LONG':
                profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] - exit_commission
            else:
                profit = (current_trade['entry_price'] - exit_price) * current_trade['shares'] - exit_commission
            
            capital += (current_trade['shares'] * exit_price) + profit
            current_trade.update({
                'exit_date': df.index[-1],
                'exit_price': exit_price,
                'profit': profit,
                'exit_reason': 'End of Data',
                'exit_commission': exit_commission
            })
            logger.info(f"Closed final position at ${exit_price:.2f}, profit: ${profit:.2f}")

        # Calculate success rate and total profit for ChromaDB storage
        total_trades = len(trades)
        successful_trades = sum(1 for trade in trades if trade.get('profit', 0) > 0)
        success_rate = (successful_trades / total_trades * 100) if total_trades > 0 else 0
        # FIX: Calculate total profit as final capital minus initial capital
        total_profit = capital - initial_capital
        total_commission = sum(trade.get('commission', 0) for trade in trades) + sum(trade.get('exit_commission', 0) for trade in trades)

        # Enhanced metadata with trading conditions
        metadatas = [{
            "symbol": symbol,
            "total_profit": total_profit,
            "success_rate": success_rate,
            "initial_capital": initial_capital,
            "final_capital": capital,
            "total_trades": total_trades,
            "total_commission": total_commission,
            "slippage_pct": slippage_pct,
            "commission_per_share": commission_per_share,
            "min_trade_delay_seconds": min_trade_delay_seconds,
            "trades": str(trades),
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
            "paper_trading": self.paper_trading,
            "realistic_conditions": True
        }]

        # Store results in ChromaDB
        result_id = f"{symbol}_{str(pd.Timestamp.now())}"
        self.collection.add(
            documents=[f"Realistic backtest result for {symbol}: {total_profit} profit, {success_rate}% success rate"],
            metadatas=metadatas[0],
            ids=[result_id]
        )
        logger.info(f"Stored realistic backtest results for {symbol} in ChromaDB with ID {result_id}")

        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': total_trades,
            'total_commission': total_commission,
            'slippage_pct': slippage_pct,
            'trades': trades
        }