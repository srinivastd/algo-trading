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
    def backtest(self, df: pd.DataFrame, initial_capital: float = 100000) -> Dict:
        """Run backtest on historical data"""
        pass
        
    def get_name(self) -> str:
        """Get strategy name"""
        return self.name 