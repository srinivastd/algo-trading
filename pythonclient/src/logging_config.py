#!/usr/bin/env python3
"""
Centralized daily-rotating logging configuration.
Prevents log files from growing indefinitely by creating one file per day
(e.g. logs/stock_analysis_20260425.log) with backup of previous days.

Also supports structured JSON logging for market_data logger (one JSON object per line).
"""
import logging
import logging.handlers
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Any


class JsonFormatter(logging.Formatter):
    """Formats log records as one JSON object per line (for market_data logger)."""
    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # If the message is already JSON (as used in main.py), parse and merge it
        try:
            if record.msg and isinstance(record.msg, str) and record.msg.strip().startswith('{'):
                data = json.loads(record.msg)
                log_entry.update(data)
            elif record.args and isinstance(record.args, dict):
                log_entry.update(record.args)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return json.dumps(log_entry)


def setup_daily_logging(
    log_name: str = "stock_analysis",
    level: int = logging.INFO,
    console: bool = True,
    log_dir: Optional[Path] = None,
    backup_count: int = 30,  # Keep last 30 days
) -> logging.Logger:
    """
    Set up daily rotating file logging + colored console.
    Configures root logger ONCE to avoid conflicts between main.py and autoresearch.py (Bug 2 fix).
    
    Creates logs/ directory with files like:
        stock_analysis_20260425.log
        autoresearch_20260425.log
    """
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Root logger - configure only once
    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate configuration (Bug 2 fix): only configure if no TimedRotatingFileHandler exists
    if any(isinstance(h, logging.handlers.TimedRotatingFileHandler) for h in root.handlers):
        return logging.getLogger(log_name)

    date_str = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"{log_name}_{date_str}.log"

    # File handler - daily rotation at midnight, keep last N days
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_file),
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root.addHandler(file_handler)

    if console:
        # Colored console handler
        class ColoredFormatter(logging.Formatter):
            grey = "\x1b[38;21m"
            blue = "\x1b[38;5;39m"
            yellow = "\x1b[38;5;226m"
            red = "\x1b[38;5;196m"
            bold_red = "\x1b[31;1m"
            reset = "\x1b[0m"

            def __init__(self, fmt: str):
                super().__init__()
                self.fmt = fmt
                self.FORMATS = {
                    logging.DEBUG: self.grey + self.fmt + self.reset,
                    logging.INFO: self.blue + self.fmt + self.reset,
                    logging.WARNING: self.yellow + self.fmt + self.reset,
                    logging.ERROR: self.red + self.fmt + self.reset,
                    logging.CRITICAL: self.bold_red + self.fmt + self.reset,
                }

            def format(self, record):
                log_fmt = self.FORMATS.get(record.levelno, self.fmt)
                formatter = logging.Formatter(log_fmt)
                return formatter.format(record)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(
            ColoredFormatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        root.addHandler(console_handler)

    logger = logging.getLogger(log_name)
    logger.info(f"Logging initialized to daily file: {log_file}")
    logger.info(f"Keeping last {backup_count} days of logs in {log_dir}")
    
    return logger


def setup_market_data_logger(log_dir: Optional[Path] = None) -> logging.Logger:
    """
    Set up the market_data logger with pure JSON output (one object per line).
    This fixes Bug 3: previously propagate=False with no handler meant no output.
    """
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    market_logger = logging.getLogger('market_data')
    market_logger.setLevel(logging.INFO)
    
    # Remove any existing handlers to avoid duplicates
    for handler in market_logger.handlers[:]:
        market_logger.removeHandler(handler)
    
    # Always propagate to root for console/file logging, but also add dedicated JSON handler
    market_logger.propagate = True
    
    date_str = datetime.now().strftime("%Y%m%d")
    json_log_file = log_dir / f"market_data_{date_str}.json"
    
    # JSON file handler - one JSON object per line, rotates daily
    json_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(json_log_file),
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    json_handler.setFormatter(JsonFormatter())
    market_logger.addHandler(json_handler)
    
    market_logger.info("Market data JSON logging initialized")
    return market_logger


def get_logger(name: str = "root") -> logging.Logger:
    """Get a logger that will use the daily setup."""
    return logging.getLogger(name)
