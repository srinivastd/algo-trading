"""
Main script for stock analysis and strategy backtesting with paper trading.
Version 1.7
"""
import asyncio
import logging
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta, time
from uuid import uuid4
import pytz
import random
import pandas as pd
from ibkr.connection import IBKRConnection
from ibkr.historical_data import HistoricalDataManager
from strategies.breakout_pullback import BreakoutPullbackStrategy
from strategies.supplydemand_surge import SupplyDemandSurgeStrategy
from strategies.groq_recommendations_strategy import GroqRecommendationsStrategy
from strategies.crossSectionalMomentumStrategy import CrossSectionalMomentumStrategy
from strategies.vwap_mean_reversion_strategy import VWAPTrendTradingStrategy
from strategies.mean_reversion_strategy import MeanReversionStrategy
from ib_insync import Stock, util
import json
import os
from ib_insync import ExecutionFilter
from trade_journal import TradeJournal
from trade_memory import TradeMemory

_PYTHONCLIENT_ROOT = Path(__file__).resolve().parent.parent

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

# Create a directory for market data logs
os.makedirs('market_data_logs', exist_ok=True)

# Add a new file handler for market data
market_data_handler = logging.FileHandler('market_data_logs/market_data.json', mode='a')  # Append mode
market_data_handler.setFormatter(logging.Formatter('%(message)s'))  # Raw JSON format
market_data_logger = logging.getLogger('market_data')
market_data_logger.setLevel(logging.INFO)
market_data_logger.addHandler(market_data_handler)
market_data_logger.propagate = False  # Don't propagate to root logger

# Add a test log entry to verify logger is working
market_data_logger.info(json.dumps({
    'timestamp': datetime.now(pytz.timezone('US/Eastern')).isoformat(),
    'type': 'system',
    'message': 'Market data logger initialized'
}))

# Set up root logger
logging.basicConfig(
    level=logging.DEBUG,  # Set to DEBUG to see all messages
    handlers=[console_handler, file_handler]
)

logger = logging.getLogger(__name__)

def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _infer_regime(indicators: dict) -> tuple[str, float | None, float | None, str]:
    adx = _safe_float(indicators.get("adx"))
    vol_ratio = _safe_float(indicators.get("vol_ratio"))
    sma20 = _safe_float(indicators.get("sma_20"))
    sma50 = _safe_float(indicators.get("sma_50"))
    if sma20 is not None and sma50 is not None:
        trend_direction = "up" if sma20 >= sma50 else "down"
    else:
        trend_direction = "unknown"
    if adx is not None and adx >= 30:
        regime = "trending"
    elif adx is not None and adx <= 20:
        regime = "ranging"
    elif vol_ratio is not None and vol_ratio >= 1.5:
        regime = "volatile"
    elif vol_ratio is not None and vol_ratio <= 0.8:
        regime = "quiet"
    else:
        regime = "mixed"
    return regime, adx, vol_ratio, trend_direction


def _positions_snapshot(positions):
    rows = []
    for p in positions:
        if float(p.position) == 0:
            continue
        rows.append(
            {
                "symbol": p.contract.symbol.upper(),
                "secType": getattr(p.contract, "secType", "STK"),
                "qty": float(p.position),
                "avgCost": float(p.avgCost),
            }
        )
    return rows


