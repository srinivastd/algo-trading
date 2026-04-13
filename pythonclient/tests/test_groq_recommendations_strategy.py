"""
Unit tests for GroqRecommendationsStrategy (no IBKR / live gateway).

Uses sys.path like other pythonclient tests so `strategies.*` resolves from ``src/``.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

try:
    from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
except ModuleNotFoundError as e:
    GroqRecommendationsStrategy = None  # type: ignore[misc, assignment]
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


def _make_strategy(**kwargs) -> GroqRecommendationsStrategy:
    with patch("strategies.groq_recommendations_strategy.chromadb.PersistentClient") as pc:
        pc.side_effect = RuntimeError("skip chroma in tests")
        return GroqRecommendationsStrategy(paper_trading=True, **kwargs)


def _trending_ohlcv(n: int = 90) -> pd.DataFrame:
    """Enough bars for sma_50 / ADX; mild uptrend."""
    idx = pd.date_range("2026-01-03 14:00", periods=n, freq="h", tz="UTC")
    t = np.linspace(0, 1, n)
    close = 100 + 8 * t + np.sin(t * 12) * 0.5
    high = close + 0.4 + np.abs(np.sin(t * 7)) * 0.3
    low = close - 0.4 - np.abs(np.cos(t * 5)) * 0.3
    open_ = np.r_[close[0], close[:-1]]
    vol = (1e6 * (1 + 0.15 * np.sin(t * 20))).astype(int)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx)


class GroqRecommendationsStrategyImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if GroqRecommendationsStrategy is None:
            raise unittest.SkipTest(f"Groq strategy import failed: {_IMPORT_ERROR}")


class GroqVolumeAndZoneHelpersTests(GroqRecommendationsStrategyImportTests):
    def test_two_bar_volume_breakout_requires_three_bars(self):
        s = _make_strategy(volume_breakout_mult=1.3, volume_confirm_mult=1.3)
        df = _trending_ohlcv(10)
        df = s.calculate_indicators(df)
        self.assertFalse(s._two_bar_volume_breakout(df, 0))
        self.assertFalse(s._two_bar_volume_breakout(df, 1))
        self.assertIsInstance(s._two_bar_volume_breakout(df, 2), bool)

    def test_two_bar_volume_false_when_middle_volume_zero(self):
        s = _make_strategy(volume_breakout_mult=1.1, volume_confirm_mult=1.1)
        idx = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100] * 6,
                "high": [101] * 6,
                "low": [99] * 6,
                "close": [100] * 6,
                "volume": [1_000_000, 1_000_000, 0, 2_000_000, 3_000_000, 4_000_000],
            },
            index=idx,
        )
        df = s.calculate_indicators(df)
        self.assertFalse(s._two_bar_volume_breakout(df, 4))

    def test_zone_still_fresh_respects_zone_freshness_hours(self):
        s = _make_strategy(zone_freshness_hours=4.0)
        zt = pd.Timestamp("2026-01-01 10:00", tz="America/New_York")
        bt = zt + pd.Timedelta(hours=3)
        self.assertTrue(s._zone_still_fresh(bt, zt))
        bt2 = zt + pd.Timedelta(hours=5)
        self.assertFalse(s._zone_still_fresh(bt2, zt))


class GroqShortRealtimeFiltersTests(GroqRecommendationsStrategyImportTests):
    def _df_tail(self, adx: float, rsi: float, plus_di: float, minus_di: float) -> pd.DataFrame:
        n = 5
        idx = pd.date_range("2026-01-10", periods=n, freq="h", tz="UTC")
        d = pd.DataFrame(
            {
                "adx": [25.0] * n,
                "rsi": [50.0] * n,
                "plus_di": [15.0] * n,
                "minus_di": [25.0] * n,
                "macd_histogram": [0.0] * n,
            },
            index=idx,
        )
        d.iloc[-1, d.columns.get_loc("adx")] = adx
        d.iloc[-1, d.columns.get_loc("rsi")] = rsi
        d.iloc[-1, d.columns.get_loc("plus_di")] = plus_di
        d.iloc[-1, d.columns.get_loc("minus_di")] = minus_di
        return d

    def test_filters_disabled_always_pass(self):
        s = _make_strategy(short_realtime_filters_enabled=False)
        df = self._df_tail(adx=10, rsi=80, plus_di=40, minus_di=10)
        ok, _ = s._passes_short_realtime_filters(df)
        self.assertTrue(ok)

    def test_block_low_adx(self):
        s = _make_strategy(short_realtime_filters_enabled=True)
        df = self._df_tail(adx=15, rsi=50, plus_di=10, minus_di=30)
        ok, why = s._passes_short_realtime_filters(df)
        self.assertFalse(ok)
        self.assertIn("block_adx", why)

    def test_block_high_rsi(self):
        s = _make_strategy(short_realtime_filters_enabled=True)
        df = self._df_tail(adx=28, rsi=80, plus_di=10, minus_di=30)
        ok, why = s._passes_short_realtime_filters(df)
        self.assertFalse(ok)
        self.assertIn("block_rsi", why)

    def test_pass_typical_bearish_di(self):
        s = _make_strategy(short_realtime_filters_enabled=True)
        df = self._df_tail(adx=28, rsi=55, plus_di=12, minus_di=28)
        ok, why = s._passes_short_realtime_filters(df)
        self.assertTrue(ok, why)


class GroqRealtimeSignalsTests(GroqRecommendationsStrategyImportTests):
    def test_realtime_returns_zero_if_fewer_than_three_bars(self):
        s = _make_strategy()
        idx = pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")
        df = pd.DataFrame(
            {"open": [1, 1], "high": [2, 2], "low": [0.5, 0.5], "close": [1, 1], "volume": [100, 200]},
            index=idx,
        )
        sig = asyncio.run(s.generate_realtime_signals(df))
        self.assertEqual(sig, 0)

    def test_realtime_runs_on_synthetic_history(self):
        s = _make_strategy()
        df = _trending_ohlcv(90)
        sig = asyncio.run(s.generate_realtime_signals(df))
        self.assertIn(sig, (-1, 0, 1))


class GroqGenerateSignalsTests(GroqRecommendationsStrategyImportTests):
    def test_generate_signals_shape_and_columns(self):
        s = _make_strategy()
        df = _trending_ohlcv(90)
        df = s.calculate_indicators(df)
        sig = s.generate_signals(df)
        self.assertEqual(len(sig), len(df))
        self.assertIn("signal", sig.columns)
        self.assertIn("confidence_score", sig.columns)
        self.assertTrue(sig["signal"].isin([-1, 0, 1]).all())

    def test_final_index_not_written_by_loop(self):
        """``generate_signals`` iterates ``i < len(df)-1``, so the last row stays 0 unless filled elsewhere."""
        s = _make_strategy()
        df = _trending_ohlcv(20)
        df = s.calculate_indicators(df)
        sig = s.generate_signals(df)
        self.assertEqual(int(sig["signal"].iloc[-1]), 0)


class GroqBacktestSmokeTests(GroqRecommendationsStrategyImportTests):
    def test_backtest_returns_expected_keys(self):
        s = _make_strategy(risk_per_trade=500.0)
        df = _trending_ohlcv(100)
        df = s.calculate_indicators(df)
        out = asyncio.run(s.backtest(df, initial_capital=50_000.0, symbol="TEST"))
        for key in ("initial_capital", "final_capital", "return_pct", "total_trades", "trades"):
            self.assertIn(key, out)
        self.assertIsInstance(out["trades"], list)


if __name__ == "__main__":
    unittest.main()
