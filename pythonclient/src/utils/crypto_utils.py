"""
Crypto trading utilities and configuration.
Provides clean separation between crypto and equity trading logic.
"""
import logging
from typing import Dict, List, Optional, Tuple
from ib_insync import Crypto, Stock, Contract

logger = logging.getLogger(__name__)

class CryptoConfig:
    """Configuration for crypto trading"""
    
    # Supported crypto symbols and their exchanges
    SUPPORTED_CRYPTOS = {
        'BTC': {'exchange': 'PAXOS', 'currency': 'USD', 'name': 'Bitcoin'},
        'ETH': {'exchange': 'PAXOS', 'currency': 'USD', 'name': 'Ethereum'},
        'BTCUSD': {'exchange': 'PAXOS', 'currency': 'USD', 'name': 'Bitcoin'},
        'ETHUSD': {'exchange': 'PAXOS', 'currency': 'USD', 'name': 'Ethereum'},
    }
    
    # Crypto-specific trading parameters
    CRYPTO_PARAMS = {
        'BTC': {
            'min_tick': 0.01,
            'commission_rate': 0.0005,  # 0.05% commission (realistic for IBKR)
            'volatility_multiplier': 2.0,
            'max_duration_hours': 48,
            'slippage_bps': 5   # 5 basis points slippage (realistic)
        },
        'ETH': {
            'min_tick': 0.01,
            'commission_rate': 0.0005,  # 0.05% commission (realistic for IBKR)
            'volatility_multiplier': 2.5,
            'max_duration_hours': 48,
            'slippage_bps': 5   # 5 basis points slippage (realistic)
        },
        'BTCUSD': {
            'min_tick': 0.01,
            'commission_rate': 0.0005,
            'volatility_multiplier': 2.0,
            'max_duration_hours': 48,
            'slippage_bps': 5
        },
        'ETHUSD': {
            'min_tick': 0.01,
            'commission_rate': 0.0005,
            'volatility_multiplier': 2.5,
            'max_duration_hours': 48,
            'slippage_bps': 5
        }
    }