def _fmt_gate_num(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _log_ib_margin_gates(snap: dict) -> None:
    """Log IB account summary tags that mirror live margin, settlement, and PDT gates."""
    parts = [
        f"type={snap.get('account_type') or '—'}",
        f"NL={_fmt_gate_num(snap.get('net_liquidation'))}",
        f"cash={_fmt_gate_num(snap.get('total_cash_value'))}",
        f"settled={_fmt_gate_num(snap.get('settled_cash'))}",
        f"BP={_fmt_gate_num(snap.get('buying_power'))}",
        f"excessLiq={_fmt_gate_num(snap.get('excess_liquidity'))}",
        f"avail={_fmt_gate_num(snap.get('available_funds'))}",
        f"fullAvail={_fmt_gate_num(snap.get('full_available_funds'))}",
        f"dayTradesLeft={_fmt_gate_num(snap.get('day_trades_remaining'))}",
        f"cushion={_fmt_gate_num(snap.get('cushion'))}",
        f"severity={snap.get('highest_severity') or '—'}",
    ]
    logger.info("IB_MARGIN_GATES %s", " ".join(parts))
    dtr = snap.get("day_trades_remaining")
    if dtr is not None:
        try:
            fd = float(dtr)
        except (TypeError, ValueError):
            fd = None
        if fd is not None:
            if fd == 0.0:
                logger.warning(
                    "IB PDT gate: DayTradesRemaining=0 — day-trade capacity exhausted until rollover; "
                    "confirm in Account Management vs live behavior."
                )
            elif fd < 0:
                logger.info(
                    "IB PDT gate: DayTradesRemaining=%s (IB: unlimited day trades for this account tag)",
                    dtr,
                )


def _log_ib_account_pnl(pnl_ib: dict) -> None:
    """Log IB reqPnL fields; same numbers persisted to journal ``daily_snapshots``."""
    logger.info(
        "IB_ACCOUNT_PNL daily=%s unrealized=%s realized_today=%s",
        _fmt_gate_num(pnl_ib.get("daily_pnl")),
        _fmt_gate_num(pnl_ib.get("unrealized_pnl")),
        _fmt_gate_num(pnl_ib.get("realized_pnl")),
    )


async def watch_mode(
    symbols,
    strategy,
    connection,
    check_interval,
    *,
    journal: TradeJournal | None = None,
    memory: TradeMemory | None = None,
    run_id: str | None = None,
    snapshot_interval_sec: int = 60,
    paper_margin_gates_interval_sec: int = 0,
):
    """Real-time watching mode for multiple stocks"""
    logger.info(f"Starting real-time watch for {symbols} with check interval {check_interval} seconds")

    allow_outside_rth = getattr(connection, "_allow_equity_orders_when_market_closed", False)
    if allow_outside_rth:
        logger.warning(
            "Watch loop runs outside US RTH (--allow-closed-market); real-time bars use extended hours where IB sends them"
        )

    # Load or recover active trades on startup
    active_trades = await load_active_trades(connection)

    # Log watch mode start
    market_data_logger.info(json.dumps({
        'timestamp': datetime.now(pytz.timezone('US/Eastern')).isoformat(),
        'type': 'system',
        'message': f'Watch mode started for symbols: {symbols}'
    }))

    # Set up real-time data subscriptions for all stocks
    contracts = [Stock(symbol, "SMART", "USD") for symbol in symbols]

    # Initialize data storage for each stock - normalize to uppercase
    symbols_upper = [symbol.upper() for symbol in symbols]
    data_dict = {symbol: pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume']) for symbol in symbols_upper}

    # Store real-time bar lists for each symbol
    realtime_bar_lists = {}

    # Trade IDs must be globally unique across restarts to avoid journal collisions.
    trade_counter = 0
    trade_id_prefix = (run_id or f"RUNLESS_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}")

    def _next_trade_id() -> str:
        nonlocal trade_counter
        trade_counter += 1
        return f"TRADE_{trade_id_prefix}_{trade_counter:06d}"

    async def setup_market_data(contract):
        try:
            await connection.get_ib().qualifyContractsAsync(contract)

            # Request real-time bars - ib_insync automatically handles reqId internally
            bars = connection.get_ib().reqRealTimeBars(
                contract=contract,
                barSize=5,
                whatToShow='TRADES',
                useRTH=not allow_outside_rth,
            )

            symbol_upper = contract.symbol.upper()
            realtime_bar_lists[symbol_upper] = bars

            logger.debug(f"Requested real-time bars for {contract.symbol}")
            return True
        except Exception as e:
            logger.error(f"Error setting up market data for {contract.symbol}: {e}")
            return False

    # Set up market data for each contract
    for contract in contracts:
        success = await setup_market_data(contract)
        if not success:
            logger.error(f"Failed to set up market data for {contract.symbol}")

    def on_bar_update(bars, has_new_bar):
        """Handle barUpdateEvent from ib_insync"""
        # The bars parameter is a RealTimeBarList that contains contract information
        if not hasattr(bars, 'contract') or not bars.contract:
            logger.warning("Received bar update without contract information")
            return

        symbol = bars.contract.symbol.upper()

        # Get the latest bar
        if len(bars) == 0:
            return

        latest_bar = bars[-1]

        # Ensure symbol exists in our tracking
        if symbol not in data_dict:
            logger.warning(f"Received bar for untracked symbol {symbol}, initializing...")
            data_dict[symbol] = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

        timestamp = datetime.now(pytz.timezone('US/Eastern'))

        # Log raw market data
        market_data = {
            'timestamp': timestamp.isoformat(),
            'symbol': symbol,
            'bar_data': {
                'date': latest_bar.time.isoformat() if hasattr(latest_bar.time, 'isoformat') else str(latest_bar.time),
                'open': latest_bar.open_,
                'high': latest_bar.high,
                'low': latest_bar.low,
                'close': latest_bar.close,
                'volume': latest_bar.volume if latest_bar.volume >= 0 else 0,
                'count': latest_bar.count,
                'wap': latest_bar.wap,
                'has_new_bar': has_new_bar
            }
        }
        market_data_logger.info(json.dumps(market_data))

        # Update data dictionary
        new_data = pd.DataFrame({
            'open': [latest_bar.open_],
            'high': [latest_bar.high],
            'low': [latest_bar.low],
            'close': [latest_bar.close],
            'volume': [latest_bar.volume if latest_bar.volume >= 0 else 0]
        }, index=[latest_bar.time])

        data_dict[symbol] = pd.concat([data_dict[symbol], new_data]).drop_duplicates().sort_index()
        logger.debug(f"Received new bar for {symbol}: {latest_bar.time}, Close: {latest_bar.close}, Has new bar: {has_new_bar}")

    # Subscribe to bar update events from ib_insync
    connection.get_ib().barUpdateEvent += on_bar_update

    snapshot_task = None
    if journal and snapshot_interval_sec > 0:
        async def _snapshot_loop():
            while True:
                await asyncio.sleep(snapshot_interval_sec)
                try:
                    now = datetime.now(pytz.timezone('US/Eastern'))
                    positions = await connection.get_ib().reqPositionsAsync()
                    pos_rows = _positions_snapshot(positions)
                    equity = await connection.get_account_equity()
                    pnl_ib = await connection.get_account_pnl_ib_snapshot()
                    journal.insert_daily_snapshot(
                        timestamp=now,
                        net_liquidation=_safe_float(equity),
                        positions=pos_rows,
                        unrealized_pnl=pnl_ib.get("unrealized_pnl"),
                        realized_pnl_today=pnl_ib.get("realized_pnl"),
                        ib_daily_pnl=pnl_ib.get("daily_pnl"),
                    )
                    _log_ib_account_pnl(pnl_ib)
                except asyncio.CancelledError:
                    raise
                except Exception as snapshot_err:
                    logger.warning(f"Trade journal snapshot failed: {snapshot_err}")

        snapshot_task = asyncio.create_task(_snapshot_loop())
        try:
            _log_ib_account_pnl(await connection.get_account_pnl_ib_snapshot())
        except Exception as pnl_log_err:
            logger.debug("Initial IB account PnL log skipped: %s", pnl_log_err)

    margin_gates_task = None
    if paper_margin_gates_interval_sec and paper_margin_gates_interval_sec > 0:

        async def _margin_gates_loop():
            while True:
                await asyncio.sleep(paper_margin_gates_interval_sec)
                try:
                    snap = await connection.get_account_margin_gate_snapshot()
                    _log_ib_margin_gates(snap)
                except asyncio.CancelledError:
                    raise
                except Exception as loop_err:
                    logger.warning("Paper margin gates periodic log failed: %s", loop_err)

        margin_gates_task = asyncio.create_task(_margin_gates_loop())

    # Start watching loop
    try:
        while True:
            current_time = datetime.now(pytz.timezone('US/Eastern'))
            # Match IBKRConnection.is_market_open (US equity RTH) unless paper allow-closed-market.
            if not allow_outside_rth:
                if current_time.weekday() >= 5:
                    logger.info("Outside market hours (weekend), skipping this cycle...")
                    await asyncio.sleep(check_interval)
                    continue
                tod = current_time.time()
                if tod < time(9, 30) or tod > time(16, 0):
                    logger.info("Outside market hours, skipping this cycle...")
                    await asyncio.sleep(check_interval)
                    continue

            # STEP 1: Check existing positions for exit signals
            try:
                existing_positions = await connection.get_ib().reqPositionsAsync()

                for position in existing_positions:
                    if position.position != 0:  # Only check non-zero positions
                        pos_symbol = position.contract.symbol.upper()
                        if position.contract.secType not in ('STK', ''):
                            logger.debug(f"Skipping non-stock position: {pos_symbol} (type={position.contract.secType})")
                            continue
                        position_size = float(position.position)
                        avg_cost = float(position.avgCost)

                        logger.debug(f"Checking position: {pos_symbol}, Size: {position_size}, Avg Cost: ${avg_cost:.2f}")

                        # Ensure we have market data for this position
                        if pos_symbol not in data_dict:
                            # Add this symbol to our watch list dynamically
                            logger.info(f"Adding {pos_symbol} to watch list (currently held position)")
                            try:
                                pos_contract = Stock(pos_symbol, "SMART", "USD")
                                success = await setup_market_data(pos_contract)
                                if success:
                                    data_dict[pos_symbol] = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
                                    logger.info(f"Successfully added {pos_symbol} to monitoring")
                                else:
                                    logger.warning(f"Failed to add {pos_symbol} to monitoring")
                                    continue
                            except Exception as e:
                                logger.error(f"Error adding {pos_symbol} to watch list: {e}")
                                continue

                        # Check if we have enough data for this position
                        if len(data_dict[pos_symbol]) >= 2:
                            df = strategy.calculate_indicators(data_dict[pos_symbol].copy())

                            # Generate exit signal for this position
                            if position_size > 0:  # Long position
                                exit_signal = await strategy.generate_exit_signals(df, 'long')
                            else:  # Short position
                                exit_signal = await strategy.generate_exit_signals(df, 'short')

                            # Log position monitoring
                            position_data = {
                                'timestamp': current_time.isoformat(),
                                'symbol': pos_symbol,
                                'action': 'position_monitoring',
                                'position_details': {
                                    'position_size': position_size,
                                    'avg_cost': avg_cost,
                                    'current_price': float(df['close'].iloc[-1]),
                                    'unrealized_pnl': position_size * (float(df['close'].iloc[-1]) - avg_cost),
                                    'exit_signal': int(exit_signal),
                                    'position_type': 'long' if position_size > 0 else 'short',
                                    'max_duration_hours': strategy.max_duration_hours
                                }
                            }
                            market_data_logger.info(json.dumps(position_data))

                            # Execute exit trade if signal indicates
                            if exit_signal != 0:
                                logger.info(f"Exit signal {exit_signal} generated for position {pos_symbol}")
                                exit_action = 'SELL' if position_size > 0 else 'BUY'
                                exit_quantity = abs(int(position_size))
                                trade_id = active_trades.get(pos_symbol, {}).get('trade_id', 'UNKNOWN_TRADE')

                                exit_data = {
                                    'timestamp': current_time.isoformat(),
                                    'trade_id': trade_id,
                                    'symbol': pos_symbol,
                                    'action': 'position_exit_attempt',
                                    'exit_details': {
                                        'exit_signal': exit_signal,
                                        'exit_action': exit_action,
                                        'exit_quantity': exit_quantity,
                                        'current_price': float(df['close'].iloc[-1]),
                                        'position_size': position_size,
                                        'avg_cost': avg_cost,
                                        'exit_reason': 'strategy_signal'
                                    }
                                }
                                market_data_logger.info(json.dumps(exit_data))

                                exit_result = await strategy.execute_trade(
                                    symbol=pos_symbol,
                                    signal=-1 if position_size > 0 else 1,  # Opposite signal to close
                                    entry_price=df['close'].iloc[-1],
                                    shares=exit_quantity,
                                    stop_loss=None,
                                    target=None
                                )

                                exit_result_data = {
                                    'timestamp': current_time.isoformat(),
                                    'trade_id': trade_id,
                                    'symbol': pos_symbol,
                                    'action': 'position_exit_result',
                                    'exit_result': exit_result
                                }
                                market_data_logger.info(json.dumps(exit_result_data))

                                if exit_result['status'] == 'success':
                                    pnl = position_size * (float(df['close'].iloc[-1]) - avg_cost)
                                    logger.info(
                                        f"TRADE {trade_id}: Closed position {pos_symbol}: "
                                        f"{exit_action} {exit_quantity} shares at ${df['close'].iloc[-1]:.2f}, PnL: ${pnl:.2f}"
                                    )
                                    at = active_trades.get(pos_symbol, {})
                                    if journal and trade_id != "UNKNOWN_TRADE":
                                        try:
                                            journal.close_trade(
                                                trade_id=trade_id,
                                                exit_fill_price=float(df['close'].iloc[-1]),
                                                exit_fill_time=current_time,
                                                exit_reason="strategy_signal",
                                                realized_pnl=float(pnl),
                                                status="closed",
                                            )
                                            journal.record_fill(
                                                trade_id=trade_id,
                                                order_id=None,
                                                exec_id=None,
                                                symbol=pos_symbol,
                                                side=exit_action,
                                                qty=exit_quantity,
                                                price=float(df['close'].iloc[-1]),
                                                commission=None,
                                                time=current_time,
                                                is_entry=False,
                                            )
                                        except Exception as journal_err:
                                            logger.warning(f"Trade journal close logging failed for {trade_id}: {journal_err}")

                                    if memory and memory.enabled and trade_id != "UNKNOWN_TRADE":
                                        try:
                                            memory.capture_closed_trade(
                                                trade_id=trade_id,
                                                symbol=pos_symbol,
                                                strategy=type(strategy).__name__,
                                                side="LONG" if at.get("is_long", position_size > 0) else "SHORT",
                                                signal_value=int(at.get("signal_value", -1 if position_size > 0 else 1)),
                                                entry_price=_safe_float(at.get("entry_price")),
                                                exit_price=float(df['close'].iloc[-1]),
                                                realized_pnl=float(pnl),
                                                shares=_safe_float(at.get("shares", exit_quantity)),
                                                entry_time=at.get("entry_time"),
                                                exit_time=current_time,
                                                entry_regime=at.get("entry_regime"),
                                                exit_reason="strategy_signal",
                                                indicators=at.get("indicator_snapshot", {}),
                                            )
                                        except Exception as memory_err:
                                            logger.warning(f"Trade memory capture failed for {trade_id}: {memory_err}")

                                    if pos_symbol in active_trades:
                                        del active_trades[pos_symbol]
                                        save_active_trades_cache(active_trades)
                                else:
                                    logger.error(f"TRADE {trade_id}: Failed to close position {pos_symbol}: {exit_result['message']}")

            except Exception as e:
                logger.error(f"Error checking existing positions: {e}")

            # STEP 2: Log data status for each symbol
            for symbol in symbols:
                symbol_upper = symbol.upper()
                data_len = len(data_dict.get(symbol_upper, pd.DataFrame()))
                logger.debug(f"Data status for {symbol}: {data_len} bars")
                if data_len < 5:
                    logger.debug(f"Waiting for more data for {symbol} (need at least 5 bars)")

            # STEP 3: Generate entry signals and execute trades
            signals = []
            for symbol in symbols:
                symbol_upper = symbol.upper()
                if symbol_upper in data_dict and len(data_dict[symbol_upper]) >= 2:
                    try:
                        df = strategy.calculate_indicators(data_dict[symbol_upper].copy())
                    except Exception as calc_err:
                        logger.warning(f"Indicator calculation failed for {symbol}: {calc_err}")
                        continue

                    signal = await strategy.generate_realtime_signals(df)
                    signal_data = {
                        'timestamp': current_time.isoformat(),
                        'symbol': symbol,
                        'signal': int(signal),
                        'market_context': {
                            'current_price': float(df['close'].iloc[-1]),
                            'current_volume': float(df['volume'].iloc[-1]),
                            'bars_analyzed': int(len(df)),
                            'indicators': {
                                col: float(df[col].iloc[-1]) if pd.api.types.is_numeric_dtype(df[col]) else str(df[col].iloc[-1])
                                for col in df.columns
                                if col not in ['open', 'high', 'low', 'close', 'volume']
                            }
                        }
                    }
                    market_data_logger.info(json.dumps(signal_data))
                    indicators = signal_data.get("market_context", {}).get("indicators", {})
                    regime, adx, vol_ratio, trend_direction = _infer_regime(indicators)

                    if journal and run_id:
                        try:
                            journal.log_signal(
                                run_id=run_id,
                                symbol=symbol_upper,
                                timestamp=current_time,
                                signal_value=int(signal),
                                price=_safe_float(df['close'].iloc[-1]),
                                indicators=indicators,
                            )
                            regime, adx, vol_ratio, trend_direction = _infer_regime(indicators)
                            journal.log_market_regime(
                                symbol=symbol_upper,
                                timestamp=current_time,
                                adx=adx,
                                vol_ratio=vol_ratio,
                                trend_direction=trend_direction,
                                regime_label=regime,
                                regime_confidence=(adx / 50.0 if adx is not None else None),
                            )
                        except Exception as journal_err:
                            logger.warning(f"Trade journal signal logging failed for {symbol_upper}: {journal_err}")

                    if signal != 0:
                        signals.append(f"{symbol}: {signal}")
                        logger.debug(f"Generated signal {signal} for {symbol}")
                        trade_id = _next_trade_id()

                        entry_price = float(df['close'].iloc[-1])
                        is_long = signal == 1
                        stop_loss = entry_price * (1 - strategy.stop_loss_pct/100) if is_long else entry_price * (1 + strategy.stop_loss_pct/100)
                        target = entry_price * (1 + strategy.target_pct/100) if is_long else entry_price * (1 - strategy.target_pct/100)
                        risk_per_share = abs(entry_price - stop_loss)
                        shares = int(strategy.risk_per_trade / risk_per_share) if risk_per_share > 0 else 100
                        if shares <= 0:
                            shares = 100

                        logger.info(
                            f"Trade parameters for {symbol}: Entry=${entry_price:.2f}, Stop=${stop_loss:.2f} "
                            f"({strategy.stop_loss_pct}%), Target=${target:.2f} ({strategy.target_pct}%), Shares={shares}"
                        )
                        memory_verdict = None
                        if memory and memory.enabled:
                            try:
                                recall = memory.pre_trade_recall(
                                    symbol=symbol_upper,
                                    strategy=type(strategy).__name__,
                                    signal_value=int(signal),
                                    entry_price=entry_price,
                                    indicators=indicators,
                                )
                                memory_verdict = recall.as_dict()
                                if recall.win_rate_similar is not None:
                                    logger.info(
                                        f"Memory recall for {symbol_upper}: "
                                        f"win_rate={recall.win_rate_similar:.1%}, avg_pnl={recall.avg_pnl_similar}"
                                    )
                                if recall.regime_warning:
                                    logger.warning(f"Memory warning for {symbol_upper}: {recall.regime_warning}")
                            except Exception as memory_err:
                                logger.warning(f"Trade memory recall failed for {symbol_upper}: {memory_err}")

                        trade_data = {
                            'timestamp': current_time.isoformat(),
                            'trade_id': trade_id,
                            'symbol': symbol,
                            'action': 'trade_execution_attempt',
                            'trade_details': {
                                'signal': signal,
                                'entry_price': entry_price,
                                'shares': shares,
                                'stop_loss': stop_loss,
                                'target': target,
                                'risk_per_share': risk_per_share,
                                'is_long': is_long,
                                'trade_type': 'LONG' if is_long else 'SHORT'
                            }
                        }
                        market_data_logger.info(json.dumps(trade_data))

                        if journal and run_id:
                            try:
                                journal.log_trade_attempt(
                                    trade_id=trade_id,
                                    run_id=run_id,
                                    symbol=symbol,
                                    side="LONG" if is_long else "SHORT",
                                    signal_value=int(signal),
                                    intended_entry_price=entry_price,
                                    intended_shares=int(shares),
                                    stop_loss=float(stop_loss),
                                    target=float(target),
                                    indicator_snapshot=indicators,
                                    memory_consulted=memory_verdict is not None,
                                    memory_verdict=memory_verdict,
                                )
                            except Exception as journal_err:
                                logger.warning(f"Trade journal attempt logging failed for {trade_id}: {journal_err}")

                        trade_result = await strategy.execute_trade(
                            symbol=symbol,
                            signal=signal,
                            entry_price=entry_price,
                            shares=shares,
                            stop_loss=stop_loss,
                            target=target
                        )

                        trade_result_data = {
                            'timestamp': current_time.isoformat(),
                            'trade_id': trade_id,
                            'symbol': symbol,
                            'action': 'trade_execution_result',
                            'trade_result': trade_result
                        }
                        market_data_logger.info(json.dumps(trade_result_data))

                        if journal:
                            try:
                                if trade_result['status'] == 'success':
                                    journal.update_trade_execution_result(
                                        trade_id=trade_id,
                                        status="submitted",
                                        order_id=trade_result.get('order_id'),
                                        stop_order_id=trade_result.get('stop_order_id'),
                                        target_order_id=trade_result.get('target_order_id'),
                                    )
                                else:
                                    journal.update_trade_execution_result(
                                        trade_id=trade_id,
                                        status="error",
                                        error_message=trade_result.get('message'),
                                    )
                            except Exception as journal_err:
                                logger.warning(f"Trade journal result logging failed for {trade_id}: {journal_err}")

                        if trade_result['status'] == 'success':
                            await asyncio.sleep(5)
                            exec_filter = ExecutionFilter(symbol=symbol, time="1 D")
                            executions = await connection.get_ib().reqExecutionsAsync(exec_filter)
                            matching_execs = [exec for exec in executions if exec.orderId == trade_result['order_id']]
                            if matching_execs:
                                fill_time = matching_execs[0].time.astimezone(pytz.timezone('US/Eastern'))
                                fill_price = float(matching_execs[0].price)
                                fill_qty = float(matching_execs[0].shares)
                            else:
                                fill_time = current_time
                                fill_price = entry_price
                                fill_qty = float(shares)

                            active_trades[symbol.upper()] = {
                                'entry_time': fill_time,
                                'trade_id': trade_id,
                                'entry_price': entry_price,
                                'shares': shares,
                                'is_long': is_long,
                                'signal_value': int(signal),
                                'entry_regime': regime,
                                'indicator_snapshot': indicators,
                                'parent_order_id': trade_result.get('order_id'),
                                'stop_order_id': trade_result.get('stop_order_id'),
                                'target_order_id': trade_result.get('target_order_id')
                            }
                            save_active_trades_cache(active_trades)

                            if journal:
                                try:
                                    slippage_bps = ((fill_price - entry_price) / entry_price * 10000.0) if entry_price else None
                                    journal.mark_trade_entry_fill(
                                        trade_id=trade_id,
                                        entry_fill_price=fill_price,
                                        entry_fill_time=fill_time,
                                        slippage_bps=slippage_bps,
                                        status="open",
                                    )
                                    journal.record_fill(
                                        trade_id=trade_id,
                                        order_id=trade_result.get('order_id'),
                                        exec_id=None,
                                        symbol=symbol,
                                        side='BUY' if signal == 1 else 'SELL',
                                        qty=fill_qty,
                                        price=fill_price,
                                        commission=None,
                                        time=fill_time,
                                        is_entry=True,
                                    )
                                except Exception as journal_err:
                                    logger.warning(f"Trade journal fill logging failed for {trade_id}: {journal_err}")

                            logger.info(
                                f"TRADE {trade_id}: Executed {'Buy' if signal == 1 else 'Sell'} trade for {symbol}: "
                                f"Entry ${entry_price:.2f}, Shares {shares}"
                            )
                            logger.info(
                                f"TRADE {trade_id}: Stop Loss Order ID: {trade_result.get('stop_order_id')}, "
                                f"Target Order ID: {trade_result.get('target_order_id')}"
                            )
                        else:
                            logger.error(f"TRADE {trade_id}: Trade failed for {symbol}: {trade_result['message']}")

            if signals:
                signals_summary = ", ".join(signals)
                logger.info(f"Completed watch cycle. Signals: {signals_summary}")
            else:
                logger.info("Completed watch cycle. No signals generated.")

            await asyncio.sleep(check_interval)
    finally:
        if snapshot_task:
            snapshot_task.cancel()
            try:
                await snapshot_task
            except asyncio.CancelledError:
                pass
        if margin_gates_task:
            margin_gates_task.cancel()
            try:
                await margin_gates_task
            except asyncio.CancelledError:
                pass
        try:
            connection.get_ib().barUpdateEvent -= on_bar_update
        except Exception:
            pass
        try:
            connection.cancel_account_pnl_subscription()
        except Exception:
            pass

def save_active_trades_cache(active_trades):
    cache_data = {
        k: {
            **v,
            'entry_time': v['entry_time'].isoformat()
        } for k, v in active_trades.items()
    }
    with open('active_trades.json', 'w') as f:
        json.dump(cache_data, f)
    logger.debug("Saved active trades cache")

async def load_active_trades(connection):
    try:
        active = {}
        # Query open positions
        positions = await connection.get_ib().reqPositionsAsync()
        for pos in positions:
            if pos.position != 0:
                symbol = pos.contract.symbol.upper()
                exec_filter = ExecutionFilter(symbol=symbol, time="1 D")  # Last day
                executions = await connection.get_ib().reqExecutionsAsync(exec_filter)
                if executions:
                    # Use latest fill time (assume single or last fill for simplicity)
                    latest_exec = max(executions, key=lambda e: e.time)
                    entry_time = latest_exec.time.astimezone(pytz.timezone('US/Eastern'))
                    active[symbol] = {
                        'entry_time': entry_time,
                        'shares': abs(pos.position),
                        'is_long': pos.position > 0,
                        # Add other needed fields
                    }
        # Save cache
        save_active_trades_cache(active)
        logger.info(f"Loaded {len(active)} active trades from IBKR")
        return active
    except Exception as e:
        logger.error(f"Error loading active trades: {e}")
        return {}

def parse_args():
    parser = argparse.ArgumentParser(description="Trading System")
    parser.add_argument("symbols", nargs="+", help="Trading symbols")
    parser.add_argument("--duration", default="1 M", help="Duration for historical data")
    parser.add_argument("--bar-size", default="15 mins", help="Bar size for historical data")
    parser.add_argument("--strategy", default="groq", help="Trading strategy to use")
    parser.add_argument("--force-refresh", action="store_true", help="Force refresh historical data")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--client-id", type=int, default=1, help="IBKR client ID")
    parser.add_argument("--paper", action="store_true", help="Use paper trading")
    parser.add_argument(
        "--allow-closed-market",
        action="store_true",
        help="Paper only: run watch loop and equity orders outside US RTH (extended-hours diagnostics)",
    )
    parser.add_argument(
        "--paper-margin-gates-interval",
        type=int,
        default=600,
        metavar="SEC",
        help="Paper: re-log IB margin/settled cash/PDT (DayTradesRemaining) tags every SEC in watch mode; 0=startup log only. Default 600.",
    )
    parser.add_argument(
        "--lax-paper",
        action="store_true",
        help="Paper only: disable strict pre-trade gates (PDT, settled cash, buying power). "
             "Use ONLY for diagnostics. Live always uses strict gates. Default for paper is now STRICT.",
    )
    parser.add_argument("--live", action="store_true", help="Use live trading")
    parser.add_argument("--watch", action="store_true", help="Watch mode only")
    parser.add_argument("--check-interval", type=int, default=60, help="Check interval in seconds")
    parser.add_argument("--backtest-only", action="store_true", help="Run backtest only")
    parser.add_argument("--risk-per-trade", type=float, default=1000.0, help="Risk per trade in dollars")
    parser.add_argument("--max-duration", type=float, default=12.0, help="Maximum trade duration in hours")
    parser.add_argument("--stop-loss", type=float, default=1.0, help="Stop loss percentage")
    parser.add_argument("--target", type=float, default=4.0, help="Target profit percentage")
    parser.add_argument("--initial-capital", type=float, default=50000.0, help="Initial capital for backtest")
    parser.add_argument("--rebalance-period", type=int, default=60, help="Portfolio rebalance period in minutes")
    parser.add_argument("--vwap-threshold", type=float, default=0.5, help="VWAP threshold for entry")
    parser.add_argument("--phase2-rules", action="store_true", help="Use Phase 2 trading rules")
    parser.add_argument("--zone-lookback", type=int, default=4, help="Bars to look back for supply/demand zones")
    parser.add_argument("--adx-min", type=float, default=15.0, help="Minimum ADX for zone validity")
    parser.add_argument("--zone-freshness-hours", type=float, default=12.0, help="Max hours a zone stays valid")
    parser.add_argument("--volume-breakout-mult", type=float, default=1.1, help="Volume multiplier for breakout bar")
    parser.add_argument("--volume-confirm-mult", type=float, default=1.05, help="Volume multiplier for confirmation bar")
    parser.add_argument("--position-size-cap", type=float, default=0.95, help="Max fraction of capital per position")
    parser.add_argument("--no-journal", action="store_true", help="Disable SQLite trade journaling in watch mode")
    parser.add_argument("--journal-db", default=str(_PYTHONCLIENT_ROOT / "trade_journal.db"), help="Path to SQLite journal db")
    parser.add_argument("--journal-snapshot-interval", type=int, default=60, help="Seconds between account/position snapshots in journal")
    parser.add_argument("--no-memory", action="store_true", help="Disable Chroma trade memory recall/capture in watch mode")
    parser.add_argument("--memory-db", default=str(_PYTHONCLIENT_ROOT / "chroma_db"), help="Path to ChromaDB for trade memory")
    return parser.parse_args()

async def main():
    args = parse_args()
    
    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Initialize strategy with mode-specific parameters
    strategy_params = {
        "risk_per_trade": args.risk_per_trade,
        "max_duration_hours": args.max_duration,
        "paper_trading": args.paper,
        "stop_loss_pct": args.stop_loss,
        "target_pct": args.target,
        "bar_size": args.bar_size,
        "duration": args.duration,
    }
    groq_extra = {
        "zone_lookback": args.zone_lookback,
        "adx_min": args.adx_min,
        "zone_freshness_hours": args.zone_freshness_hours,
        "volume_breakout_mult": args.volume_breakout_mult,
        "volume_confirm_mult": args.volume_confirm_mult,
        "position_size_cap": args.position_size_cap,
    }
    
    if args.strategy == "groq":
        strategy = GroqRecommendationsStrategy(**strategy_params, **groq_extra)
        logger.info(f"Strategy initialized with: stop_loss_pct={strategy.stop_loss_pct}%, target_pct={strategy.target_pct}%, risk_per_trade=${strategy.risk_per_trade}")
    elif args.strategy == "mean_reversion":
        strategy = MeanReversionStrategy(**strategy_params)
        logger.info(f"Mean Reversion Strategy initialized with: stop_loss_pct={strategy.stop_loss_pct}%, target_pct={strategy.target_pct}%, risk_per_trade=${strategy.risk_per_trade}")
    else:
        raise ValueError(f"Unknown strategy: {args.strategy}")
    
    # Determine trading mode
    if args.backtest_only:
        mode = "Backtest"
    elif args.paper and not args.live:
        mode = "Paper Trading"
    elif args.live:
        mode = "Live Trading"
    else:
        mode = "Watch"
    
    logger.info(f"Starting in {mode} mode")
    
    # Initialize connection for both backtesting and trading
    client_id = args.client_id if args.client_id is not None else random.randint(1, 1000)
    logger.info(f"Initializing IBKR connection with client ID {client_id}...")
    
    # Use paper trading port (4002) for both backtest-only and paper trading modes
    # Only use live trading port (7497) when explicitly requested with --live
    port = 4002 if args.backtest_only or not args.live else 7497
    allow_closed_market = bool(args.paper and args.allow_closed_market and not args.live)
    lax_paper = bool(args.paper and args.lax_paper and not args.live)
    if args.allow_closed_market and not args.paper:
        logger.warning("--allow-closed-market ignored (requires --paper)")
    if args.allow_closed_market and args.live:
        logger.warning("--allow-closed-market ignored in --live mode")
    if args.lax_paper and not args.paper:
        logger.warning("--lax-paper ignored (only affects paper)")
    connection = IBKRConnection(
        host="127.0.0.1",
        port=port,
        client_id=client_id,
        allow_equity_orders_when_market_closed=allow_closed_market,
        lax_paper=lax_paper,
    )
    if allow_closed_market:
        logger.warning(
            "Paper mode: equity orders allowed outside local RTH (outsideRth); not a substitute for real session behavior"
        )
    journal = None
    if not args.no_journal:
        try:
            journal = TradeJournal(Path(args.journal_db))
            logger.info(f"Trade journal enabled at {args.journal_db}")
        except Exception as journal_init_err:
            logger.warning(f"Trade journal init failed, continuing without journal: {journal_init_err}")
            journal = None
    memory = None
    if not args.no_memory:
        try:
            memory = TradeMemory(Path(args.memory_db))
            if memory.enabled:
                logger.info(f"Trade memory enabled at {args.memory_db}")
            else:
                logger.warning(f"Trade memory disabled (init failed): {memory.init_error}")
                memory = None
        except Exception as memory_init_err:
            logger.warning(f"Trade memory init failed, continuing without memory: {memory_init_err}")
            memory = None
    
    try:
        logger.info("Attempting to connect to IBKR Gateway...")
        if not await connection.connect():
            logger.error("Failed to connect to IBKR Gateway after multiple attempts")
            return
        logger.info("Successfully connected to IBKR Gateway")

        if args.paper:
            try:
                _log_ib_margin_gates(await connection.get_account_margin_gate_snapshot())
            except Exception as gate_err:
                logger.warning("IB margin gate snapshot failed: %s", gate_err)
            
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
            # Real-time watching mode for multiple stocks
            logger.info(f"Starting real-time watch for {args.symbols} with check interval {args.check_interval} seconds")
            if args.strategy not in ['groq', 'mean_reversion']:
                logger.error(f"Real-time watching only supported for groq and mean_reversion strategies, not: {args.strategy}")
                return

            # Set IB client for watch mode
            strategy.set_ib_client(connection)

            run_id = None
            if journal:
                try:
                    run_id = journal.start_run(
                        strategy=args.strategy,
                        symbols=[s.upper() for s in args.symbols],
                        params={**strategy_params, **groq_extra, "check_interval": args.check_interval},
                    )
                except Exception as run_err:
                    logger.warning(f"Could not start trade journal run: {run_err}")
                    run_id = None

            try:
                await watch_mode(
                    args.symbols,
                    strategy,
                    connection,
                    args.check_interval,
                    journal=journal,
                    memory=memory,
                    run_id=run_id,
                    snapshot_interval_sec=args.journal_snapshot_interval,
                    paper_margin_gates_interval_sec=(
                        args.paper_margin_gates_interval if args.paper else 0
                    ),
                )
            finally:
                if journal and run_id:
                    try:
                        journal.end_run(run_id)
                    except Exception as end_err:
                        logger.warning(f"Could not finalize trade journal run {run_id}: {end_err}")
        
        else:
            # Backtesting or paper trading mode
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
            
            # Fetch historical data for all symbols
            logger.info(f"Fetching historical data for {args.symbols}...")
            logger.info(f"Date Range (ET): {start_date.strftime('%Y-%m-%d %H:%M:%S %Z')} to {end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
            logger.info(f"Bar Size: {args.bar_size}")
            
            data_dict = await data_manager.get_historical_data_multiple(
                args.symbols,
                start_date_str,
                end_date_str,
                bar_size=args.bar_size,
                force_refresh=args.force_refresh
            )
            
            # Process each symbol
            all_results = {}
            for symbol, df in data_dict.items():
                if df is None or (hasattr(df, 'empty') and df.empty):
                    logger.error(f"No data returned for {symbol} — skipping (check the ticker is valid)")
                    continue
                logger.info(f"Calculating technical indicators for {symbol}...")
                df = strategy.calculate_indicators(df)
                
                if args.backtest_only:
                    logger.info(f"Running backtest in simulation mode for {symbol} (no trades will be executed)...")
                else:
                    logger.info(f"Running in {mode} mode for {symbol}...")
                    strategy.set_ib_client(connection)  # Set IB client for paper trading
                    
                # Validate risk per trade
                if args.risk_per_trade > 2000.0:
                    logger.warning(f"Risk per trade (${args.risk_per_trade}) exceeds maximum allowed ($2000.0). Setting to $2000.0")
                    args.risk_per_trade = 2000.0
                
                backtest_results = await strategy.backtest(df, initial_capital=args.initial_capital, symbol=symbol)
                all_results[symbol] = backtest_results
                
                logger.info(f"\nResults for {symbol}:")
                logger.info(f"Initial Capital: ${backtest_results['initial_capital']:,.2f}")
                logger.info(f"Final Capital: ${backtest_results['final_capital']:,.2f}")
                logger.info(f"Return: {backtest_results['return_pct']:.2f}%")
                logger.info(f"Total Trades: {backtest_results['total_trades']}")
                
                # Print basic statistics
                logger.info(f"\nBasic Statistics for {symbol}:")
                logger.info(f"Date Range: {df.index[0]} to {df.index[-1]}")
                logger.info(f"Number of bars: {len(df)}")
                logger.info(f"Current price: ${df['close'].iloc[-1]:.2f}")
                
                if not args.backtest_only:
                    logger.info(f"\nTrading Mode: {mode}")
                    logger.info(f"Market Hours: 9:30 AM - 4:00 PM ET")
                    logger.info(f"Circuit Breakers Active: Yes")
                    logger.info(f"Daily Trade Limit: {getattr(strategy, 'max_daily_trades', 'N/A')}")
                    logger.info(f"Daily Loss Limit: ${getattr(strategy, 'max_daily_loss', 0):,.2f}")
                    logger.info(f"Position Size Limit: ${getattr(strategy, 'max_position_size', 0):,.2f}")
                    logger.info(f"Min Volume: {getattr(strategy, 'min_volume_threshold', 0):,.0f}")
                    logger.info(f"Max Volatility: {getattr(strategy, 'max_volatility_pct', 0):.1f}%")
            
            # Print summary across all symbols
            logger.info("\nSummary across all symbols:")
            if not all_results:
                logger.error("No valid results — every symbol was skipped or failed. Check your tickers.")
                return
            total_return = sum(r['return_pct'] for r in all_results.values()) / len(all_results)
            total_trades = sum(r['total_trades'] for r in all_results.values())
            logger.info(f"Average Return: {total_return:.2f}%")
            logger.info(f"Total Trades: {total_trades}")
            
            # Check if we should proceed with trading based on backtest results
            if (args.paper or args.live) and not args.backtest_only:
                if total_return < 10.0:
                    logger.warning(
                        f"\nBacktest validation failed: Average return {total_return:.2f}% is below required 10% threshold."
                        f"\n{'Paper' if args.paper else 'Live'} trading will not be started."
                        f"\nPlease adjust strategy parameters or try different symbols."
                    )
                    return
                else:
                    logger.info(
                        f"\nBacktest validation passed: Average return {total_return:.2f}% exceeds required 10% threshold."
                        f"\nStarting {'paper' if args.paper else 'live'} trading..."
                    )
                    # Start trading in watch mode
                    run_id = None
                    if journal:
                        try:
                            run_id = journal.start_run(
                                strategy=args.strategy,
                                symbols=[s.upper() for s in args.symbols],
                                params={**strategy_params, **groq_extra, "check_interval": args.check_interval},
                            )
                        except Exception as run_err:
                            logger.warning(f"Could not start trade journal run: {run_err}")
                            run_id = None

                    try:
                        await watch_mode(
                            args.symbols,
                            strategy,
                            connection,
                            args.check_interval,
                            journal=journal,
                            memory=memory,
                            run_id=run_id,
                            snapshot_interval_sec=args.journal_snapshot_interval,
                        )
                    finally:
                        if journal and run_id:
                            try:
                                journal.end_run(run_id)
                            except Exception as end_err:
                                logger.warning(f"Could not finalize trade journal run {run_id}: {end_err}")
            
    except Exception as e:
        logger.error(f"Error in main: {str(e)}", exc_info=True)
    finally:
        if connection:
            await connection.disconnect()
            logger.info("Disconnected from IBKR Gateway")

if __name__ == "__main__":
    util.startLoop()
    asyncio.run(main())