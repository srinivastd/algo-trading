"""
Main script for stock analysis and strategy backtesting.
"""
import asyncio
import logging
import argparse
import sys
from datetime import datetime, timedelta
import pytz
import random
from ibkr.connection import IBKRConnection
from ibkr.historical_data import HistoricalDataManager
from strategies.breakout_pullback import BreakoutPullbackStrategy
from strategies.supplydemand_surge import SupplyDemandSurgeStrategy
from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
from strategies.groq_breakofstructure_riskreward import GroqBreakOfStructureStrategy
from strategies.crossSectionalMomentumStrategy import CrossSectionalMomentumStrategy
from strategies.vwap_mean_reversion_strategy import VWAPTrendTradingStrategy

# Set up logging to both file and console with color
class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors"""
    
    grey = "\x1b[38;21m"
    blue = "\x1b[38;5;39m"
    yellow = "\x1b[38;5;226m"
    red = "\x1b[38;5;196m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"

    def __init__(self, fmt):
        super().__init__()
        self.fmt = fmt
        self.FORMATS = {
            logging.DEBUG: self.grey + self.fmt + self.reset,
            logging.INFO: self.blue + self.fmt + self.reset,
            logging.WARNING: self.yellow + self.fmt + self.reset,
            logging.ERROR: self.red + self.fmt + self.reset,
            logging.CRITICAL: self.bold_red + self.fmt + self.reset
        }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt)
        return formatter.format(record)

# Create console handler with custom formatter
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s'))

# Create file handler
file_handler = logging.FileHandler('stock_analysis.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

# Set up root logger
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG to see all messages
    handlers=[console_handler, file_handler]
)

logger = logging.getLogger(__name__)

async def main():
    parser = argparse.ArgumentParser(description='Stock Analysis with IBKR')
    parser.add_argument('symbol', type=str, help='Stock symbol (e.g., NVDA)')
    parser.add_argument('--duration', type=str, default='5 Y', help='Duration of historical data (e.g., 1 M, 1 Y, 5 Y)')
    parser.add_argument('--bar-size', type=str, default='1 min', help='Bar size (e.g., 1 min, 1 hour, 1 day)')
    parser.add_argument('--strategy', type=str, default='breakout', help='Strategy to use (e.g., breakout, supplydemand, groq, groq_bos, momentum, vwap)')
    parser.add_argument('--force-refresh', action='store_true', help='Force refresh of historical data')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--client-id', type=int, help='Client ID for IBKR connection (default: random)')
    
    # Add strategy specific parameters
    parser.add_argument('--risk-per-trade', type=float, default=500.0, help='Risk per trade in dollars (default: 500)')
    parser.add_argument('--max-duration', type=float, default=12.0, help='Maximum trade duration in hours (default: 12)')
    parser.add_argument('--rebalance-period', type=int, default=30, help='Rebalance period in days for momentum strategy (default: 30)')
    parser.add_argument('--vwap-threshold', type=float, default=0.01, help='VWAP deviation threshold for mean reversion (default: 0.01)')
    
    args = parser.parse_args()
    
    # Set logging level based on debug flag
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    else:
        logging.getLogger().setLevel(logging.INFO)
    
    logger.info("="*50)
    logger.info(f"Starting analysis for {args.symbol}")
    logger.info(f"Parameters: duration={args.duration}, bar_size={args.bar_size}, strategy={args.strategy}")
    if args.strategy in ['groq', 'groq_bos']:
        logger.info(f"Groq Strategy Parameters: risk_per_trade=${args.risk_per_trade}, max_duration={args.max_duration}h")
    elif args.strategy == 'momentum':
        logger.info(f"Momentum Strategy Parameters: risk_per_trade=${args.risk_per_trade}, rebalance_period={args.rebalance_period}d")
    elif args.strategy == 'vwap':
        logger.info(f"VWAP Strategy Parameters: risk_per_trade=${args.risk_per_trade}, threshold={args.vwap_threshold}, max_duration={args.max_duration}h")
    logger.info("="*50)
    
    # Initialize connection
    client_id = args.client_id if args.client_id is not None else random.randint(1, 1000)
    logger.info(f"Initializing IBKR connection with client ID {client_id}...")
    connection = IBKRConnection()
    
    try:
        logger.info("Attempting to connect to IBKR Gateway...")
        if not await connection.connect(client_id=client_id):
            logger.error("Failed to connect to IBKR Gateway after multiple attempts")
            return
            
        logger.info("Successfully connected to IBKR Gateway")
            
        # Get account information
        logger.info("Fetching account information...")
        accounts = await connection.get_accounts()
        logger.info("Available accounts:")
        for acc in accounts:
            logger.info(f"- {acc}")
            
        # Initialize historical data manager
        logger.info("Initializing historical data manager...")
        data_manager = HistoricalDataManager(connection)
        
        # Calculate date range based on duration parameter
        et_tz = pytz.timezone('US/Eastern')
        utc_tz = pytz.UTC
        
        # Create timezone-aware datetime objects
        end_date = datetime.now(et_tz)
        duration_parts = args.duration.split()
        if len(duration_parts) != 2:
            logger.error("Invalid duration format. Use format like '5 Y' or '1 M'")
            return
            
        duration_value = int(duration_parts[0])
        duration_unit = duration_parts[1].upper()
        
        if duration_unit == 'Y':
            start_date = end_date - timedelta(days=duration_value * 365)
        elif duration_unit == 'M':
            start_date = end_date - timedelta(days=duration_value * 30)
        elif duration_unit == 'D':
            start_date = end_date - timedelta(days=duration_value)
        else:
            logger.error("Invalid duration unit. Use Y (years), M (months), or D (days)")
            return
        
        # Convert dates to UTC for IBKR API
        start_date_utc = start_date.astimezone(utc_tz)
        end_date_utc = end_date.astimezone(utc_tz)
        
        # Format dates for API call
        start_date_str = start_date_utc.strftime('%Y-%m-%d %H:%M:%S %Z')
        end_date_str = end_date_utc.strftime('%Y-%m-%d %H:%M:%S %Z')
        
        # Get historical data using get_historical_data_range
        logger.info(f"Fetching historical data for {args.symbol}...")
        logger.info(f"Date Range (ET): {start_date.strftime('%Y-%m-%d %H:%M:%S %Z')} to {end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        logger.info(f"Date Range (UTC): {start_date_str} to {end_date_str}")
        logger.info(f"Bar Size: {args.bar_size}")
        
        df = await data_manager.get_historical_data_range(
            args.symbol,
            start_date_str,
            end_date_str,
            bar_size=args.bar_size,
            force_refresh=args.force_refresh
        )
        
        if df is not None and not df.empty:
            logger.info(f"Successfully retrieved {len(df)} bars of historical data")
            logger.info(f"Data range: {df.index[0]} to {df.index[-1]}")
            
            # Initialize and run strategy
            logger.info(f"Initializing {args.strategy} strategy...")
            if args.strategy == 'breakout':
                strategy = BreakoutPullbackStrategy()
            elif args.strategy == 'supplydemand':
                strategy = SupplyDemandSurgeStrategy()
            elif args.strategy == 'groq':
                strategy = GroqRecommendationsStrategy(
                    risk_per_trade=args.risk_per_trade,
                    max_duration_hours=args.max_duration
                )
            elif args.strategy == 'groq_bos':
                strategy = GroqBreakOfStructureStrategy(
                    risk_per_trade=args.risk_per_trade,
                    max_duration_hours=args.max_duration
                )
            elif args.strategy == 'momentum':
                strategy = CrossSectionalMomentumStrategy(
                    risk_per_trade=args.risk_per_trade,
                    rebalance_period_days=args.rebalance_period
                )
            elif args.strategy == 'vwap':
                strategy = VWAPTrendTradingStrategy(
                    risk_per_trade=args.risk_per_trade,
                    threshold=args.vwap_threshold,
                    max_duration_hours=args.max_duration
                )
            else:
                logger.error(f"Unknown strategy: {args.strategy}")
                return
                
            logger.info("Calculating technical indicators...")
            df = strategy.calculate_indicators(df)
            logger.info("Running backtest...")
            results = strategy.backtest(df)
            
            logger.info("\nBacktest Results:")
            logger.info(f"Initial Capital: ${results['initial_capital']:,.2f}")
            logger.info(f"Final Capital: ${results['final_capital']:,.2f}")
            logger.info(f"Return: {results['return_pct']:.2f}%")
            logger.info(f"Total Trades: {results['total_trades']}")
            
            # Print basic statistics
            logger.info("\nBasic Statistics:")
            logger.info(f"Date Range: {df.index[0]} to {df.index[-1]}")
            logger.info(f"Number of bars: {len(df)}")
            logger.info(f"Current price: ${df['close'].iloc[-1]:.2f}")
            
            # Log trade details
            if results['trades']:
                logger.info("\nTrade Details:")
                for i, trade in enumerate(results['trades'], 1):
                    logger.info(f"\nTrade {i}:")
                    logger.info(f"Type: {trade['type']}")
                    logger.info(f"Entry Date: {trade['entry_date']}")
                    logger.info(f"Entry Price: ${trade['entry_price']:.2f}")
                    logger.info(f"Shares: {trade['shares']}")
                    logger.info(f"Stop Loss: ${trade['stop_loss']:.2f}")
                    logger.info(f"Target: ${trade['target']:.2f}")
                    if 'exit_date' in trade:
                        logger.info(f"Exit Date: {trade['exit_date']}")
                        logger.info(f"Exit Price: ${trade['exit_price']:.2f}")
                        logger.info(f"Profit: ${trade['profit']:.2f}")
                
                # Calculate consolidated metrics
                winning_trades = [t for t in results['trades'] if 'profit' in t and t['profit'] > 0]
                losing_trades = [t for t in results['trades'] if 'profit' in t and t['profit'] <= 0]
                
                total_profit = sum(t['profit'] for t in winning_trades)
                total_loss = sum(t['profit'] for t in losing_trades)
                net_profit = total_profit + total_loss
                
                win_rate = len(winning_trades) / len(results['trades']) * 100 if results['trades'] else 0
                avg_win = total_profit / len(winning_trades) if winning_trades else 0
                avg_loss = total_loss / len(losing_trades) if losing_trades else 0
                profit_factor = abs(total_profit / total_loss) if total_loss != 0 else float('inf')
                
                # Calculate trade duration metrics
                trade_durations = []
                for trade in results['trades']:
                    if 'exit_date' in trade:
                        duration = trade['exit_date'] - trade['entry_date']
                        trade_durations.append(duration.total_seconds() / 3600)  # Convert to hours
                
                avg_trade_duration = sum(trade_durations) / len(trade_durations) if trade_durations else 0
                
                logger.info("\n" + "="*50)
                logger.info("CONSOLIDATED PERFORMANCE METRICS")
                logger.info("="*50)
                logger.info("\nTrade Statistics:")
                logger.info(f"Total Trades: {len(results['trades'])}")
                logger.info(f"Winning Trades: {len(winning_trades)}")
                logger.info(f"Losing Trades: {len(losing_trades)}")
                logger.info(f"Win Rate: {win_rate:.2f}%")
                
                logger.info("\nProfit/Loss Metrics:")
                logger.info(f"Total Profit: ${total_profit:,.2f}")
                logger.info(f"Total Loss: ${total_loss:,.2f}")
                logger.info(f"Net Profit: ${net_profit:,.2f}")
                logger.info(f"Average Win: ${avg_win:,.2f}")
                logger.info(f"Average Loss: ${avg_loss:,.2f}")
                logger.info(f"Profit Factor: {profit_factor:.2f}")
                
                logger.info("\nTime-based Metrics:")
                logger.info(f"Average Trade Duration: {avg_trade_duration:.2f} hours")
                
                # Calculate monthly returns
                monthly_returns = {}
                for trade in results['trades']:
                    if 'exit_date' in trade and 'profit' in trade:
                        month_key = trade['exit_date'].strftime('%Y-%m')
                        monthly_returns[month_key] = monthly_returns.get(month_key, 0) + trade['profit']
                
                logger.info("\nMonthly Returns:")
                for month, profit in sorted(monthly_returns.items()):
                    logger.info(f"{month}: ${profit:,.2f}")
                logger.info("="*50)
            
        else:
            logger.error("No data was retrieved. Please check the logs for details.")
            
    except Exception as e:
        logger.error(f"Error occurred: {e}", exc_info=True)
        
    finally:
        logger.info("Disconnecting from IBKR...")
        await connection.disconnect()
        logger.info("Disconnected from IBKR")
        logger.info("="*50)

if __name__ == "__main__":
    asyncio.run(main()) 