class CryptoUtils:
    """Utility functions for crypto trading"""
    
    @staticmethod
    def is_crypto(symbol: str) -> bool:
        """Check if symbol is a crypto currency"""
        if not symbol:
            return False
        return symbol.upper() in CryptoConfig.SUPPORTED_CRYPTOS
    
    @staticmethod
    def get_crypto_contract(symbol: str) -> Optional[Crypto]:
        """Create crypto contract for IBKR"""
        symbol_upper = symbol.upper()
        
        if not CryptoUtils.is_crypto(symbol_upper):
            return None
        
        config = CryptoConfig.SUPPORTED_CRYPTOS[symbol_upper]
        
        # Handle different symbol formats
        if symbol_upper in ['BTCUSD', 'ETHUSD']:
            # For BTCUSD/ETHUSD format, use the base symbol
            base_symbol = symbol_upper[:3]  # BTC or ETH
        else:
            base_symbol = symbol_upper
        
        return Crypto(base_symbol, config['exchange'], config['currency'])
    
    @staticmethod
    def get_equity_contract(symbol: str) -> Stock:
        """Create equity contract for IBKR"""
        # Use SMART routing for most stocks, which will find the best exchange
        # SMART routing works for NYSE American (where BMNR trades) and other exchanges
        return Stock(symbol, 'SMART', 'USD')
    
    @staticmethod
    def create_contract(symbol: str) -> Contract:
        """Create appropriate contract based on symbol type"""
        if CryptoUtils.is_crypto(symbol):
            contract = CryptoUtils.get_crypto_contract(symbol)
            if contract is None:
                raise ValueError(f"Unsupported crypto symbol: {symbol}")
            return contract
        else:
            return CryptoUtils.get_equity_contract(symbol)
    
    @staticmethod
    def validate_contract(contract: Contract, symbol: str) -> bool:
        """Validate that a contract is properly qualified and active"""
        if not contract:
            logger.error(f"No contract provided for {symbol}")
            return False
            
        if not hasattr(contract, 'conId') or contract.conId <= 0:
            logger.error(f"Invalid contract ID for {symbol}: {getattr(contract, 'conId', 'None')}")
            return False
            
        # Check if contract has required fields
        if not hasattr(contract, 'symbol') or not contract.symbol:
            logger.error(f"Contract missing symbol for {symbol}")
            return False
            
        logger.info(f"Contract validation passed for {symbol}: ID={contract.conId}, Exchange={getattr(contract, 'exchange', 'Unknown')}")
        return True
    
    @staticmethod
    def get_crypto_params(symbol: str) -> Dict:
        """Get crypto-specific trading parameters"""
        symbol_upper = symbol.upper()
        
        if not CryptoUtils.is_crypto(symbol_upper):
            return {}
        
        return CryptoConfig.CRYPTO_PARAMS.get(symbol_upper, {})
    
    @staticmethod
    def calculate_crypto_commission(symbol: str, quantity: float, price: float) -> float:
        """Calculate commission for crypto trades (percentage-based)"""
        if not CryptoUtils.is_crypto(symbol):
            return 0.0
        
        params = CryptoUtils.get_crypto_params(symbol)
        commission_rate = params.get('commission_rate', 0.0005)  # Default 0.05%
        
        return quantity * price * commission_rate
    
    @staticmethod
    def adjust_crypto_slippage(symbol: str, base_slippage_bps: float) -> float:
        """Adjust slippage for crypto trades"""
        if not CryptoUtils.is_crypto(symbol):
            return base_slippage_bps
        
        params = CryptoUtils.get_crypto_params(symbol)
        crypto_slippage = params.get('slippage_bps', base_slippage_bps)
        
        return crypto_slippage
    
    @staticmethod
    def get_supported_cryptos() -> List[str]:
        """Get list of supported crypto symbols"""
        return list(CryptoConfig.SUPPORTED_CRYPTOS.keys())
    
    @staticmethod
    def is_24_7_trading(symbol: str) -> bool:
        """Check if symbol supports 24/7 trading (crypto only)"""
        return CryptoUtils.is_crypto(symbol)
    
    @staticmethod
    def round_crypto_price(symbol: str, price: float) -> float:
        """Round price to conform to crypto minimum tick size"""
        if not CryptoUtils.is_crypto(symbol):
            return price
            
        params = CryptoUtils.get_crypto_params(symbol)
        min_tick = params.get('min_tick', 0.01)
        
        # Round to the nearest tick
        return round(price / min_tick) * min_tick
    
    @staticmethod
    def supports_short_selling(symbol: str) -> bool:
        """Check if symbol supports short selling"""
        if CryptoUtils.is_crypto(symbol):
            return False  # IBKR does not allow crypto short selling
        else:
            return True  # Stocks generally support short selling
    
    @staticmethod
    def supports_stop_orders(symbol: str) -> bool:
        """Check if symbol supports stop orders"""
        if CryptoUtils.is_crypto(symbol):
            return False  # IBKR does not allow stop orders for crypto
        else:
            return True  # Stocks support stop orders
    
    @staticmethod
    def get_supported_order_types(symbol: str) -> List[str]:
        """Get list of supported order types for symbol"""
        if CryptoUtils.is_crypto(symbol):
            return ['LMT', 'MKT']  # Only limit and market orders for crypto
        else:
            return ['LMT', 'MKT', 'STP']  # All order types for stocks

    @staticmethod
    def calculate_max_crypto_position(symbol: str, account_equity: float, entry_price: float, existing_position: float = 0.0) -> int:
        """Calculate maximum crypto position size based on IBKR limits"""
        if not CryptoUtils.is_crypto(symbol):
            return float('inf')  # No limit for non-crypto
        
        # IBKR crypto limits: 30% of account equity OR $3M, whichever is lower
        max_crypto_value = min(account_equity * 0.30, 3_000_000)
        
        # Add safety margin (use 25% instead of 30% to avoid hitting limit)
        safe_crypto_value = max_crypto_value * 0.83  # 25%/30% = 0.83
        
        # Calculate maximum shares accounting for existing position
        if entry_price <= 0:
            return 0
        
        # Total allowed crypto value
        max_total_shares = int(safe_crypto_value // entry_price)
        
        # Subtract existing position to get additional shares allowed
        additional_shares_allowed = max_total_shares - int(existing_position)
        
        return max(additional_shares_allowed, 0)
    
    @staticmethod
    def get_crypto_risk_multiplier(symbol: str) -> float:
        """Get risk multiplier for crypto (should be lower due to volatility)"""
        if not CryptoUtils.is_crypto(symbol):
            return 1.0
        
        # Crypto is more volatile, use smaller position sizes
        return 0.5  # Use 50% of normal risk per trade for crypto
    
    @staticmethod
    def get_trading_hours_info(symbol: str) -> Dict:
        """Get trading hours information for symbol"""
        if CryptoUtils.is_crypto(symbol):
            return {
                'is_24_7': True,
                'market_open': None,
                'market_close': None,
                'timezone': 'UTC'
            }
        else:
            return {
                'is_24_7': False,
                'market_open': '09:30:00',
                'market_close': '16:00:00',
                'timezone': 'America/New_York'
            } 