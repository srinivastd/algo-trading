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
    """
    Supply/demand zones with ADX and duration tuning, plus paper trading.

    Short entries use ``_passes_short_realtime_filters`` in both backtest (``generate_signals``) and
    ``generate_realtime_signals`` so live and simulation agree.
    """

    STRATEGY_SLUG = "groq"

    def __init__(self, risk_per_trade: float = 3000, max_duration_hours: float = 12.0, paper_trading: bool = True,
                 stop_loss_pct: float = 1.0, target_pct: float = 4.0, bar_size: str = "1 hour", duration: str = "1 M",
                 zone_lookback: int = 4, adx_min: float = 18.0,
                 zone_freshness_hours: float = 8.0,
                 volume_breakout_mult: float = 1.3,
                 volume_confirm_mult: float = 1.3,
                 position_size_cap: float = 0.95,
                 # Realtime SHORT gates aligned with journal post-mortem (chop + momentum long = bad shorts).
                 short_realtime_filters_enabled: bool = True,
                 short_realtime_block_adx_below: float = 20.0,
                 short_realtime_block_rsi_above: float = 72.0,
                 short_realtime_block_bullish_di_rsi_above: float = 55.0,
                 short_realtime_require_adx_min: float = 25.0,
                 short_realtime_require_rsi_max: float = 65.0,
                 short_realtime_require_minus_di_gt_plus_di: bool = True,
                 short_realtime_max_macd_histogram: float | None = None,
                 # Long-side entry controls
                 long_require_sma_trend: bool = True,
                 long_realtime_filters_enabled: bool = False,
                 long_block_high_adx: float | None = None,
                 long_block_high_adx_rsi: float | None = None,
                 long_di_mode: str = "off",
                 long_di_adx_floor: float = 0.0,
                 long_di_pullback_spread: float = 5.0,
                 max_atr_pct: float | None = None,
                 max_volume_ratio: float | None = None):
        super().__init__("Groq Recommendations Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        self.paper_trading = paper_trading
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.ib_client = None
        self.bar_size = bar_size
        self.duration = duration
        self.zone_lookback = zone_lookback
        self.adx_min = adx_min
        self.zone_freshness_hours = zone_freshness_hours
        self.volume_breakout_mult = volume_breakout_mult
        self.volume_confirm_mult = volume_confirm_mult
        self.position_size_cap = position_size_cap
        self.short_realtime_filters_enabled = bool(short_realtime_filters_enabled)
        self.short_realtime_block_adx_below = float(short_realtime_block_adx_below)
        self.short_realtime_block_rsi_above = float(short_realtime_block_rsi_above)
        self.short_realtime_block_bullish_di_rsi_above = float(short_realtime_block_bullish_di_rsi_above)
        self.short_realtime_require_adx_min = float(short_realtime_require_adx_min)
        self.short_realtime_require_rsi_max = float(short_realtime_require_rsi_max)
        self.short_realtime_require_minus_di_gt_plus_di = bool(short_realtime_require_minus_di_gt_plus_di)
        self.short_realtime_max_macd_histogram = short_realtime_max_macd_histogram
        self.long_require_sma_trend = bool(long_require_sma_trend)
        self.long_realtime_filters_enabled = bool(long_realtime_filters_enabled)
        self.long_block_high_adx = long_block_high_adx
        self.long_block_high_adx_rsi = long_block_high_adx_rsi
        self.long_di_mode = long_di_mode if long_di_mode in ("off", "trend", "pullback") else "off"
        self.long_di_adx_floor = float(long_di_adx_floor)
        self.long_di_pullback_spread = float(long_di_pullback_spread)
        self.max_atr_pct = float(max_atr_pct) if max_atr_pct is not None else None
        self.max_volume_ratio = float(max_volume_ratio) if max_volume_ratio is not None else None
        self.chroma_client = None
        self.collection = None
        try:
            self.chroma_client = chromadb.PersistentClient(path="./chroma_db")
            self.collection = self.chroma_client.get_or_create_collection(
                name="backtest_results",
                embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                ),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as chroma_err:
            # Chroma's Rust layer can raise pyo3 PanicException (not a subclass of Exception).
            logger.warning(
                "ChromaDB unavailable (backtest results will not be persisted): %s",
                chroma_err,
            )
        logger.info(f"Initialized {self.name} with risk_per_trade=${risk_per_trade}, max_duration={max_duration_hours}h, "
                   f"stop_loss={stop_loss_pct}%, target={target_pct}%, paper_trading={paper_trading}, "
                   f"bar_size={bar_size}, duration={duration}")
        if self.short_realtime_filters_enabled:
            logger.info(
                "Realtime SHORT filters ON: block adx<%s rsi>%s | (+di>-di & rsi>%s); "
                "require adx>=%s rsi<=%s minus_di>plus_di=%s; macd_hist_max=%s",
                self.short_realtime_block_adx_below,
                self.short_realtime_block_rsi_above,
                self.short_realtime_block_bullish_di_rsi_above,
                self.short_realtime_require_adx_min,
                self.short_realtime_require_rsi_max,
                self.short_realtime_require_minus_di_gt_plus_di,
                self.short_realtime_max_macd_histogram
                if self.short_realtime_max_macd_histogram is not None
                else "off",
            )
        logger.info(
            "Long controls: sma_trend=%s, realtime_filters=%s, di_mode=%s, "
            "max_atr_pct=%s, max_volume_ratio=%s",
            self.long_require_sma_trend,
            self.long_realtime_filters_enabled,
            self.long_di_mode,
            self.max_atr_pct if self.max_atr_pct is not None else "off",
            self.max_volume_ratio if self.max_volume_ratio is not None else "off",
        )

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
        df['volume_change'] = df['volume'].replace(0, np.nan).pct_change().abs().fillna(0)
        
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
        atr_safe = df['atr'].replace(0, np.nan)
        df['plus_di'] = 100 * df['plus_dm'].rolling(window=14).mean() / atr_safe
        df['minus_di'] = 100 * df['minus_dm'].rolling(window=14).mean() / atr_safe
        di_sum = (df['plus_di'] + df['minus_di']).replace(0, np.nan)
        df['dx'] = 100 * np.abs(df['plus_di'] - df['minus_di']) / di_sum
        df['adx'] = df['dx'].rolling(window=14).mean()
        df[['plus_di', 'minus_di', 'dx', 'adx']] = df[['plus_di', 'minus_di', 'dx', 'adx']].fillna(0)

        # Enhanced stock profiling features (inspired by paper's deep stock profiles)
        # RSI (Relative Strength Index)
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi'] = (100 - (100 / (1 + rs))).fillna(50)

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
        bb_width = (df['bb_upper'] - df['bb_lower']).replace(0, np.nan)
        df['bb_position'] = ((df['close'] - df['bb_lower']) / bb_width).fillna(0.5)

        # Volume indicators
        df['volume_sma_20'] = df['volume'].rolling(window=20).mean()
        df['volume_ratio'] = (df['volume'] / df['volume_sma_20'].replace(0, np.nan)).fillna(0)

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
        
        lb = self.zone_lookback
        for i in range(lb, len(df) - lb):
            if (df['close'].iloc[i] > df['close'].iloc[i-lb:i].max() and
                df['adx'].iloc[i] > self.adx_min):
                supply_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
            elif (df['close'].iloc[i] < df['close'].iloc[i-lb:i].min() and
                  df['adx'].iloc[i] > self.adx_min):
                demand_zones.append((df.index[i], df['high'].iloc[i], df['low'].iloc[i]))
        
        return supply_zones, demand_zones

    def _passes_short_realtime_filters(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        Journal-aligned gates before accepting a supply (-1) entry.

        Block (veto) if any: ADX < 20, RSI > 72, or (+DI > −DI and RSI > 55).
        Then require all: ADX ≥ 25, RSI ≤ 65, −DI > +DI.
        Optional: macd_histogram cap (off by default).
        """
        if not self.short_realtime_filters_enabled:
            return True, ""

        row = df.iloc[-1]
        adx = float(row["adx"]) if "adx" in df.columns and pd.notna(row.get("adx")) else float("nan")
        rsi = float(row["rsi"]) if "rsi" in df.columns and pd.notna(row.get("rsi")) else float("nan")
        pdi = float(row["plus_di"]) if "plus_di" in df.columns and pd.notna(row.get("plus_di")) else float("nan")
        mdi = float(row["minus_di"]) if "minus_di" in df.columns and pd.notna(row.get("minus_di")) else float("nan")
        hist = float(row["macd_histogram"]) if "macd_histogram" in df.columns and pd.notna(row.get("macd_histogram")) else 0.0

        # --- Hard blocks (match losing-short regimes: chop, parabolic RSI, mild bullish DI) ---
        if (
            self.short_realtime_block_adx_below > 0
            and not np.isnan(adx)
            and adx < self.short_realtime_block_adx_below
        ):
            return False, f"block_adx={adx:.2f}<{self.short_realtime_block_adx_below}"
        if (
            self.short_realtime_block_rsi_above > 0
            and not np.isnan(rsi)
            and rsi > self.short_realtime_block_rsi_above
        ):
            return False, f"block_rsi={rsi:.2f}>{self.short_realtime_block_rsi_above}"
        if (
            self.short_realtime_block_bullish_di_rsi_above > 0
            and not np.isnan(rsi)
            and not np.isnan(pdi)
            and not np.isnan(mdi)
            and pdi > mdi
            and rsi > self.short_realtime_block_bullish_di_rsi_above
        ):
            return (
                False,
                f"block_bullish_di rsi={rsi:.2f}>{self.short_realtime_block_bullish_di_rsi_above} "
                f"with plus_di={pdi:.2f}>minus_di={mdi:.2f}",
            )

        # --- Require all (trend + not extended long + bearish DI); 0 = skip that check ---
        if (
            self.short_realtime_require_adx_min > 0
            and not np.isnan(adx)
            and adx < self.short_realtime_require_adx_min
        ):
            return False, f"need_adx>={self.short_realtime_require_adx_min} got {adx:.2f}"
        if (
            self.short_realtime_require_rsi_max > 0
            and not np.isnan(rsi)
            and rsi > self.short_realtime_require_rsi_max
        ):
            return False, f"need_rsi<={self.short_realtime_require_rsi_max} got {rsi:.2f}"
        if self.short_realtime_require_minus_di_gt_plus_di:
            if np.isnan(mdi) or np.isnan(pdi) or mdi <= pdi:
                return False, f"need_minus_di>plus_di got plus_di={pdi:.2f} minus_di={mdi:.2f}"

        if self.short_realtime_max_macd_histogram is not None and hist > self.short_realtime_max_macd_histogram:
            return False, f"macd_histogram={hist:.6f}>{self.short_realtime_max_macd_histogram}"
        return True, ""

    def _passes_long_realtime_filters(self, df: pd.DataFrame) -> tuple[bool, str]:
        """
        Journal-aligned gates before accepting a demand (+1) entry.

        All sub-rules default to off; behaviour only changes when the caller
        opts in via constructor params or CLI flags.

        Checks (in order):
        1. Regime: ATR-as-%-of-price ceiling  (shared with shorts via max_atr_pct)
        2. Volume: volume_ratio ceiling        (shared with shorts via max_volume_ratio)
        3. ADX + RSI combined late-trend veto  (long_block_high_adx + long_block_high_adx_rsi)
        4. DI directional rule                 (long_di_mode: off / trend / pullback)
        """
        if not self.long_realtime_filters_enabled:
            return True, ""

        row = df.iloc[-1]
        close = float(row["close"]) if pd.notna(row.get("close")) else float("nan")
        adx = float(row["adx"]) if "adx" in df.columns and pd.notna(row.get("adx")) else float("nan")
        rsi = float(row["rsi"]) if "rsi" in df.columns and pd.notna(row.get("rsi")) else float("nan")
        pdi = float(row["plus_di"]) if "plus_di" in df.columns and pd.notna(row.get("plus_di")) else float("nan")
        mdi = float(row["minus_di"]) if "minus_di" in df.columns and pd.notna(row.get("minus_di")) else float("nan")
        atr = float(row["atr"]) if "atr" in df.columns and pd.notna(row.get("atr")) else float("nan")
        vol_ratio = float(row["volume_ratio"]) if "volume_ratio" in df.columns and pd.notna(row.get("volume_ratio")) else float("nan")

        # 1 — Regime: high ATR% = too volatile for new entries
        if self.max_atr_pct is not None and not np.isnan(atr) and not np.isnan(close) and close > 0:
            atr_pct = atr / close
            if atr_pct > self.max_atr_pct:
                return False, f"atr_pct={atr_pct:.4f}>{self.max_atr_pct}"

        # 2 — Volume: extreme volume_ratio = chase risk
        if self.max_volume_ratio is not None and not np.isnan(vol_ratio):
            if vol_ratio > self.max_volume_ratio:
                return False, f"volume_ratio={vol_ratio:.2f}>{self.max_volume_ratio}"

        # 3 — ADX + RSI combined: block longs in late/stretched trends
        if (
            self.long_block_high_adx is not None
            and self.long_block_high_adx_rsi is not None
            and not np.isnan(adx)
            and not np.isnan(rsi)
            and adx > self.long_block_high_adx
            and rsi > self.long_block_high_adx_rsi
        ):
            return (
                False,
                f"late_trend_veto adx={adx:.2f}>{self.long_block_high_adx} "
                f"rsi={rsi:.2f}>{self.long_block_high_adx_rsi}",
            )

        # 4 — DI directional rule
        if self.long_di_mode == "trend":
            if np.isnan(pdi) or np.isnan(mdi) or pdi <= mdi:
                return False, f"di_trend_need +di>-di got +di={pdi:.2f} -di={mdi:.2f}"
            if self.long_di_adx_floor > 0 and (np.isnan(adx) or adx < self.long_di_adx_floor):
                return False, f"di_trend_need adx>={self.long_di_adx_floor} got {adx:.2f}"
        elif self.long_di_mode == "pullback":
            if np.isnan(pdi) or np.isnan(mdi):
                return False, f"di_pullback_need indicators got +di={pdi} -di={mdi}"
            spread = mdi - pdi
            if spread < self.long_di_pullback_spread:
                return False, f"di_pullback_need (-di)-(+di)>={self.long_di_pullback_spread} got {spread:.2f}"

        return True, ""

    def _passes_short_regime_filters(self, df: pd.DataFrame) -> tuple[bool, str]:
        """Shared regime/volume gates applied to short entries (ATR%, volume_ratio)."""
        row = df.iloc[-1]
        close = float(row["close"]) if pd.notna(row.get("close")) else float("nan")
        atr = float(row["atr"]) if "atr" in df.columns and pd.notna(row.get("atr")) else float("nan")
        vol_ratio = float(row["volume_ratio"]) if "volume_ratio" in df.columns and pd.notna(row.get("volume_ratio")) else float("nan")

        if self.max_atr_pct is not None and not np.isnan(atr) and not np.isnan(close) and close > 0:
            atr_pct = atr / close
            if atr_pct > self.max_atr_pct:
                return False, f"atr_pct={atr_pct:.4f}>{self.max_atr_pct}"
        if self.max_volume_ratio is not None and not np.isnan(vol_ratio):
            if vol_ratio > self.max_volume_ratio:
                return False, f"volume_ratio={vol_ratio:.2f}>{self.max_volume_ratio}"
        return True, ""

    def _zone_still_fresh(self, bar_time, zone_time) -> bool:
        return (bar_time - zone_time) < timedelta(hours=self.zone_freshness_hours)

    def _two_bar_volume_breakout(self, df: pd.DataFrame, i: int) -> bool:
        """Same volume chain as ``generate_signals`` (bars i-2, i-1, i). Requires i >= 2."""
        if i < 2:
            return False
        v_now = df["volume"].iloc[i]
        v_prev = df["volume"].iloc[i - 1]
        v_prev2 = df["volume"].iloc[i - 2]
        if float(v_prev) <= 0 or float(v_prev2) <= 0:
            return False
        return bool(
            v_now > v_prev * self.volume_breakout_mult
            and v_prev > v_prev2 * self.volume_confirm_mult
        )

    def _supply_short_zone_hit(
        self, df: pd.DataFrame, i: int, zone_time, zone_high, zone_low
    ) -> bool:
        return (
            self._zone_still_fresh(df.index[i], zone_time)
            and float(df["close"].iloc[i]) > float(zone_high)
            and self._two_bar_volume_breakout(df, i)
        )

    def _demand_long_zone_hit(
        self, df: pd.DataFrame, i: int, zone_time, zone_high, zone_low
    ) -> bool:
        return (
            self._zone_still_fresh(df.index[i], zone_time)
            and float(df["close"].iloc[i]) < float(zone_low)
            and self._two_bar_volume_breakout(df, i)
        )

    def _short_confidence_at(self, df: pd.DataFrame, i: int) -> float:
        profile_score = df["stock_profile_score"].iloc[i] if "stock_profile_score" in df.columns else 0
        confidence = 0.5
        if profile_score < -0.3:
            confidence += 0.3
        elif profile_score < 0:
            confidence += 0.1
        if df["rsi"].iloc[i] > 70:
            confidence += 0.2
        if df["macd_histogram"].iloc[i] < 0:
            confidence += 0.1
        return float(min(confidence, 1.0))

    def _long_signal_and_confidence_at(self, df: pd.DataFrame, i: int) -> tuple[int, float]:
        """
        Long branch after demand zone + volume match (same as ``generate_signals``).
        Returns (1, confidence) or (0, 0.0).
        """
        profile_score = df["stock_profile_score"].iloc[i] if "stock_profile_score" in df.columns else 0
        confidence = 0.5
        sma_bullish = df["sma_20"].iloc[i] > df["sma_50"].iloc[i]
        if self.long_require_sma_trend and not sma_bullish:
            return 0, 0.0
        if sma_bullish:
            confidence += 0.2
        if profile_score > 0.3:
            confidence += 0.3
        elif profile_score > 0:
            confidence += 0.1
        if df["rsi"].iloc[i] < 30:
            confidence += 0.2
        if df["macd_histogram"].iloc[i] > 0:
            confidence += 0.1
        if df["bb_position"].iloc[i] < 0.2:
            confidence += 0.1
        return 1, float(min(confidence, 1.0))

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

        # i >= 2 so volume[i-2] is the true prior bar (i==1 would alias iloc[-1] in pandas).
        for i in range(2, len(df) - 1):
            current_price = df['close'].iloc[i]
            profile_score = df['stock_profile_score'].iloc[i] if 'stock_profile_score' in df.columns else 0
            signals.loc[signals.index[i], 'stock_profile_score'] = profile_score

            for zone_time, zone_high, zone_low in supply_zones:
                if self._supply_short_zone_hit(df, i, zone_time, zone_high, zone_low):
                    rok, rwhy = self._passes_short_regime_filters(df.iloc[: i + 1])
                    if not rok:
                        logger.debug("Short zone hit but regime-filtered at %s: %s", df.index[i], rwhy)
                        continue
                    ok, why = self._passes_short_realtime_filters(df.iloc[: i + 1])
                    if not ok:
                        logger.debug("Short zone hit but filtered at %s: %s", df.index[i], why)
                        continue
                    confidence = self._short_confidence_at(df, i)
                    signals.loc[signals.index[i], 'signal'] = -1
                    signals.loc[signals.index[i], 'confidence_score'] = confidence
                    logger.debug(
                        f"Short signal at {df.index[i]}, price: {current_price:.2f}, confidence: {confidence:.2f}"
                    )
                    break

            for zone_time, zone_high, zone_low in demand_zones:
                if self._demand_long_zone_hit(df, i, zone_time, zone_high, zone_low):
                    lok, lwhy = self._passes_long_realtime_filters(df.iloc[: i + 1])
                    if not lok:
                        logger.debug("Long zone hit but filtered at %s: %s", df.index[i], lwhy)
                        continue
                    long_sig, confidence = self._long_signal_and_confidence_at(df, i)
                    if long_sig == 1:
                        signals.loc[signals.index[i], 'signal'] = 1
                        signals.loc[signals.index[i], 'confidence_score'] = confidence
                        logger.debug(
                            f"Long signal at {df.index[i]}, price: {current_price:.2f}, "
                            f"confidence: {confidence:.2f}, profile: {profile_score:.3f}"
                        )
                    break

        signal_count = len(signals[signals['signal'] != 0])
        avg_confidence = signals[signals['signal'] != 0]['confidence_score'].mean() if signal_count > 0 else 0
        logger.info(f"Generated {signal_count} signals with avg confidence: {avg_confidence:.2f}")
        return signals
        
    async def generate_realtime_signals(self, df: pd.DataFrame) -> int:
        """Same entry rules as ``generate_signals`` on the latest bar (index i = len(df)-1)."""
        if len(df) < 3:
            logger.debug("Insufficient data for signal generation (need at least 3 bars)")
            return 0

        df = self.calculate_indicators(df)
        supply_zones, demand_zones = self.identify_zones(df)
        i = len(df) - 1
        current_price = float(df["close"].iloc[i])
        current_time = df.index[i]

        out = 0

        for zone_time, zone_high, zone_low in supply_zones:
            if not self._supply_short_zone_hit(df, i, zone_time, zone_high, zone_low):
                continue

            current_volume = float(df["volume"].iloc[i])
            prev_volume = float(df["volume"].iloc[i - 1])
            prev2_volume = float(df["volume"].iloc[i - 2])
            volume_ratio = (
                current_volume / prev_volume if prev_volume > 0
                else (float("inf") if current_volume > 0 else 0.0)
            )
            ratio_log = f"{volume_ratio:.2f}" if volume_ratio != float("inf") else "inf"
            current_adx = df["adx"].iloc[i] if "adx" in df.columns else 0
            time_diff_hours = (current_time - zone_time).total_seconds() / 3600

            logger.info(f"✅ SHORT CANDIDATE at {current_time}:")
            logger.info(f"   Price: ${current_price:.2f} (broke above supply zone ${zone_high:.2f})")
            logger.info(
                f"   Volume chain: {prev2_volume:,.0f} -> {prev_volume:,.0f} -> {current_volume:,.0f} "
                f"(breakout_mult={self.volume_breakout_mult}, confirm_mult={self.volume_confirm_mult}; "
                f"last/prev ratio {ratio_log})"
            )
            logger.info(f"   ADX: {current_adx:.1f}")
            logger.info(
                f"   Zone age: {time_diff_hours:.1f} h (freshness < {self.zone_freshness_hours} h)"
            )
            logger.info(f"   Zone range: ${zone_low:.2f} - ${zone_high:.2f}")
            rok, rwhy = self._passes_short_regime_filters(df.iloc[: i + 1])
            if not rok:
                logger.info(f"SHORT signal suppressed by regime filters ({rwhy})")
                continue
            ok, why = self._passes_short_realtime_filters(df.iloc[: i + 1])
            if not ok:
                logger.info(f"SHORT signal suppressed by filters ({why})")
                continue
            out = -1
            logger.info("   -> SHORT signal accepted")
            break

        for zone_time, zone_high, zone_low in demand_zones:
            if not self._demand_long_zone_hit(df, i, zone_time, zone_high, zone_low):
                continue

            current_volume = float(df["volume"].iloc[i])
            prev_volume = float(df["volume"].iloc[i - 1])
            prev2_volume = float(df["volume"].iloc[i - 2])
            volume_ratio = (
                current_volume / prev_volume if prev_volume > 0
                else (float("inf") if current_volume > 0 else 0.0)
            )
            ratio_log = f"{volume_ratio:.2f}" if volume_ratio != float("inf") else "inf"
            current_adx = df["adx"].iloc[i] if "adx" in df.columns else 0
            time_diff_hours = (current_time - zone_time).total_seconds() / 3600

            logger.info(f"✅ LONG CANDIDATE at {current_time}:")
            logger.info(f"   Price: ${current_price:.2f} (broke below demand zone ${zone_low:.2f})")
            logger.info(
                f"   Volume chain: {prev2_volume:,.0f} -> {prev_volume:,.0f} -> {current_volume:,.0f} "
                f"(breakout_mult={self.volume_breakout_mult}, confirm_mult={self.volume_confirm_mult}; "
                f"last/prev ratio {ratio_log})"
            )
            logger.info(f"   ADX: {current_adx:.1f}")
            logger.info(
                f"   Zone age: {time_diff_hours:.1f} h (freshness < {self.zone_freshness_hours} h)"
            )
            logger.info(f"   Zone range: ${zone_low:.2f} - ${zone_high:.2f}")
            lok, lwhy = self._passes_long_realtime_filters(df.iloc[: i + 1])
            if not lok:
                logger.info(f"LONG signal suppressed by filters ({lwhy})")
                continue
            long_sig, confidence = self._long_signal_and_confidence_at(df, i)
            if long_sig == 1:
                logger.info(f"   -> LONG signal accepted (confidence {confidence:.2f})")
                out = 1
            else:
                logger.info("   -> LONG suppressed (sma_trend check, same as backtest)")
            break

        return out
        
    async def generate_exit_signals(self, df: pd.DataFrame, position_type: str) -> int:
        """
        Optional live-only exits (momentum, zone reversals, ADX). ``backtest()`` does not call this;
        watch mode runs it only when ``main.py`` is started with ``--strategy-exit-signals``.
        """
        if len(df) < 3:
            return 0
        
        df = self.calculate_indicators(df)
        current_price = df['close'].iloc[-1]
        prev_price = df['close'].iloc[-2]
        current_time = df.index[-1]
        
        # Get current momentum indicators
        current_adx = df['adx'].iloc[-1] if 'adx' in df.columns else 50
        prev_price = float(prev_price)
        current_price = float(current_price)
        price_change = (
            (current_price - prev_price) / prev_price if prev_price != 0 else 0.0
        )
        
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
            # Anchor execution prices to live market snapshot (not stale backtest bar)
            # while preserving requested risk/reward proportions.
            live_entry_price = entry_price
            try:
                contract = self.ib_client.get_ib().qualifyContracts(
                    IBKRConnection.create_contract if False else None
                )
            except Exception:
                pass
            try:
                ib = self.ib_client.get_ib()
                contract = Stock(symbol.upper(), "SMART", "USD")
                qualified = await ib.qualifyContractsAsync(contract)
                if qualified:
                    ticker = ib.reqMktData(qualified[0], '', True, False)
                    await asyncio.sleep(1.0)
                    market_px = None
                    if hasattr(ticker, 'marketPrice'):
                        mp = ticker.marketPrice()
                        if mp and mp > 0:
                            market_px = float(mp)
                    if market_px is None and hasattr(ticker, 'last') and ticker.last and ticker.last > 0:
                        market_px = float(ticker.last)
                    if market_px is None and hasattr(ticker, 'close') and ticker.close and ticker.close > 0:
                        market_px = float(ticker.close)
                    if market_px is not None:
                        live_entry_price = market_px
                        logger.info(f"Using live market reference for {symbol}: ${live_entry_price:.4f}")
                    ib.cancelMktData(qualified[0])
            except Exception as market_error:
                logger.warning(f"Could not fetch live market price for {symbol}; using computed entry price: {market_error}")

            # Preserve stop/target ratios but apply them to live entry.
            if entry_price and entry_price > 0:
                if stop_loss is not None:
                    stop_ratio = abs(entry_price - stop_loss) / entry_price
                    stop_loss = live_entry_price * (1 - stop_ratio) if signal == 1 else live_entry_price * (1 + stop_ratio)
                if target is not None:
                    target_ratio = abs(target - entry_price) / entry_price
                    target = live_entry_price * (1 + target_ratio) if signal == 1 else live_entry_price * (1 - target_ratio)

            # Place parent order first
            parent_res = await self.ib_client.place_order(
                symbol=symbol,
                action='BUY' if signal == 1 else 'SELL',
                quantity=shares,
                order_type='LMT',
                limit_price=live_entry_price
            )

            if not parent_res.ok or not parent_res.order_id:
                return {
                    'status': 'error',
                    'message': f"{parent_res.message} [{parent_res.code}]",
                    'ib_place_order': parent_res.as_dict(),
                }

            parent_order_id = parent_res.order_id
            bracket_qty = (
                parent_res.quantity_submitted
                if parent_res.quantity_submitted is not None
                else shares
            )
            
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
            stop_res = None
            target_res = None

            if stop_loss is not None:
                try:
                    await asyncio.sleep(5)  # Additional delay before stop order
                    await asyncio.sleep(2)  # Additional delay before stop order
                    stop_res = await self.ib_client.place_order(
                        symbol=symbol,
                        action='SELL' if signal == 1 else 'BUY',
                        quantity=bracket_qty,
                        order_type='STP',
                        aux_price=stop_loss,
                        parent_id=parent_order_id
                    )
                    if stop_res.ok and stop_res.order_id:
                        stop_order_id = stop_res.order_id
                        logger.info(f"Stop-loss order {stop_order_id} placed successfully")
                    else:
                        logger.warning("Failed to place stop-loss order: %s", stop_res.as_dict())
                except Exception as stop_error:
                    logger.error(f"Error placing stop-loss order: {stop_error}")
            
            if target is not None:
                try:
                    await asyncio.sleep(5)  # Additional delay before target order
                    await asyncio.sleep(2)  # Additional delay before target order
                    target_res = await self.ib_client.place_order(
                        symbol=symbol,
                        action='SELL' if signal == 1 else 'BUY',
                        quantity=bracket_qty,
                        order_type='LMT',
                        limit_price=target,
                        parent_id=parent_order_id
                    )
                    if target_res.ok and target_res.order_id:
                        target_order_id = target_res.order_id
                        logger.info(f"Target order {target_order_id} placed successfully")
                    else:
                        logger.warning("Failed to place target order: %s", target_res.as_dict())
                except Exception as target_error:
                    logger.error(f"Error placing target order: {target_error}")
            
            # Return success even if child orders failed.
            # Expose stop/target actually sent to IB (after live-entry rescaling) so watch_mode
            # can align soft exits with bracket orders.
            result = {
                'status': 'success',
                'order_id': parent_order_id,
                'effective_stop_loss': float(stop_loss) if stop_loss is not None else None,
                'effective_target': float(target) if target is not None else None,
                'live_entry_anchor': float(live_entry_price),
            }

            if stop_order_id:
                result['stop_order_id'] = stop_order_id
            elif stop_loss is not None and stop_res is not None:
                result['stop_order_error'] = stop_res.as_dict()
            if target_order_id:
                result['target_order_id'] = target_order_id
            elif target is not None and target_res is not None:
                result['target_order_error'] = target_res.as_dict()
            
            trade_type = 'Long' if signal == 1 else 'Short'
            stop_s = f"${stop_loss:.2f}" if stop_loss is not None else "n/a"
            target_s = f"${target:.2f}" if target is not None else "n/a"
            entry_s = f"${live_entry_price:.2f}" if live_entry_price is not None else "n/a"
            logger.info(
                f"Placed {'paper' if self.paper_trading else 'live'} trade: "
                f"{trade_type} {symbol}, Entry: {entry_s}, Stop: {stop_s}, Target: {target_s}"
            )
            
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

                max_shares_by_capital = int((self.position_size_cap * capital) // entry_price)
                if max_shares_by_capital > 0:
                    shares = min(shares, max_shares_by_capital)
                elif shares * entry_price > capital:
                    continue  # Skip trade if we can't afford even 1 share with proper risk management
                
                # Backtest always simulates — real IBKR orders are only placed
                # from watch_mode (live monitoring), never from historical replay.
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

                # Backtest exits are always simulated (no IBKR interaction)
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
            "strategy": self.STRATEGY_SLUG,
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
            "paper_trading": self.paper_trading,
            "max_duration_hours": float(self.max_duration_hours),
            "zone_lookback": self.zone_lookback,
            "adx_min": self.adx_min,
            "zone_freshness_hours": self.zone_freshness_hours,
            "volume_breakout_mult": self.volume_breakout_mult,
            "volume_confirm_mult": self.volume_confirm_mult,
            "position_size_cap": self.position_size_cap,
            "short_realtime_filters_enabled": int(self.short_realtime_filters_enabled),
            "short_realtime_block_adx_below": float(self.short_realtime_block_adx_below),
            "short_realtime_block_rsi_above": float(self.short_realtime_block_rsi_above),
            "short_realtime_block_bullish_di_rsi_above": float(self.short_realtime_block_bullish_di_rsi_above),
            "short_realtime_require_adx_min": float(self.short_realtime_require_adx_min),
            "short_realtime_require_rsi_max": float(self.short_realtime_require_rsi_max),
            "short_realtime_require_minus_di_gt_plus_di": int(self.short_realtime_require_minus_di_gt_plus_di),
            "short_realtime_max_macd_histogram": self.short_realtime_max_macd_histogram,
            "long_require_sma_trend": int(self.long_require_sma_trend),
            "long_realtime_filters_enabled": int(self.long_realtime_filters_enabled),
            "long_block_high_adx": self.long_block_high_adx,
            "long_block_high_adx_rsi": self.long_block_high_adx_rsi,
            "long_di_mode": self.long_di_mode,
            "long_di_adx_floor": self.long_di_adx_floor,
            "long_di_pullback_spread": self.long_di_pullback_spread,
            "max_atr_pct": self.max_atr_pct,
            "max_volume_ratio": self.max_volume_ratio,
        }]

        if self.collection is not None:
            result_id = f"{symbol}_{str(pd.Timestamp.now())}"
            self.collection.add(
                documents=[
                    f"Backtest result ({self.STRATEGY_SLUG}) for {symbol}: {total_profit} profit, {success_rate}% success rate"
                ],
                metadatas=metadatas[0],
                ids=[result_id],
            )
            logger.info(f"Stored backtest results for {symbol} in ChromaDB with ID {result_id}")

        return {
            'initial_capital': initial_capital,
            'final_capital': capital,
            'return_pct': ((capital - initial_capital) / initial_capital) * 100,
            'total_trades': total_trades,
            'trades': trades
        }