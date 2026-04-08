"""
Historical data management for stock analysis.
Version 1.1 - Added crypto support
"""
import os
import logging
import pandas as pd
from datetime import datetime, timedelta
from ib_insync import Stock, RealTimeBar, Crypto
from typing import Optional, Dict
import asyncio
from pytz import timezone
from .connection import IBKRConnection
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from utils.crypto_utils import CryptoUtils

logger = logging.getLogger(__name__)

class HistoricalDataManager:
    def __init__(self, connection: IBKRConnection, data_dir: str = 'historical_data'):
        self.connection = connection
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        logger.info(f"Historical data directory: {os.path.abspath(data_dir)}")
        
    def _get_cached_data_path(self, symbol: str, start_date: str, end_date: str, bar_size: str) -> str:
        # Extract just the date part from the timestamps
        start_date_only = start_date.split()[0]  # Get YYYY-MM-DD part
        end_date_only = end_date.split()[0]      # Get YYYY-MM-DD part
        filepath = os.path.join(self.data_dir, f"{symbol}-{start_date_only}-{end_date_only}-{bar_size.replace(' ', '')}.csv")
        logger.debug(f"Cache file path: {filepath}")
        return filepath
        
    def _get_latest_cached_data(self, symbol: str, start_date: str, end_date: str, bar_size: str) -> Optional[pd.DataFrame]:
        filepath = self._get_cached_data_path(symbol, start_date, end_date, bar_size)
        if os.path.exists(filepath):
            try:
                logger.info(f"Reading cached data from {filepath}")
                # Read CSV with parse_dates=False to handle timezone conversion manually
                df = pd.read_csv(filepath, index_col='date', parse_dates=False)
                
                # Convert index to datetime with UTC timezone first
                df.index = pd.to_datetime(df.index, utc=True)
                
                # Then convert to Eastern time
                eastern = timezone('America/New_York')
                df.index = df.index.tz_convert(eastern)
                
                logger.info(f"Successfully loaded {len(df)} rows from cache with ET timezone")
                return df
            except Exception as e:
                logger.error(f"Error reading cached data: {str(e)}")
                # If there's an error, try to delete the corrupted cache file
                try:
                    os.remove(filepath)
                    logger.info(f"Removed corrupted cache file: {filepath}")
                except:
                    pass
        else:
            logger.info(f"No cached data found at {filepath}")
        return None
        
    def _save_data(self, df: pd.DataFrame, symbol: str, start_date: str, end_date: str, bar_size: str) -> str:
        filepath = self._get_cached_data_path(symbol, start_date, end_date, bar_size)
        logger.info(f"Saving {len(df)} rows to {filepath}")
        df.to_csv(filepath)
        logger.info(f"Successfully saved data to {filepath}")
        return filepath
        
    def _handle_timezone(self, df: pd.DataFrame) -> pd.DataFrame:
        try:
            logger.debug("Starting timezone handling")
            eastern = timezone('America/New_York')
            
            if 'date' in df.columns:
                logger.debug("Converting date column to datetime")
                df['date'] = pd.to_datetime(df['date'])
                if df['date'].dt.tz is None:
                    logger.debug("Localizing naive datetime to ET")
                    df['date'] = df['date'].dt.tz_localize(eastern)
                else:
                    logger.debug("Converting timezone-aware datetime to ET")
                    df['date'] = df['date'].dt.tz_convert(eastern)
                logger.debug("Setting date as index")
                df = df.set_index('date')
            else:
                if df.index.name == 'date' or df.index.name is None:
                    if df.index.tz is None:
                        logger.debug("Localizing naive index to ET")
                        df.index = pd.to_datetime(df.index).tz_localize(eastern)
                    else:
                        logger.debug("Converting timezone-aware index to ET")
                        df.index = pd.to_datetime(df.index).tz_convert(eastern)
            logger.debug("Timezone handling completed with ET")
            return df
        except Exception as e:
            logger.error(f"Error handling timezone: {str(e)}")
            raise
            
    async def fetch_data_chunk(
        self,
        contract,
        end_date: datetime,
        duration: str,
        bar_size: str,
        max_retries: int = 4,
        base_delay: float = 15.0,
    ) -> Optional[pd.DataFrame]:
        try:
            end_date_str = end_date.strftime('%Y%m%d %H:%M:%S US/Eastern')
            is_crypto = CryptoUtils.is_crypto(contract.symbol)

            bars = None
            for attempt in range(1, max_retries + 1):
                try:
                    logger.info(
                        f"Requesting historical data ending at {end_date_str} "
                        f"(attempt {attempt}/{max_retries})..."
                    )
                    bars = await asyncio.wait_for(
                        self.connection.get_ib().reqHistoricalDataAsync(
                            contract,
                            endDateTime=end_date_str,
                            durationStr=duration,
                            barSizeSetting=bar_size,
                            whatToShow='AGGTRADES' if is_crypto else 'TRADES',
                            useRTH=False if is_crypto else True,
                            keepUpToDate=False,
                        ),
                        timeout=120,
                    )
                    if bars:
                        break
                except asyncio.TimeoutError:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        f"IBKR request timed out after 120s "
                        f"(attempt {attempt}/{max_retries}). "
                        f"Back-off {delay:.0f}s before retry."
                    )
                    await asyncio.sleep(delay)
                except Exception as req_err:
                    err_msg = str(req_err).lower()
                    is_pacing = "pacing" in err_msg or "162" in err_msg
                    delay = base_delay * (2 ** (attempt - 1))
                    if is_pacing:
                        delay = max(delay, 30)
                        logger.warning(
                            f"IBKR pacing violation detected: {req_err}. "
                            f"Back-off {delay:.0f}s (attempt {attempt}/{max_retries})."
                        )
                    else:
                        logger.warning(
                            f"IBKR request error: {req_err}. "
                            f"Back-off {delay:.0f}s (attempt {attempt}/{max_retries})."
                        )
                    await asyncio.sleep(delay)

            if not bars:
                logger.error(
                    f"No historical data received for {contract.symbol} "
                    f"ending {end_date_str} after {max_retries} attempts"
                )
                return None
              
            logger.info(f"Received {len(bars)} bars")
            eastern = timezone('America/New_York')
            
            # Convert naive timestamps to timezone-aware ET timestamps
            dates = [bar.date.replace(tzinfo=eastern) if bar.date.tzinfo is None else bar.date.astimezone(eastern) for bar in bars]
            
            df = pd.DataFrame({
                'date': dates,
                'open': [bar.open for bar in bars],
                'high': [bar.high for bar in bars],
                'low': [bar.low for bar in bars],
                'close': [bar.close for bar in bars],
                'volume': [bar.volume for bar in bars]
            })
            
            # Set date as index and ensure it's timezone-aware
            df = df.set_index('date')
            if df.index.tz is None:
                df.index = df.index.tz_localize(eastern)
            elif df.index.tz != eastern:
                df.index = df.index.tz_convert(eastern)
                
            return df
          
        except Exception as e:
            logger.error(f"Error fetching data chunk: {e}")
            return None
            
    async def get_historical_data_range(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        bar_size: str = '1 min',
        duration: str = '1 M',
        force_refresh: bool = False
    ) -> Optional[pd.DataFrame]:
        eastern = timezone('America/New_York')
        current_time = datetime.now(eastern)
        
        # Convert input dates to timezone-aware datetime objects
        try:
            start_dt = pd.to_datetime(start_date)
            if start_dt.tzinfo is None:
                start_dt = eastern.localize(start_dt)
            
            end_dt = pd.to_datetime(end_date)
            if end_dt.tzinfo is None:
                end_dt = eastern.localize(end_dt)
            
            # Ensure end_date is not in the future
            if end_dt > current_time:
                end_dt = current_time
                logger.info(f"Adjusted end date to current time: {end_dt}")
        except Exception as e:
            logger.error(f"Error parsing dates: {e}")
            return None
      
        if not force_refresh:
            cached_data = self._get_latest_cached_data(symbol, start_date, end_date, bar_size)
            if cached_data is not None:
                logger.info(f"Using cached data for {symbol} from {start_date} to {end_date}")
                return cached_data
          
        if not self.connection.is_connected():
            raise ConnectionError("Not connected to IBKR")
          
        # Create contract based on symbol type (crypto vs equity)
        logger.info(f"Creating contract for {symbol}...")
        contract = CryptoUtils.create_contract(symbol)
        qualified_contracts = await self.connection.get_ib().qualifyContractsAsync(contract)
        if not qualified_contracts:
            logger.error(f"Could not qualify contract for {symbol}")
            return None
        contract = qualified_contracts[0]
        logger.info(f"Contract qualified: {contract}")
      
        all_data = []
        current_end = end_dt
        pacing_delay = 10
        consecutive_failures = 0
        max_consecutive_failures = 3
        
        while current_end > start_dt:
            if current_end > current_time:
                current_end = current_time
                
            chunk = await self.fetch_data_chunk(contract, current_end, duration, bar_size)
            if chunk is None or chunk.empty:
                consecutive_failures += 1
                logger.warning(
                    f"No data for {symbol} ending {current_end} "
                    f"(consecutive failures: {consecutive_failures}/{max_consecutive_failures})"
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        f"STALL DETECTED: {symbol} had {consecutive_failures} consecutive "
                        f"empty responses. IBKR may be throttling or disconnected. "
                        f"Aborting data fetch for this symbol."
                    )
                    break
                current_end -= timedelta(days=30)
                await asyncio.sleep(pacing_delay)
                continue

            consecutive_failures = 0
            chunk_start = chunk.index.min()
            all_data.append(chunk)
            current_end = chunk_start - timedelta(seconds=1)
            logger.info(f"Fetched data for {symbol} from {chunk_start} to {chunk.index.max()}")
            await asyncio.sleep(pacing_delay)
          
        if not all_data:
            logger.error(f"No data fetched for {symbol} for the entire period")
            return None
          
        df = pd.concat(all_data).sort_index()
        df = df[(df.index >= start_dt) & (df.index <= end_dt)]
        self._save_data(df, symbol, start_date, end_date, bar_size)
        return df
    
    async def get_historical_data(
        self,
        symbol: str,
        duration: str = '1 M',
        bar_size: str = '1 hour',
        force_refresh: bool = False
    ) -> Optional[pd.DataFrame]:
        import warnings
        warnings.warn("get_historical_data is deprecated. Use get_historical_data_range with explicit start and end dates.", DeprecationWarning)
        
        eastern = timezone('America/New_York')
        end_date = datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')
        duration_parts = duration.split()
        duration_value = int(duration_parts[0])
        duration_unit = duration_parts[1].upper()
        
        if duration_unit == 'M':
            delta = timedelta(days=duration_value * 30)
        elif duration_unit == 'Y':
            delta = timedelta(days=duration_value * 365)
        else:
            delta = timedelta(days=duration_value)
            
        start_date = (datetime.now(eastern) - delta).strftime('%Y-%m-%d %H:%M:%S')
        return await self.get_historical_data_range(symbol, start_date, end_date, bar_size, duration, force_refresh)
    
    async def get_incremental_data(
        self,
        symbol: str,
        duration: str = '1 M',
        bar_size: str = '1 hour'
    ) -> Optional[pd.DataFrame]:
        import warnings
        warnings.warn("get_incremental_data is deprecated. Use get_historical_data_range for full range updates.", DeprecationWarning)
        
        eastern = timezone('America/New_York')
        end_date = datetime.now(eastern).strftime('%Y-%m-%d %H:%M:%S')
        
        cached_data = self._get_latest_cached_data(symbol, duration, end_date, bar_size)
        if cached_data is None:
            return await self.get_historical_data(symbol, duration, bar_size, force_refresh=True)
            
        latest_date = cached_data.index.max()
        latest_date_str = latest_date.strftime('%Y-%m-%d %H:%M:%S')
        return await self.get_historical_data_range(symbol, latest_date_str, end_date, bar_size, duration)
    
    async def get_historical_data_multiple(
        self,
        symbols: list,
        start_date: str,
        end_date: str,
        bar_size: str = '1 min',
        duration: str = '1 M',
        force_refresh: bool = False,
        max_concurrent: int = 5
    ) -> Dict[str, Optional[pd.DataFrame]]:
        """Fetch historical data for multiple stocks concurrently"""
        eastern = timezone('America/New_York')
        current_time = datetime.now(eastern)
        
        # Convert input dates to timezone-aware datetime objects
        try:
            start_dt = pd.to_datetime(start_date)
            if start_dt.tzinfo is None:
                start_dt = eastern.localize(start_dt)
            
            end_dt = pd.to_datetime(end_date)
            if end_dt.tzinfo is None:
                end_dt = eastern.localize(end_dt)
            
            if end_dt > current_time:
                end_dt = current_time
                logger.info(f"Adjusted end date to current time: {end_dt}")
        except Exception as e:
            logger.error(f"Error parsing dates: {e}")
            return {symbol: None for symbol in symbols}

        # Use semaphore to limit concurrent requests (respect IBKR rate limit ~60/min)
        semaphore = asyncio.Semaphore(max_concurrent)
        async def fetch_symbol_data(symbol):
            async with semaphore:
                if not force_refresh:
                    cached_data = self._get_latest_cached_data(symbol, start_date, end_date, bar_size)
                    if cached_data is not None:
                        logger.info(f"Using cached data for {symbol} from {start_date} to {end_date}")
                        return symbol, cached_data
                
                if not self.connection.is_connected():
                    logger.error(f"Not connected to IBKR for {symbol}")
                    return symbol, None
                
                # Create contract based on symbol type (crypto vs equity)
                logger.info(f"Creating contract for {symbol}...")
                contract = CryptoUtils.create_contract(symbol)
                qualified_contracts = await self.connection.get_ib().qualifyContractsAsync(contract)
                if not qualified_contracts:
                    logger.error(f"Could not qualify contract for {symbol}")
                    return symbol, None
                contract = qualified_contracts[0]
                logger.info(f"Contract qualified: {contract}")
                
                all_data = []
                current_end = end_dt
                pacing_delay = 10
                consecutive_failures = 0
                max_consecutive_failures = 3
                
                while current_end > start_dt:
                    if current_end > current_time:
                        current_end = current_time
                    
                    chunk = await self.fetch_data_chunk(contract, current_end, duration, bar_size)
                    if chunk is None or chunk.empty:
                        consecutive_failures += 1
                        logger.warning(
                            f"No data for {symbol} ending {current_end} "
                            f"(consecutive failures: {consecutive_failures}/{max_consecutive_failures})"
                        )
                        if consecutive_failures >= max_consecutive_failures:
                            logger.error(
                                f"STALL DETECTED: {symbol} had {consecutive_failures} consecutive "
                                f"empty responses. Aborting data fetch."
                            )
                            break
                        current_end -= timedelta(days=30)
                        await asyncio.sleep(pacing_delay)
                        continue

                    consecutive_failures = 0
                    chunk_start = chunk.index.min()
                    all_data.append(chunk)
                    current_end = chunk_start - timedelta(seconds=1)
                    logger.info(f"Fetched data for {symbol} from {chunk_start} to {chunk.index.max()}")
                    await asyncio.sleep(pacing_delay)
                
                if not all_data:
                    logger.error(f"No data fetched for {symbol} for the entire period")
                    return symbol, None
                
                df = pd.concat(all_data).sort_index()
                df = df[(df.index >= start_dt) & (df.index <= end_dt)]
                self._save_data(df, symbol, start_date, end_date, bar_size)
                return symbol, df

        # Run concurrent tasks
        tasks = [fetch_symbol_data(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks)
        
        return {symbol: df for symbol, df in results}