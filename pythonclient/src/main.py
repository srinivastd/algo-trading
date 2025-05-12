"""
Main script for stock analysis and strategy backtesting with paper trading.
Version 1.5
"""
import asyncio
import logging
import argparse
import sys
from datetime import datetime, timedelta
import pytz
import random
import pandas as pd
from ibkr.connection import IBKRConnection
from ibkr.historical_data import HistoricalDataManager
from strategies.breakout_pullback import BreakoutPullbackStrategy
from strategies.supplydemand_surge import SupplyDemandSurgeStrategy
from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
from strategies.groq_breakofstructure_riskreward import GroqBreakOfStructureStrategy
from strategies.crossSectionalMomentumStrategy import CrossSectionalMomentumStrategy
from strategies.vwap_mean_reversion_strategy import VWAPTrendTradingStrategy
from ib_insync import Stock, util

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
    parser.add_argument('--paper', action='store_true', help='Use paper trading account (default: True)')
    parser.add_argument('--live', action='store_true', help='Use live trading account (WARNING: This will execute real trades)')
    parser.add_argument('--watch', action='store_true', help='Enable real-time watching mode')
    parser.add_argument('--check-interval', type=int, default=10, help='Interval between checks in seconds (default: 10)')
    parser.add_argument('--backtest-only', action='store_true', help='Run backtesting without executing trades')
    
    # Add strategy specific parameters
    parser.add_argument('--risk-per-trade', type=float, default=500.0,
                      help='Amount of money to risk per trade (default: $500.0, max: $2000.0)')
    parser.add_argument('--max-duration', type=float, default=12.0,
                      help='Maximum duration to hold a trade in hours (default: 12.0)')
    parser.add_argument('--stop-loss', type=float, default=1.0,
                      help='Stop loss percentage (default: 1.0%%)')
    parser.add_argument('--target', type=float, default=4.0,
                      help='Target profit percentage (default: 4.0%%)')
    parser.add_argument('--initial-capital', type=float, default=100000.0,
                      help='Initial capital for backtesting in dollars (default: 100000)')
    parser.add_argument('--rebalance-period', type=int, default=30, help='Rebalance period in days for momentum strategy (default: 30)')
    parser.add_argument('--vwap-threshold', type=float, default=0.01, help='VWAP deviation threshold for mean reversion (default: 0.01)')
    
    args = parser.parse_args()
    
    # Set logging level based on debug flag
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled")
    else:
        logging.getLogger().setLevel(logging.INFO)
    
    # Determine paper trading mode
    paper_mode = args.paper and not args.live  # Default to paper trading unless --live overrides
    
    # Initialize connection for both backtesting and trading
    client_id = args.client_id if args.client_id is not None else random.randint(1, 1000)
    logger.info(f"Initializing IBKR connection with client ID {client_id}...")
    
    # Use paper trading port (4002) for both backtest-only and paper trading modes
    # Only use live trading port (7497) when explicitly requested with --live
    port = 4002 if args.backtest_only or paper_mode else 7497
    connection = IBKRConnection(host="127.0.0.1", port=port, client_id=client_id)
    
    try:
        logger.info("Attempting to connect to IBKR Gateway...")
        if not await connection.connect():
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
        
        if args.watch:
            # Real-time watching mode
            logger.info(f"Starting real-time watch for {args.symbol} with check interval {args.check_interval} seconds")
            if args.strategy not in ['groq', 'groq_bos']:
                logger.error(f"Real-time watching not supported for strategy: {args.strategy}")
                return
                
            # Initialize strategy
            if args.strategy == 'groq':
                strategy = GroqRecommendationsStrategy(
                    risk_per_trade=args.risk_per_trade,
                    max_duration_hours=args.max_duration,
                    paper_trading=paper_mode,
                    stop_loss_pct=args.stop_loss,
                    target_pct=args.target
                )
            elif args.strategy == 'groq_bos':
                strategy = GroqBreakOfStructureStrategy(
                    risk_per_trade=args.risk_per_trade,
                    max_duration_hours=args.max_duration,
                    paper_trading=paper_mode
                )
            strategy.set_ib_client(connection)
            
            # Set up real-time data subscription
            contract = Stock(args.symbol, "SMART", "USD")
            await connection.get_ib().qualifyContractsAsync(contract)
            connection.get_ib().reqMktData(contract, '', False, False)
            data = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
            
            def on_data_received(bar):
                new_data = pd.DataFrame({
                    'open': [bar.open],
                    'high': [bar.high],
                    'low': [bar.low],
                    'close': [bar.close],
                    'volume': [bar.volume]
                }, index=[bar.date])
                nonlocal data
                data = pd.concat([data, new_data]).drop_duplicates().sort_index()
                logger.debug(f"Received new bar for {args.symbol}: {bar.date}, Close: {bar.close}")
            
            connection.get_ib().barUpdateEvent += on_data_received
            
            # Start watching loop
            while True:
                if len(data) >= 5:
                    df = strategy.calculate_indicators(data.copy())
                    signal = await strategy.generate_realtime_signals(df)
                    if signal != 0:
                        entry_price = data['close'].iloc[-1]
                        stop_loss = entry_price * 0.99 if signal == 1 else entry_price * 1.01
                        target = entry_price * 1.03 if signal == 1 else entry_price * 0.97
                        shares = int(args.risk_per_trade / abs(entry_price - stop_loss))
                        if shares <= 0:
                            shares = 100  # Minimum trade size
                        
                        trade_result = await strategy.execute_trade(
                            args.symbol, signal, entry_price, shares, stop_loss, target
                        )
                        if trade_result['status'] == 'success':
                            logger.info(f"Executed {'Buy' if signal == 1 else 'Sell'} trade: Entry ${entry_price:.2f}, Shares {shares}")
                        else:
                            logger.error(f"Trade failed: {trade_result['message']}")
                
                await asyncio.sleep(args.check_interval)
        
        else:
            # Backtesting mode
            et_tz = pytz.timezone('US/Eastern')
            utc_tz = pytz.UTC
            
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
            
            start_date_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
            end_date_str = end_date.strftime('%Y-%m-%d %H:%M:%S')
            
            # Get historical data using get_historical_data_range
            logger.info(f"Fetching historical data for {args.symbol}...")
            logger.info(f"Date Range (ET): {start_date.strftime('%Y-%m-%d %H:%M:%S %Z')} to {end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
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
                        max_duration_hours=args.max_duration,
                        paper_trading=not args.backtest_only,
                        stop_loss_pct=args.stop_loss,
                        target_pct=args.target
                    )
                    if not args.backtest_only and paper_mode:
                        strategy.set_ib_client(connection)
                elif args.strategy == 'groq_bos':
                    strategy = GroqBreakOfStructureStrategy(
                        risk_per_trade=args.risk_per_trade,
                        max_duration_hours=args.max_duration,
                        paper_trading=not args.backtest_only,
                        stop_loss_pct=args.stop_loss,
                        target_pct=args.target
                    )
                    if not args.backtest_only and paper_mode:
                        strategy.set_ib_client(connection)
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
                
                if args.backtest_only:
                    logger.info("Running backtest in simulation mode (no trades will be executed)...")
                else:
                    logger.info("Running backtest with paper trading enabled...")
                    
                # Validate risk per trade
                if args.risk_per_trade > 2000.0:
                    logger.warning(f"Risk per trade (${args.risk_per_trade}) exceeds maximum allowed ($2000.0). Setting to $2000.0")
                    args.risk_per_trade = 2000.0
                
                backtest_results = await strategy.backtest(df, initial_capital=args.initial_capital, symbol=args.symbol)
                
                logger.info("\nBacktest Results:")
                logger.info(f"Initial Capital: ${backtest_results['initial_capital']:,.2f}")
                logger.info(f"Final Capital: ${backtest_results['final_capital']:,.2f}")
                logger.info(f"Return: {backtest_results['return_pct']:.2f}%")
                logger.info(f"Total Trades: {backtest_results['total_trades']}")
                
                # Print basic statistics
                logger.info("\nBasic Statistics:")
                logger.info(f"Date Range: {df.index[0]} to {df.index[-1]}")
                logger.info(f"Number of bars: {len(df)}")
                logger.info(f"Current price: ${df['close'].iloc[-1]:.2f}")
                
                # Log trade details
                if backtest_results['trades']:
                    logger.info("\nTrade Details:")
                    current_capital = backtest_results['initial_capital']
                    for i, trade in enumerate(backtest_results['trades'], 1):
                        logger.info(f"\nTrade {i}:")
                        logger.info(f"Type: {trade['type']}")
                        logger.info(f"Entry Date: {trade['entry_date']}")
                        logger.info(f"Entry Price: ${trade['entry_price']:.2f}")
                        logger.info(f"Shares: {trade['shares']}")
                        logger.info(f"Position Value: ${trade['shares'] * trade['entry_price']:,.2f}")
                        logger.info(f"Capital Before Trade: ${current_capital:,.2f}")
                        logger.info(f"Stop Loss: ${trade['stop_loss']:.2f}")
                        logger.info(f"Target: ${trade['target']:.2f}")
                        if 'exit_date' in trade:
                            logger.info(f"Exit Date: {trade['exit_date']}")
                            logger.info(f"Exit Price: ${trade['exit_price']:.2f}")
                            logger.info(f"Profit: ${trade['profit']:.2f}")
                            current_capital += trade['profit']
                            logger.info(f"Capital After Trade: ${current_capital:,.2f}")
                    
                    # Calculate consolidated metrics
                    winning_trades = [t for t in backtest_results['trades'] if 'profit' in t and t['profit'] > 0]
                    losing_trades = [t for t in backtest_results['trades'] if 'profit' in t and t['profit'] <= 0]
                    
                    total_profit = sum(t['profit'] for t in winning_trades)
                    total_loss = sum(t['profit'] for t in losing_trades)
                    net_profit = total_profit + total_loss
                    
                    win_rate = len(winning_trades) / len(backtest_results['trades']) * 100 if backtest_results['trades'] else 0
                    avg_win = total_profit / len(winning_trades) if winning_trades else 0
                    avg_loss = total_loss / len(losing_trades) if losing_trades else 0
                    profit_factor = abs(total_profit / total_loss) if total_loss != 0 else float('inf')
                    
                    # Calculate trade duration metrics
                    trade_durations = []
                    for trade in backtest_results['trades']:
                        if 'exit_date' in trade:
                            duration = trade['exit_date'] - trade['entry_date']
                            trade_durations.append(duration.total_seconds() / 3600)  # Convert to hours
                    
                    avg_trade_duration = sum(trade_durations) / len(trade_durations) if trade_durations else 0
                    
                    logger.info("\n" + "="*50)
                    logger.info("CONSOLIDATED PERFORMANCE METRICS")
                    logger.info("="*50)
                    logger.info("\nTrade Statistics:")
                    logger.info(f"Total Trades: {len(backtest_results['trades'])}")
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
                    for trade in backtest_results['trades']:
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
        if 'connection' in locals() and connection.is_connected():
            await connection.disconnect()
        logger.info("Disconnected from IBKR")
        logger.info("="*50)

if __name__ == "__main__":
    util.startLoop()
    asyncio.run(main())