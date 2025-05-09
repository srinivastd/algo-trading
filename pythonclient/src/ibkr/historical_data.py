import os
import logging
import pandas as pd
from datetime import datetime, timedelta
from ib_insync import Stock
from typing import Optional
import asyncio
from pytz import timezone
from .connection import IBKRConnection

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
                df = pd.read_csv(filepath, index_col='date', parse_dates=True)
                df.index = pd.to_datetime(df.index)
                # Ensure ET timezone on load
                eastern = timezone('America/New_York')
                if df.index.tz is None:
                    logger.debug("Localizing cached data to ET")
                    df.index = df.index.tz_localize(eastern)
                elif df.index.tz != eastern:
                    logger.debug("Converting cached data to ET")
                    df.index = df.index.tz_convert(eastern)
                logger.info(f"Successfully loaded {len(df)} rows from cache with ET timezone")
                return df
            except Exception as e:
                logger.error(f"Error reading cached data: {str(e)}")
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
            if 'date' in df.columns:
                logger.debug("Converting date column to datetime")
                df['date'] = pd.to_datetime(df['date'])
                eastern = timezone('America/New_York')
                if df['date'].dt.tz is None:
                    logger.debug("Localizing naive datetime to ET")
                    df['date'] = df['date'].dt.tz_localize(eastern)
                else:
                    logger.debug("Converting timezone-aware datetime to ET")
                    df['date'] = df['date'].dt.tz_convert(eastern)
                logger.debug("Setting date as index")
                df = df.set_index('date')
            elif df.index.name == 'date':
                eastern = timezone('America/New_York')
                if df.index.tz is None:
                    logger.debug("Localizing naive index to ET")
                    df.index = df.index.tz_localize(eastern)
                else:
                    logger.debug("Converting timezone-aware index to ET")
                    df.index = df.index.tz_convert(eastern)
            logger.debug("Timezone handling completed with ET")
            return df
        except Exception as e:
            logger.error(f"Error handling timezone: {str(e)}")
            raise
            
    async def fetch_data_chunk(self, contract, end_date: datetime, duration: str, bar_size: str) -> Optional[pd.DataFrame]:
        try:
            logger.info(f"Requesting historical data ending at {end_date}...")
            bars = await self.connection.get_ib().reqHistoricalDataAsync(
                contract,
                endDateTime=end_date,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow='TRADES',
                useRTH=True,
                keepUpToDate=False
            )
            
            if not bars:
                logger.error("No historical data received")
                return None
              
            logger.info(f"Received {len(bars)} bars")
            df = pd.DataFrame(bars)
            df = self._handle_timezone(df)
            return df
          
        except Exception as e:
            logger.error(f"Error fetching data chunk: {str(e)}")
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
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
      
        if not force_refresh:
            cached_data = self._get_latest_cached_data(symbol, start_date, end_date, bar_size)
            if cached_data is not None:
                logger.info(f"Using cached data for {symbol} from {start_date} to {end_date}")
                return cached_data
          
        if not self.connection.is_connected():
            raise ConnectionError("Not connected to IBKR")
          
        logger.info(f"Defining contract for {symbol}...")
        contract = Stock(symbol, 'SMART', 'USD')
        qualified_contracts = await self.connection.get_ib().qualifyContractsAsync(contract)
        if not qualified_contracts:
            logger.error(f"Could not qualify contract for {symbol}")
            return None
        contract = qualified_contracts[0]
        logger.info(f"Contract qualified: {contract}")
      
        all_data = []
        current_end = end_dt
        while current_end > start_dt:
            chunk = await self.fetch_data_chunk(contract, current_end, duration, bar_size)
            if chunk is None or chunk.empty:
                logger.warning(f"No data for period ending {current_end}")
                current_end -= timedelta(days=30)
                await asyncio.sleep(10)
                continue
              
            chunk_start = chunk.index.min()
            all_data.append(chunk)
            current_end = chunk_start - timedelta(seconds=1)
            logger.info(f"Fetched data from {chunk_start} to {chunk.index.max()}")
            await asyncio.sleep(10)
          
        if not all_data:
            logger.error("No data fetched for the entire period")
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
        end_date = datetime.now(timezone('America/New_York')).strftime('%Y-%m-%d')
        return await self.get_historical_data_range(symbol, (datetime.now(timezone('America/New_York')) - timedelta(days=int(duration.split(' ')[0]) * 30 if 'M' in duration else 365)).strftime('%Y-%m-%d'), end_date, bar_size)
    
    async def get_incremental_data(
        self,
        symbol: str,
        duration: str = '1 M',
        bar_size: str = '1 hour'
    ) -> Optional[pd.DataFrame]:
        import warnings
        warnings.warn("get_incremental_data is deprecated. Use get_historical_data_range for full range updates.", DeprecationWarning)
        cached_data = self._get_latest_cached_data(symbol, duration, bar_size)
        if cached_data is None:
            return await self.get_historical_data(symbol, duration, bar_size, force_refresh=True)
        latest_date = cached_data.index.max().strftime('%Y-%m-%d')
        end_date = datetime.now(timezone('America/New_York')).strftime('%Y-%m-%d')
        return await self.get_historical_data_range(symbol, latest_date, end_date, bar_size)