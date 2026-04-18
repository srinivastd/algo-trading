"""
Base Strategy Class
Defines the interface for all trading strategies.
"""
import pandas as pd
from typing import Dict, List, Optional, Tuple, Union
from abc import ABC, abstractmethod

class StrategyBase(ABC):
    """Base class for all trading strategies"""

    # CLI / ChromaDB `strategy` metadata slug (override in subclasses)
    STRATEGY_SLUG = "unknown"

    def __init__(self, name: str):
        self.name = name
        self.indicators = {}
        
    @abstractmethod
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate strategy-specific indicators"""
        pass
        
    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Generate trading signals"""
        pass
        
    async def generate_realtime_signals(self, df: pd.DataFrame, symbol: str = '') -> int:
        """Generate real-time trading signals (entry signals)
        
        Args:
            df: DataFrame with current market data and indicators
            symbol: Symbol being traded (optional for backward compatibility)
            
        Returns:
            int: Signal value (1 for buy, -1 for sell, 0 for no action)
        """
        # Default implementation can be overridden by specific strategies
        # Pass symbol to generate_signals if the method supports it
        try:
            signals = self.generate_signals(df, symbol)
        except TypeError:
            # Fallback for strategies that don't support symbol parameter
            signals = self.generate_signals(df)
        
        if len(signals) > 0:
            return int(signals['signal'].iloc[-1])
        return 0
        
    async def generate_exit_signals(
        self, df: pd.DataFrame, position_type: str
    ) -> Union[int, Tuple[int, str]]:
        """Generate exit signals for existing positions.

        Returns:
            ``(signal, reason)`` where signal is 1 / -1 / 0 and reason is a short tag for the journal
            (empty string when holding). Implementations may still return a bare int ``0`` for hold-only
            backward compatibility; ``main.watch_mode`` normalizes both shapes.
        """
        return (0, "")
        
    @abstractmethod
    async def backtest(self, df: pd.DataFrame, initial_capital: float = 100000, symbol: str = 'MSFT') -> Dict:
        """Run backtest on historical data
        
        Args:
            df: DataFrame with historical price data
            initial_capital: Starting capital for the backtest
            symbol: Trading symbol for the backtest
            
        Returns:
            Dict containing backtest results including:
            - initial_capital: Starting capital
            - final_capital: Ending capital
            - return_pct: Percentage return
            - total_trades: Number of trades executed
            - trades: List of trade details
        """
        pass
        
    def get_name(self) -> str:
        """Get strategy name"""
        return self.name 