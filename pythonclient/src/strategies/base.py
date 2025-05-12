"""
Base Strategy Class
Defines the interface for all trading strategies.
"""
import pandas as pd
from typing import Dict, List, Optional
from abc import ABC, abstractmethod

class StrategyBase(ABC):
    """Base class for all trading strategies"""
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