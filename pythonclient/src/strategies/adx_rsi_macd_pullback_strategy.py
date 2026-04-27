"""
ADX + RSI + MACD Trend-Pullback Strategy (long-only, quality signals).
Version 1.0 - Optimized for consistent income on QBTS/AAPL/MSFT/TSLA/PLTR/NVDA/META/GOOG.
Fully compatible with Groq pattern, IBKR, ChromaDB memory, journal, watch mode.
"""
import pandas as pd
import numpy as np
import logging
from datetime import timedelta
from typing import Tuple, Optional, Union, Dict, Any
from .base import StrategyBase, normalize_strategy_exit_signal, suppress_strategy_exit_if_scratch_win_zone
import chromadb
from chromadb.utils import embedding_functions
from ib_insync import Stock

logger = logging.getLogger(__name__)

class AdxRsiMacdTrendPullbackStrategy(StrategyBase):
    STRATEGY_SLUG = "adx_rsi_macd"

    def __init__(self,
                 risk_per_trade: float = 3000,
                 max_duration_hours: float = 12.0,
                 paper_trading: bool = True,
                 stop_loss_pct: float = 1.5,      # ~1.5x ATR style
                 target_pct: float = 4.0,
                 bar_size: str = "1 hour",
                 duration: str = "1 M",
                 adx_threshold: float = 35.0,      # bumped for quality
                 rsi_entry_cross: float = 45.0,    # cross above
                 min_hold_hours: float = 2.0,
                 position_size_cap: float = 0.95):
        super().__init__("ADX RSI MACD Trend Pullback Strategy")
        self.risk_per_trade = risk_per_trade
        self.max_duration_hours = max_duration_hours
        self.paper_trading = paper_trading
        self.stop_loss_pct = stop_loss_pct
        self.target_pct = target_pct
        self.bar_size = bar_size
        self.duration = duration
        self.adx_threshold = adx_threshold
        self.rsi_entry_cross = rsi_entry_cross
        self.min_hold_hours = min_hold_hours
        self.position_size_cap = position_size_cap
        self.ib_client = None

        # ChromaDB for backtest results (exact same pattern as Groq)
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
        except Exception as e:
            logger.warning(f"ChromaDB unavailable (backtest persistence disabled): {e}")

        logger.info(
            f"Initialized {self.name} | risk=${risk_per_trade} | ADX>{adx_threshold} & rising | "
            f"RSI cross>{rsi_entry_cross} | MACD hist rising | min_hold={min_hold_hours}h | "
            f"paper={paper_trading}"
        )

    def set_ib_client(self, ib_client):
        self.ib_client = ib_client
        logger.info("IBKR client set for ADXRSIMACD strategy")

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Exact indicators used in our 10-year backtests (manual calc to match Groq style)."""
        import pytz
        eastern = pytz.timezone('America/New_York')
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC').tz_convert(eastern)
        else:
            df.index = df.index.tz_convert(eastern)

        # ADX + DI (14) - identical to Groq
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
        df[['plus_di', 'minus_di', 'adx']] = df[['plus_di', 'minus_di', 'adx']].fillna(0)

        # RSI(14)
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(window=14).mean()
        loss = -delta.where(delta < 0, 0).rolling(window=14).mean()
        rs = gain / loss.replace(0, np.nan)
        df['rsi'] = (100 - (100 / (1 + rs))).fillna(50)

        # MACD(12,26,9)
        df['ema_12'] = df['close'].ewm(span=12, adjust=False).mean()
        df['ema_26'] = df['close'].ewm(span=26, adjust=False).mean()
        df['macd'] = df['ema_12'] - df['ema_26']
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_histogram'] = df['macd'] - df['macd_signal']

        # Trend score for regime memory (used by TradeMemory)
        df['trend_score'] = (df['adx'] / 100) * 0.4 + ((df['plus_di'] - df['minus_di']) / 100) * 0.3 + (df['macd_histogram'].clip(-3, 3) / 6) * 0.3
        df['stock_profile_score'] = df['trend_score']  # simple but effective for Chroma recall

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Robust long-only signals with proper warm-up, NaN safety, and debug logging."""
        if len(df) < 40:  # Need enough bars for reliable indicators
            logger.warning(f"[{self.STRATEGY_SLUG}] Insufficient bars for signals: only {len(df)}")
            signals = pd.DataFrame(index=df.index)
            signals['signal'] = 0
            signals['confidence_score'] = 0.0
            signals['stock_profile_score'] = 0.0
            return signals

        # Make a copy and calculate fresh indicators
        df = self.calculate_indicators(df.copy())

        signals = pd.DataFrame(index=df.index)
        signals['signal'] = 0
        signals['confidence_score'] = 0.0
        # Proper Series assignment (fixes scalar bug)
        signals['stock_profile_score'] = df.get('stock_profile_score', pd.Series(0.0, index=df.index))

        signal_count = 0
        skipped_reasons = {'low_adx': 0, 'not_rising': 0, 'di_wrong': 0, 'rsi_no_cross': 0, 'macd_no_rise': 0}

        for i in range(30, len(df) - 1):   # Start after indicators are stable (was range(2, ...))
            try:
                adx = float(df['adx'].iloc[i])
                adx_prev = float(df['adx'].iloc[i-1])
                plus_di = float(df['plus_di'].iloc[i])
                minus_di = float(df['minus_di'].iloc[i])
                rsi = float(df['rsi'].iloc[i])
                rsi_prev = float(df['rsi'].iloc[i-1])
                macd_hist = float(df['macd_histogram'].iloc[i])
                macd_hist_prev = float(df['macd_histogram'].iloc[i-1])
            except (IndexError, ValueError, TypeError):
                continue

            # Skip if any key value is NaN
            if any(np.isnan(x) for x in [adx, adx_prev, plus_di, minus_di, rsi, rsi_prev, macd_hist, macd_hist_prev]):
                continue

            # QUALITY LONG ENTRY CONDITIONS
            if (adx > self.adx_threshold and 
                adx > adx_prev and                    # ADX rising
                plus_di > minus_di and                # Uptrend direction
                rsi > self.rsi_entry_cross and 
                rsi_prev <= self.rsi_entry_cross and  # RSI pullback cross
                macd_hist > 0 and 
                macd_hist > macd_hist_prev):          # MACD momentum rising

                confidence = 0.75 + (0.25 if adx > 40 else 0.0)
                signals.loc[signals.index[i], 'signal'] = 1
                signals.loc[signals.index[i], 'confidence_score'] = min(confidence, 1.0)
                signal_count += 1

                logger.debug(f"[{df.index[i]}] LONG SIGNAL | ADX={adx:.1f}↑ RSI={rsi:.1f} (cross) MACD_hist={macd_hist:.4f}↑")

            else:
                # Optional: count skipped reasons for debugging
                if adx <= self.adx_threshold:
                    skipped_reasons['low_adx'] += 1
                elif adx <= adx_prev:
                    skipped_reasons['not_rising'] += 1
                elif plus_di <= minus_di:
                    skipped_reasons['di_wrong'] += 1
                elif not (rsi > self.rsi_entry_cross and rsi_prev <= self.rsi_entry_cross):
                    skipped_reasons['rsi_no_cross'] += 1
                elif not (macd_hist > 0 and macd_hist > macd_hist_prev):
                    skipped_reasons['macd_no_rise'] += 1

        logger.info(f"[{self.STRATEGY_SLUG}] Generated {signal_count} LONG signals on {len(df)} bars "
                    f"(ADX>{self.adx_threshold}, RSI cross>{self.rsi_entry_cross})")
        if signal_count == 0:
            logger.info(f"Skipped reasons: {skipped_reasons}")

        return signals

    async def generate_realtime_signals(self, df: pd.DataFrame) -> int:
        """Realtime last-bar signal (watch mode)."""
        if len(df) < 3:
            return 0
        df = self.calculate_indicators(df)
        i = len(df) - 1
        return 1 if self.generate_signals(df)['signal'].iloc[-1] == 1 else 0   # reuse same logic

    def _long_exit_bar_detail(self, df: pd.DataFrame) -> Optional[str]:
        """2-bar confirmed exit logic (RSI>70, MACD flip, ADX collapse)."""
        if len(df) < 2:
            return None
        row = df.iloc[-1]
        if (row['rsi'] > 70 or
            row['macd_histogram'] < 0 or
            row['adx'] < 25):
            return "overbought_or_trend_weak"
        return None

    async def generate_exit_signals(self, df: pd.DataFrame, position_type: str) -> Union[int, Tuple[int, str]]:
        """2 consecutive bar confirmation (exact Groq pattern)."""
        if len(df) < 4 or position_type != "long":
            return 0
        d_last = self.calculate_indicators(df)
        d_prev = self.calculate_indicators(df.iloc[:-1])
        r_last = self._long_exit_bar_detail(d_last)
        r_prev = self._long_exit_bar_detail(d_prev)
        if r_last and r_prev:
            tag = r_last if r_last == r_prev else f"2bar_mix:{r_last}+{r_prev}"
            logger.info(f"🚪 EXIT LONG (2-bar): {tag}")
            return (1, f"strategy_exit:{tag}")
        return (0, "")

    async def backtest(self, df: pd.DataFrame, initial_capital: float = 100000, symbol: str = "NVDA", **kwargs) -> Dict:
        """Full backtest using generate_signals + generate_exit_signals. Supports extended hours data."""
        logger.info(f"Running ADXRSIMACD backtest on {symbol} with {len(df)} bars (extended hours supported)")

        # Reuse Groq-style logic but with our signals/exits
        signals = self.generate_signals(df)
        capital = float(initial_capital)
        position = 0
        trades = []
        strategy_exit_signals = bool(kwargs.get("strategy_exit_signals", True))
        strategy_exit_min_unrealized_pct = float(kwargs.get("strategy_exit_min_unrealized_pct", 0.0))
        min_hold_hours = float(kwargs.get("min_hold_hours", self.min_hold_hours))

        for i in range(1, len(df) - 1):
            sig = int(signals['signal'].iloc[i]) if 'signal' in signals.columns else 0
            if sig != 0 and position == 0:
                entry_price = float(df['close'].iloc[i])
                entry_time = df.index[i]
                is_long = sig > 0

                stop_loss = entry_price * (1 - self.stop_loss_pct / 100) if is_long else entry_price * (1 + self.stop_loss_pct / 100)
                target = entry_price * (1 + self.target_pct / 100) if is_long else entry_price * (1 - self.target_pct / 100)

                risk_per_share = abs(entry_price - stop_loss)
                if risk_per_share <= 0:
                    continue

                shares = max(1, int(self.risk_per_trade // risk_per_share))  # min 1 share
                max_by_capital = int((self.position_size_cap * capital) // entry_price)
                if max_by_capital > 0:
                    shares = min(shares, max_by_capital)

                position = shares if is_long else -shares
                trades.append({
                    'entry_date': entry_time,
                    'entry_price': entry_price,
                    'shares': abs(shares),
                    'stop_loss': stop_loss,
                    'target': target,
                    'type': 'LONG' if is_long else 'SHORT'
                })
                logger.debug(f"Sim entry {trades[-1]['type']} {symbol} @ ${entry_price:.2f}, shares={shares}")

            elif position != 0:
                current_price = float(df['close'].iloc[i])
                current_time = df.index[i]
                time_held = (current_time - trades[-1]['entry_date']).total_seconds() / 3600.0
                current_trade = trades[-1]

                # Time-based or stop/target exit
                if time_held >= self.max_duration_hours or (
                    (current_trade['type'] == 'LONG' and (current_price <= current_trade['stop_loss'] or current_price >= current_trade['target'])) or
                    (current_trade['type'] == 'SHORT' and (current_price >= current_trade['stop_loss'] or current_price <= current_trade['target']))
                ):
                    exit_price = current_price
                    profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if current_trade['type'] == 'LONG' else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                    capital += profit
                    position = 0
                    current_trade.update({'exit_date': current_time, 'exit_price': exit_price, 'profit': profit, 'exit_reason': 'stop_target_or_timeout'})
                    continue

                # Strategy programmatic exit (respects min_hold_hours)
                if strategy_exit_signals and time_held >= min_hold_hours:
                    hist = df.iloc[:i+1]
                    if len(hist) >= 4:
                        # Bug 1 fix: Use correct position type (long/short) instead of always "long"
                        pos_type = "long" if current_trade.get('type') == 'LONG' else "short"
                        raw_exit = await self.generate_exit_signals(hist, pos_type)
                        es, er = normalize_strategy_exit_signal(raw_exit)
                        if es != 0:
                            exit_price = current_price
                            profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if current_trade.get('type') == 'LONG' else (current_trade['entry_price'] - exit_price) * current_trade['shares']
                            capital += profit
                            position = 0
                            current_trade.update({
                                'exit_date': current_time,
                                'exit_price': exit_price,
                                'profit': profit,
                                'exit_reason': er or "strategy_exit"
                            })
                            logger.info(f"Strategy exit on {symbol} ({pos_type}) after {time_held:.1f}h, profit ${profit:.2f}")
                            continue

        # Close any remaining position
        if position != 0 and trades:
            current_trade = trades[-1]
            exit_price = float(df['close'].iloc[-1])
            profit = (exit_price - current_trade['entry_price']) * current_trade['shares'] if current_trade['type'] == 'LONG' else (current_trade['entry_price'] - exit_price) * current_trade['shares']
            capital += profit
            current_trade.update({
                'exit_date': df.index[-1],
                'exit_price': exit_price,
                'profit': profit,
                'exit_reason': 'end_of_data'
            })

        total_trades = len(trades)
        total_profit = sum(t.get('profit', 0) for t in trades)
        success_rate = sum(1 for t in trades if t.get('profit', 0) > 0) / total_trades * 100 if total_trades > 0 else 0.0
        return_pct = (capital - initial_capital) / initial_capital * 100

        logger.info(f"ADXRSIMACD Backtest {symbol}: {return_pct:.2f}% return, {total_trades} trades, success {success_rate:.1f}%")

        # Store to Chroma (sanitized like Groq)
        if hasattr(self, 'collection') and self.collection is not None:
            try:
                safe_meta = self._sanitize_chroma_meta if hasattr(self, '_sanitize_chroma_meta') else lambda m: m
                meta = safe_meta({
                    "strategy": self.STRATEGY_SLUG,
                    "symbol": symbol,
                    "return_pct": return_pct,
                    "total_trades": total_trades,
                    "success_rate": success_rate,
                    "total_profit": total_profit,
                    "adx_threshold": self.adx_threshold,
                    "rsi_entry_cross": self.rsi_entry_cross,
                    "min_hold_hours": min_hold_hours,
                })
                self.collection.add(
                    documents=[f"Backtest {self.STRATEGY_SLUG} {symbol}: {return_pct:.1f}% ({total_trades} trades)"],
                    metadatas=[meta],
                    ids=[f"{self.STRATEGY_SLUG}_{symbol}_{pd.Timestamp.now().isoformat()}"],
                )
            except Exception as e:
                logger.debug(f"Chroma store failed: {e}")

        return {
            "initial_capital": initial_capital,
            "final_capital": capital,
            "return_pct": return_pct,
            "total_trades": total_trades,
            "success_rate": success_rate,
            "total_profit": total_profit,
            "trades": trades[:5]  # first 5 for summary
        }

    def _sanitize_chroma_meta(self, meta: Dict) -> Dict:
        """Convert numpy/types for Chroma (copied from Groq)."""
        sanitized = {}
        for k, v in meta.items():
            if v is None:
                continue
            if isinstance(v, (np.integer, np.floating)):
                sanitized[k] = int(v) if isinstance(v, np.integer) else float(v)
            elif isinstance(v, np.bool_):
                sanitized[k] = bool(v)
            else:
                sanitized[k] = v
        return sanitized

    # execute_trade inherited from base