"""
SQLite-backed trade journal for persistent run, signal, and trade lifecycle memory.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping


def _to_iso(ts: Any) -> str:
    if ts is None:
        return datetime.utcnow().isoformat()
    if hasattr(ts, "isoformat"):
        return ts.isoformat()
    return str(ts)


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


class TradeJournal:
    """Persistent journal for watch-mode decisions and outcomes."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _dump_json(value: Any) -> str:
        return json.dumps(_sanitize_json(value), separators=(",", ":"))

    def _init_db(self) -> None:
        with self._conn() as con:
            con.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    strategy TEXT NOT NULL,
                    symbols_json TEXT NOT NULL,
                    params_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    signal_value INTEGER NOT NULL,
                    price REAL,
                    indicators_json TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_signals_run_symbol_time
                    ON signals(run_id, symbol, timestamp);

                CREATE TABLE IF NOT EXISTS market_regimes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    adx REAL,
                    vol_ratio REAL,
                    trend_direction TEXT,
                    regime_label TEXT,
                    regime_confidence REAL
                );
                CREATE INDEX IF NOT EXISTS idx_market_regimes_symbol_time
                    ON market_regimes(symbol, timestamp);

                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    signal_value INTEGER NOT NULL,
                    intended_entry_price REAL NOT NULL,
                    intended_shares INTEGER NOT NULL,
                    stop_loss REAL,
                    target REAL,
                    status TEXT NOT NULL,
                    entry_fill_price REAL,
                    entry_fill_time TEXT,
                    exit_fill_price REAL,
                    exit_fill_time TEXT,
                    exit_reason TEXT,
                    realized_pnl REAL,
                    commission REAL,
                    slippage_bps REAL,
                    indicator_snapshot_json TEXT NOT NULL,
                    memory_consulted INTEGER NOT NULL DEFAULT 0,
                    memory_verdict_json TEXT,
                    classification TEXT,
                    order_id INTEGER,
                    stop_order_id INTEGER,
                    target_order_id INTEGER,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_trades_run_symbol
                    ON trades(run_id, symbol);

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    order_id INTEGER,
                    exec_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    commission REAL,
                    time TEXT NOT NULL,
                    is_entry INTEGER NOT NULL,
                    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_fills_trade_time
                    ON fills(trade_id, time);

                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY,
                    trade_id TEXT,
                    symbol TEXT NOT NULL,
                    order_type TEXT,
                    action TEXT,
                    qty REAL,
                    limit_price REAL,
                    status TEXT,
                    parent_order_id INTEGER,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_orders_trade
                    ON orders(trade_id);

                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    net_liquidation REAL,
                    positions_json TEXT NOT NULL,
                    unrealized_pnl REAL,
                    realized_pnl_today REAL
                );
                CREATE INDEX IF NOT EXISTS idx_daily_snapshots_time
                    ON daily_snapshots(time);

                CREATE TABLE IF NOT EXISTS execution_quality (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    time_of_day_bucket TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    avg_slippage_bps REAL,
                    fill_rate REAL,
                    avg_fill_time_ms REAL,
                    sample_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_quality_key
                    ON execution_quality(symbol, time_of_day_bucket, order_type);
                """
            )

    def start_run(self, strategy: str, symbols: list[str], params: Mapping[str, Any]) -> str:
        run_id = f"RUN_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        now = datetime.utcnow().isoformat()
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO runs(run_id, strategy, symbols_json, params_json, started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, strategy, self._dump_json(symbols), self._dump_json(dict(params)), now),
            )
        return run_id

    def end_run(self, run_id: str) -> None:
        with self._conn() as con:
            con.execute(
                "UPDATE runs SET ended_at = ? WHERE run_id = ? AND ended_at IS NULL",
                (datetime.utcnow().isoformat(), run_id),
            )

    def log_signal(
        self,
        run_id: str,
        symbol: str,
        timestamp: Any,
        signal_value: int,
        price: float | None,
        indicators: Mapping[str, Any],
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO signals(run_id, symbol, timestamp, signal_value, price, indicators_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, symbol.upper(), _to_iso(timestamp), int(signal_value), price, self._dump_json(dict(indicators))),
            )

    def log_market_regime(
        self,
        symbol: str,
        timestamp: Any,
        *,
        adx: float | None,
        vol_ratio: float | None,
        trend_direction: str | None,
        regime_label: str | None,
        regime_confidence: float | None,
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO market_regimes(
                    symbol, timestamp, adx, vol_ratio, trend_direction, regime_label, regime_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol.upper(),
                    _to_iso(timestamp),
                    adx,
                    vol_ratio,
                    trend_direction,
                    regime_label,
                    regime_confidence,
                ),
            )

    def log_trade_attempt(
        self,
        *,
        trade_id: str,
        run_id: str,
        symbol: str,
        side: str,
        signal_value: int,
        intended_entry_price: float,
        intended_shares: int,
        stop_loss: float | None,
        target: float | None,
        indicator_snapshot: Mapping[str, Any],
        memory_consulted: bool = False,
        memory_verdict: Mapping[str, Any] | None = None,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._conn() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO trades(
                    trade_id, run_id, symbol, side, signal_value, intended_entry_price, intended_shares,
                    stop_loss, target, status, indicator_snapshot_json, memory_consulted, memory_verdict_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    run_id,
                    symbol.upper(),
                    side,
                    int(signal_value),
                    float(intended_entry_price),
                    int(intended_shares),
                    stop_loss,
                    target,
                    "attempted",
                    self._dump_json(dict(indicator_snapshot)),
                    1 if memory_consulted else 0,
                    self._dump_json(dict(memory_verdict)) if memory_verdict else None,
                    now,
                    now,
                ),
            )

    def update_trade_execution_result(
        self,
        *,
        trade_id: str,
        status: str,
        error_message: str | None = None,
        order_id: int | None = None,
        stop_order_id: int | None = None,
        target_order_id: int | None = None,
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                UPDATE trades
                SET status = ?, error_message = ?, order_id = ?, stop_order_id = ?, target_order_id = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (
                    status,
                    error_message,
                    order_id,
                    stop_order_id,
                    target_order_id,
                    datetime.utcnow().isoformat(),
                    trade_id,
                ),
            )

    def upsert_order(
        self,
        *,
        order_id: int,
        trade_id: str | None,
        symbol: str,
        order_type: str | None,
        action: str | None,
        qty: float | None,
        limit_price: float | None,
        status: str | None,
        parent_order_id: int | None,
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO orders(order_id, trade_id, symbol, order_type, action, qty, limit_price, status, parent_order_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    trade_id = excluded.trade_id,
                    symbol = excluded.symbol,
                    order_type = excluded.order_type,
                    action = excluded.action,
                    qty = excluded.qty,
                    limit_price = excluded.limit_price,
                    status = excluded.status,
                    parent_order_id = excluded.parent_order_id,
                    updated_at = excluded.updated_at
                """,
                (
                    order_id,
                    trade_id,
                    symbol.upper(),
                    order_type,
                    action,
                    qty,
                    limit_price,
                    status,
                    parent_order_id,
                    datetime.utcnow().isoformat(),
                ),
            )

    def record_fill(
        self,
        *,
        trade_id: str,
        order_id: int | None,
        exec_id: str | None,
        symbol: str,
        side: str | None,
        qty: float,
        price: float,
        commission: float | None,
        time: Any,
        is_entry: bool,
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO fills(
                    trade_id, order_id, exec_id, symbol, side, qty, price, commission, time, is_entry
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    order_id,
                    exec_id,
                    symbol.upper(),
                    side,
                    float(qty),
                    float(price),
                    commission,
                    _to_iso(time),
                    1 if is_entry else 0,
                ),
            )

    def mark_trade_entry_fill(
        self,
        *,
        trade_id: str,
        entry_fill_price: float,
        entry_fill_time: Any,
        slippage_bps: float | None,
        status: str = "open",
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                UPDATE trades
                SET entry_fill_price = ?, entry_fill_time = ?, slippage_bps = ?, status = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (
                    float(entry_fill_price),
                    _to_iso(entry_fill_time),
                    slippage_bps,
                    status,
                    datetime.utcnow().isoformat(),
                    trade_id,
                ),
            )

    def close_trade(
        self,
        *,
        trade_id: str,
        exit_fill_price: float,
        exit_fill_time: Any,
        exit_reason: str,
        realized_pnl: float | None,
        status: str = "closed",
    ) -> None:
        with self._conn() as con:
            con.execute(
                """
                UPDATE trades
                SET status = ?, exit_fill_price = ?, exit_fill_time = ?, exit_reason = ?, realized_pnl = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (
                    status,
                    float(exit_fill_price),
                    _to_iso(exit_fill_time),
                    exit_reason,
                    realized_pnl,
                    datetime.utcnow().isoformat(),
                    trade_id,
                ),
            )

    def insert_daily_snapshot(
        self,
        *,
        timestamp: Any,
        net_liquidation: float | None,
        positions: list[dict[str, Any]],
        unrealized_pnl: float | None,
        realized_pnl_today: float | None,
    ) -> None:
        iso = _to_iso(timestamp)
        day = iso.split("T", 1)[0]
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO daily_snapshots(
                    date, time, net_liquidation, positions_json, unrealized_pnl, realized_pnl_today
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    day,
                    iso,
                    net_liquidation,
                    self._dump_json(positions),
                    unrealized_pnl,
                    realized_pnl_today,
                ),
            )
