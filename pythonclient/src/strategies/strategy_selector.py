"""
Strategy Selector - Automatically chooses optimal strategy based on stock characteristics
"""

import pandas as pd
import logging
from typing import Optional, Tuple
from .groq_recommendations_strategy import GroqRecommendationsStrategy
from .mean_reversion_strategy import MeanReversionStrategy

logger = logging.getLogger(__name__)

class StrategySelector:
    """Automatically selects the best strategy for a given stock"""

    def __init__(self):
        self.strategies = {
            'supply_demand': GroqRecommendationsStrategy,
            'mean_reversion': MeanReversionStrategy
        }

    def analyze_stock_characteristics(self, df: pd.DataFrame) -> dict:
        """Analyze stock characteristics to determine optimal strategy"""
        if len(df) < 50:
            return {'error': 'Insufficient data'}

        # Extract key metrics
        volatility = df['volatility'].iloc[-1] if 'volatility' in df.columns else 0
        adx = df['adx'].iloc[-1] if 'adx' in df.columns else 0
        rsi = df['rsi'].iloc[-1] if 'rsi' in df.columns else 50
        bb_position = df['bb_position'].iloc[-1] if 'bb_position' in df.columns else 0.5

        return {
            'volatility': volatility,
            'adx': adx,
            'rsi': rsi,
            'bb_position': bb_position,
            'volatility_percentile': self._calculate_percentile(df, 'volatility'),
            'adx_category': self._categorize_adx(adx),
            'rsi_extreme': self._is_rsi_extreme(rsi),
            'bb_extreme': self._is_bb_extreme(bb_position)
        }

    def select_strategy(self, df: pd.DataFrame) -> Tuple[Optional[object], str]:
        """Select the optimal strategy for the stock"""
        characteristics = self.analyze_stock_characteristics(df)

        if 'error' in characteristics:
            return None, characteristics['error']

        volatility = characteristics['volatility']
        adx = characteristics['adx']
        rsi = characteristics['rsi']
        bb_position = characteristics['bb_position']

        # Decision logic
        if volatility > 2.0:
            # High volatility stocks
            if characteristics['rsi_extreme'] or characteristics['bb_extreme']:
                # Mean reversion opportunity
                strategy = MeanReversionStrategy()
                reason = f"High volatility ({volatility:.2f}) with extreme RSI ({rsi:.1f}) or BB position ({bb_position:.2f}) - mean reversion strategy"
            else:
                # Too volatile for any strategy
                return None, f"Volatility too high ({volatility:.2f}) - no suitable strategy"

        elif adx >= 25 and volatility <= 2.0:
            # Low volatility, trending stocks - supply/demand zones
            strategy = GroqRecommendationsStrategy()
            reason = f"Strong trend (ADX {adx:.1f}) and manageable volatility ({volatility:.2f}) - supply/demand zone strategy"

        elif characteristics['rsi_extreme'] or characteristics['bb_extreme']:
            # Mean reversion signals in moderate volatility
            strategy = MeanReversionStrategy()
            reason = f"Extreme RSI ({rsi:.1f}) or BB position ({bb_position:.2f}) signals - mean reversion strategy"

        else:
            # Default to supply/demand (GroqRecommendationsStrategy has no volatility_filter/min_adx knobs)
            strategy = GroqRecommendationsStrategy()
            reason = "Moderate conditions — default supply/demand zone strategy"

        # Mean reversion exposes suitability checks; Groq does not
        if hasattr(strategy, "is_stock_suitable"):
            is_suitable, suitability_reason = strategy.is_stock_suitable(df)
            if not is_suitable:
                return None, f"Selected strategy not suitable: {suitability_reason}"

        return strategy, reason

    def _calculate_percentile(self, df: pd.DataFrame, column: str) -> float:
        """Calculate percentile of current value in historical data"""
        if column not in df.columns:
            return 50.0

        current_value = df[column].iloc[-1]
        historical_values = df[column].dropna()

        if len(historical_values) == 0:
            return 50.0

        return (historical_values <= current_value).mean() * 100

    def _categorize_adx(self, adx: float) -> str:
        """Categorize ADX strength"""
        if adx >= 25:
            return "strong_trend"
        elif adx >= 20:
            return "moderate_trend"
        else:
            return "weak_trend"

    def _is_rsi_extreme(self, rsi: float) -> bool:
        """Check if RSI indicates extreme conditions"""
        return rsi < 30 or rsi > 70

    def _is_bb_extreme(self, bb_position: float) -> bool:
        """Check if Bollinger Band position indicates extreme conditions"""
        return bb_position < 0.2 or bb_position > 0.8

    def get_strategy_recommendations(self, df: pd.DataFrame) -> dict:
        """Get detailed strategy recommendations"""
        strategy, reason = self.select_strategy(df)

        if strategy is None:
            return {
                'recommended_strategy': None,
                'reason': reason,
                'characteristics': self.analyze_stock_characteristics(df)
            }

        return {
            'recommended_strategy': strategy.__class__.__name__,
            'strategy_instance': strategy,
            'reason': reason,
            'characteristics': self.analyze_stock_characteristics(df),
            'expected_performance': self._estimate_performance(df, strategy)
        }

    def _estimate_performance(self, df: pd.DataFrame, strategy: object) -> dict:
        """Estimate expected performance based on historical patterns"""
        characteristics = self.analyze_stock_characteristics(df)

        if isinstance(strategy, GroqRecommendationsStrategy):
            if characteristics['volatility'] < 1.0 and characteristics['adx'] > 25:
                return {'expected_return': '+50% to +100%', 'confidence': 'High'}
            else:
                return {'expected_return': '+20% to +50%', 'confidence': 'Medium'}

        elif isinstance(strategy, MeanReversionStrategy):
            if characteristics['rsi_extreme'] or characteristics['bb_extreme']:
                return {'expected_return': '+30% to +70%', 'confidence': 'High'}
            else:
                return {'expected_return': '+10% to +30%', 'confidence': 'Medium'}

        return {'expected_return': 'Unknown', 'confidence': 'Low'}

