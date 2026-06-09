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


def _finite_number(value: Any) -> bool:
    try:
        if value is None:
            return False
        v = float(value)
        return not (math.isnan(v) or math.isinf(v))
    except (TypeError, ValueError):
        return False


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

                CREATE TABLE IF NOT EXISTS ib_executions (
                    exec_id TEXT PRIMARY KEY,
                    order_id INTEGER NOT NULL,
                    perm_id INTEGER,
                    client_id INTEGER,
                    symbol TEXT NOT NULL,
                    side TEXT,
                    shares REAL NOT NULL,
                    price REAL NOT NULL,
                    time TEXT NOT NULL,
                    commission REAL,
                    trade_id TEXT,
                    ingested_at TEXT NOT NULL,
                    FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_ib_executions_order
                    ON ib_executions(order_id);
                CREATE INDEX IF NOT EXISTS idx_ib_executions_symbol_time
                    ON ib_executions(symbol, time);

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
            # Lightweight schema migration for existing DBs created before
            # classification scoring fields were introduced.
            self._ensure_column(con, "trades", "classification_score", "INTEGER")
            self._ensure_column(con, "trades", "classification_rationale", "TEXT")
            self._ensure_column(con, "daily_snapshots", "ib_daily_pnl", "REAL")
            self._ensure_column(con, "runs", "git_commit", "TEXT")
            self._ensure_column(con, "runs", "git_commit_short", "TEXT")
            self._ensure_column(con, "runs", "git_branch", "TEXT")
            self._ensure_column(con, "runs", "git_dirty", "INTEGER")
            self._ensure_ib_executions_table(con)
            self._ensure_fills_exec_id_unique_index(con)

    def _ensure_ib_executions_table(self, con: sqlite3.Connection) -> None:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ib_executions (
                exec_id TEXT PRIMARY KEY,
                order_id INTEGER NOT NULL,
                perm_id INTEGER,
                client_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT,
                shares REAL NOT NULL,
                price REAL NOT NULL,
                time TEXT NOT NULL,
                commission REAL,
                trade_id TEXT,
                ingested_at TEXT NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES trades(trade_id)
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_ib_executions_order ON ib_executions(order_id)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_ib_executions_symbol_time ON ib_executions(symbol, time)"
        )

    def _ensure_fills_exec_id_unique_index(self, con: sqlite3.Connection) -> None:
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_fills_exec_id
            ON fills(exec_id) WHERE exec_id IS NOT NULL AND length(exec_id) > 0
            """
        )

    @staticmethod
    def _column_exists(con: sqlite3.Connection, table: str, column: str) -> bool:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(r[1]) == column for r in rows)

    def _ensure_column(self, con: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
        if self._column_exists(con, table, column):
            return
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    def start_run(
        self,
        strategy: str,
        symbols: list[str],
        params: Mapping[str, Any],
        *,
        capture_git: bool = True,
    ) -> str:
        run_id = f"RUN_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        now = datetime.utcnow().isoformat()
        params_dict = dict(params)
        if capture_git and "code_provenance" not in params_dict:
            try:
                from git_provenance import get_git_provenance

                params_dict["code_provenance"] = get_git_provenance()
            except Exception:
                params_dict.setdefault("code_provenance", {"available": False, "reason": "capture_failed"})

        prov = params_dict.get("code_provenance")
        prov = prov if isinstance(prov, dict) else {}
        git_commit = prov.get("commit") if prov.get("available") else None
        git_commit_short = prov.get("commit_short") if prov.get("available") else None
        git_branch = prov.get("branch") if prov.get("available") else None
        git_dirty = (
            int(bool(prov.get("dirty")))
            if prov.get("available") and prov.get("dirty") is not None
            else None
        )

        with self._conn() as con:
            con.execute(
                """
                INSERT INTO runs(
                    run_id, strategy, symbols_json, params_json, started_at,
                    git_commit, git_commit_short, git_branch, git_dirty
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    strategy,
                    self._dump_json(symbols),
                    self._dump_json(params_dict),
                    now,
                    git_commit,
                    git_commit_short,
                    git_branch,
                    git_dirty,
                ),
            )
        return run_id

    def get_run_params(self, run_id: str) -> dict[str, Any] | None:
        """
        Return parsed ``params_json`` for a journal run, or ``None`` if the run_id is missing
        or invalid JSON.
        """
        rid = (run_id or "").strip()
        if not rid:
            return None
        with self._conn() as con:
            row = con.execute(
                "SELECT params_json FROM runs WHERE run_id = ?",
                (rid,),
            ).fetchone()
        if not row or not row[0]:
            return None
        try:
            obj = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Full run row including git provenance columns and parsed params."""
        rid = (run_id or "").strip()
        if not rid:
            return None
        with self._conn() as con:
            row = con.execute(
                """
                SELECT run_id, strategy, symbols_json, params_json, started_at, ended_at,
                       git_commit, git_commit_short, git_branch, git_dirty
                FROM runs WHERE run_id = ?
                """,
                (rid,),
            ).fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["symbols"] = json.loads(out.pop("symbols_json") or "[]")
        except json.JSONDecodeError:
            out["symbols"] = []
        try:
            out["params"] = json.loads(out.pop("params_json") or "{}")
        except json.JSONDecodeError:
            out["params"] = {}
        out["code_provenance"] = out["params"].get("code_provenance")
        return out

    def list_recent_runs(self, limit: int = 30) -> list[dict[str, Any]]:
        """Recent journal runs (for zone UI / tooling). Newest first."""
        lim = max(1, min(int(limit), 200))
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT run_id, strategy, symbols_json, started_at,
                       git_commit_short, git_branch, git_dirty
                FROM runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            sym_raw = r["symbols_json"] or "[]"
            try:
                syms = json.loads(sym_raw) if isinstance(sym_raw, str) else sym_raw
            except json.JSONDecodeError:
                syms = []
            if not isinstance(syms, list):
                syms = []
            out.append(
                {
                    "run_id": r["run_id"],
                    "strategy": r["strategy"],
                    "started_at": r["started_at"],
                    "symbols": [str(s).upper() for s in syms],
                    "git_commit_short": r["git_commit_short"],
                    "git_branch": r["git_branch"],
                    "git_dirty": r["git_dirty"],
                }
            )
        return out

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
                INSERT INTO trades(
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

    def resolve_trade_id_for_order(self, order_id: int | None) -> str | None:
        """Resolve ``trade_id`` from ``orders`` or bracket columns on ``trades``."""
        if order_id is None or int(order_id) <= 0:
            return None
        oid = int(order_id)
        with self._conn() as con:
            row = con.execute(
                "SELECT trade_id FROM orders WHERE order_id = ? AND trade_id IS NOT NULL LIMIT 1",
                (oid,),
            ).fetchone()
            if row and row[0]:
                return str(row[0])
            row = con.execute(
                """
                SELECT trade_id FROM trades
                WHERE order_id = ? OR stop_order_id = ? OR target_order_id = ?
                LIMIT 1
                """,
                (oid, oid, oid),
            ).fetchone()
            if row and row[0]:
                return str(row[0])
        return None

    @staticmethod
    def _infer_is_entry_for_order(
        con: sqlite3.Connection, trade_id: str, order_id: int
    ) -> bool:
        row = con.execute(
            "SELECT order_id, stop_order_id, target_order_id FROM trades WHERE trade_id = ?",
            (trade_id,),
        ).fetchone()
        if row is None:
            return False
        entry_oid, stop_oid, tgt_oid = row[0], row[1], row[2]
        if entry_oid is not None and int(entry_oid) == int(order_id):
            return True
        if stop_oid is not None and int(stop_oid) == int(order_id):
            return False
        if tgt_oid is not None and int(tgt_oid) == int(order_id):
            return False
        return False

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
        ex = (exec_id or "").strip()
        with self._conn() as con:
            if ex:
                con.execute(
                    """
                    INSERT OR IGNORE INTO fills(
                        trade_id, order_id, exec_id, symbol, side, qty, price, commission, time, is_entry
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        order_id,
                        ex,
                        symbol.upper(),
                        side,
                        float(qty),
                        float(price),
                        commission,
                        _to_iso(time),
                        1 if is_entry else 0,
                    ),
                )
            else:
                con.execute(
                    """
                    INSERT INTO fills(
                        trade_id, order_id, exec_id, symbol, side, qty, price, commission, time, is_entry
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        order_id,
                        None,
                        symbol.upper(),
                        side,
                        float(qty),
                        float(price),
                        commission,
                        _to_iso(time),
                        1 if is_entry else 0,
                    ),
                )

    def ingest_ib_execution_fill(self, fill: Any) -> None:
        """
        Real-time path: persist IB ``Fill`` / execution into ``ib_executions`` and ``fills``
        when ``exec_id`` is present (deduped) and ``order_id`` maps to a ``trade_id``.
        """
        ex = getattr(fill, "execution", None)
        contract = getattr(fill, "contract", None)
        if ex is None:
            return
        exec_id = (getattr(ex, "execId", None) or "").strip()
        if not exec_id:
            return
        try:
            oid = int(getattr(ex, "orderId", 0) or 0)
        except (TypeError, ValueError):
            oid = 0
        sym = str(getattr(contract, "symbol", "") or "").strip().upper()
        if not sym:
            return
        side = getattr(ex, "side", None)
        try:
            shares = float(getattr(ex, "shares", 0) or 0)
            price = float(getattr(ex, "price", 0) or 0)
        except (TypeError, ValueError):
            return
        t = getattr(ex, "time", None)
        comm: float | None = None
        cr = getattr(fill, "commissionReport", None)
        if cr is not None:
            try:
                c = float(getattr(cr, "commission", 0) or 0)
                comm = c
            except (TypeError, ValueError):
                pass
        try:
            perm_id = int(getattr(ex, "permId", 0) or 0) or None
        except (TypeError, ValueError):
            perm_id = None
        try:
            client_id = int(getattr(ex, "clientId", 0) or 0) or None
        except (TypeError, ValueError):
            client_id = None

        now = datetime.utcnow().isoformat()
        with self._conn() as con:
            tid: str | None = None
            if oid > 0:
                row = con.execute(
                    "SELECT trade_id FROM orders WHERE order_id = ? AND trade_id IS NOT NULL LIMIT 1",
                    (oid,),
                ).fetchone()
                if row and row[0]:
                    tid = str(row[0])
                if not tid:
                    row = con.execute(
                        """
                        SELECT trade_id FROM trades
                        WHERE order_id = ? OR stop_order_id = ? OR target_order_id = ?
                        LIMIT 1
                        """,
                        (oid, oid, oid),
                    ).fetchone()
                    if row and row[0]:
                        tid = str(row[0])
            con.execute(
                """
                INSERT INTO ib_executions(
                    exec_id, order_id, perm_id, client_id, symbol, side, shares, price, time,
                    commission, trade_id, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(exec_id) DO UPDATE SET
                    commission = COALESCE(excluded.commission, ib_executions.commission),
                    trade_id = COALESCE(excluded.trade_id, ib_executions.trade_id),
                    ingested_at = excluded.ingested_at
                """,
                (
                    exec_id,
                    oid,
                    perm_id,
                    client_id,
                    sym,
                    str(side) if side is not None else None,
                    shares,
                    price,
                    _to_iso(t),
                    comm,
                    tid,
                    now,
                ),
            )
            if tid and oid > 0:
                is_entry = self._infer_is_entry_for_order(con, tid, oid)
                con.execute(
                    """
                    INSERT OR IGNORE INTO fills(
                        trade_id, order_id, exec_id, symbol, side, qty, price, commission, time, is_entry
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tid,
                        oid,
                        exec_id,
                        sym,
                        str(side) if side is not None else None,
                        shares,
                        price,
                        comm,
                        _to_iso(t),
                        1 if is_entry else 0,
                    ),
                )

    def apply_ib_commission_report(self, fill: Any, commission: float | None) -> None:
        """Update ``ib_executions`` and ``fills`` when IB sends a standalone commission report."""
        ex = getattr(fill, "execution", None)
        if ex is None or commission is None:
            return
        exec_id = (getattr(ex, "execId", None) or "").strip()
        if not exec_id:
            return
        with self._conn() as con:
            con.execute(
                "UPDATE ib_executions SET commission = ? WHERE exec_id = ?",
                (float(commission), exec_id),
            )
            con.execute(
                "UPDATE fills SET commission = ? WHERE exec_id = ?",
                (float(commission), exec_id),
            )

    def backfill_executions_from_ib_fills(self, fills: list[Any]) -> int:
        """Ingest a batch from ``reqExecutionsAsync`` (e.g. at watch startup). Returns count processed."""
        n = 0
        for f in fills or []:
            try:
                self.ingest_ib_execution_fill(f)
                n += 1
            except Exception:
                continue
        return n

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

    def merge_indicator_snapshot(
        self,
        *,
        trade_id: str,
        patch: Mapping[str, Any],
        fill_keys: tuple[str, ...] = ("adx", "rsi", "plus_di", "minus_di"),
    ) -> bool:
        """
        Shallow-merge numeric indicator fields into ``indicator_snapshot_json`` for keys
        that are missing or non-finite in the stored snapshot. Preserves bootstrap metadata.
        Returns True if at least one key was written.

        **Exit-time context** (RSI/MACD/zones at close) uses ``merge_exit_indicator_snapshot``
        with ``exit_*`` keys; see ``get_trades_touching_calendar_day`` /
        ``_split_indicator_entry_exit``.
        """
        if not patch:
            return False
        now = datetime.utcnow().isoformat()
        with self._conn() as con:
            row = con.execute(
                "SELECT indicator_snapshot_json FROM trades WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
            if row is None:
                return False
            raw = row[0] or "{}"
            try:
                cur = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                cur = {}
            if not isinstance(cur, dict):
                cur = {}
            merged = dict(cur)
            changed = False
            patch_s = _sanitize_json(dict(patch))
            if not isinstance(patch_s, dict):
                return False
            for k in fill_keys:
                if k not in patch_s:
                    continue
                pv = patch_s[k]
                if not _finite_number(pv):
                    continue
                if k not in merged or not _finite_number(merged.get(k)):
                    merged[k] = pv
                    changed = True
            if not changed:
                return False
            con.execute(
                """
                UPDATE trades
                SET indicator_snapshot_json = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (self._dump_json(merged), now, trade_id),
            )
        return True

    def merge_exit_indicator_snapshot(
        self,
        *,
        trade_id: str,
        patch: Mapping[str, Any],
    ) -> bool:
        """
        Merge **exit** fields into ``indicator_snapshot_json``.

        Convention (see ``_split_indicator_entry_exit`` / dashboard): keys prefixed ``exit_``
        hold bar/time/indicator context at **close**, distinct from entry-time keys.

        Unlike ``merge_indicator_snapshot``, this **always overwrites** matching keys so the
        latest close wins on reconcile/retry.

        Keys in ``patch`` may be passed with or without ``exit_`` prefix; bare names are
        prefixed automatically.
        """
        if not patch:
            return False
        now = datetime.utcnow().isoformat()
        with self._conn() as con:
            row = con.execute(
                "SELECT indicator_snapshot_json FROM trades WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
            if row is None:
                return False
            raw = row[0] or "{}"
            try:
                cur = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                cur = {}
            if not isinstance(cur, dict):
                cur = {}
            merged = dict(cur)
            patch_s = _sanitize_json(dict(patch))
            if not isinstance(patch_s, dict):
                return False
            changed = False
            for k, v in patch_s.items():
                ks = str(k)
                key = ks if ks.lower().startswith("exit_") else f"exit_{ks}"
                merged[key] = v
                changed = True
            merged["exit_snapshot_merged_at"] = now
            if not changed:
                return False
            con.execute(
                """
                UPDATE trades
                SET indicator_snapshot_json = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (self._dump_json(merged), now, trade_id),
            )
        return True

    def set_trade_classification(
        self,
        *,
        trade_id: str,
        classification: str | None,
        classification_score: int | None = None,
        classification_rationale: str | None = None,
    ) -> None:
        """Post-hoc or automated quality / review labels on `trades` rows."""
        with self._conn() as con:
            con.execute(
                """
                UPDATE trades
                SET classification = ?, classification_score = ?, classification_rationale = ?, updated_at = ?
                WHERE trade_id = ?
                """,
                (
                    classification,
                    classification_score,
                    classification_rationale,
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
        ib_daily_pnl: float | None = None,
    ) -> None:
        iso = _to_iso(timestamp)
        day = iso.split("T", 1)[0]
        with self._conn() as con:
            con.execute(
                """
                INSERT INTO daily_snapshots(
                    date, time, net_liquidation, positions_json, unrealized_pnl, realized_pnl_today, ib_daily_pnl
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    day,
                    iso,
                    net_liquidation,
                    self._dump_json(positions),
                    unrealized_pnl,
                    realized_pnl_today,
                    ib_daily_pnl,
                ),
            )

    @staticmethod
    def _daily_snapshot_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        pos_raw = row["positions_json"] or "[]"
        try:
            positions = json.loads(pos_raw) if isinstance(pos_raw, str) else pos_raw
        except json.JSONDecodeError:
            positions = []
        return {
            "snapshot_id": row["id"],
            "date": row["date"],
            "time": row["time"],
            "net_liquidation": row["net_liquidation"],
            "unrealized_pnl": row["unrealized_pnl"],
            "realized_pnl_today": row["realized_pnl_today"],
            "ib_daily_pnl": row["ib_daily_pnl"],
            "positions": positions,
        }

    def _journal_closed_realized_for_calendar_day(self, day_yyyy_mm_dd: str) -> float | None:
        """
        Sum of ``realized_pnl`` for closed trades whose ``exit_fill_time`` calendar day matches
        ``day_yyyy_mm_dd`` (ISO date). Used when IB ``reqPnL`` fields were not persisted (NULL).
        """
        with self._conn() as con:
            row = con.execute(
                """
                SELECT COUNT(*), SUM(realized_pnl)
                FROM trades
                WHERE LOWER(TRIM(status)) = 'closed'
                  AND exit_fill_time IS NOT NULL
                  AND length(trim(exit_fill_time)) >= 10
                  AND substr(trim(exit_fill_time), 1, 10) = ?
                """,
                (day_yyyy_mm_dd,),
            ).fetchone()
        if row is None:
            return None
        n, s = int(row[0] or 0), row[1]
        if n == 0:
            return None
        if s is None:
            return None
        return float(s)

    def _enrich_snapshot_pnl_fallbacks(self, d: dict[str, Any]) -> None:
        """Fill NULL IB P&amp;L columns from journal exits on the snapshot calendar day (display/API)."""
        day = d.get("date")
        if not day or not isinstance(day, str):
            return
        if d.get("realized_pnl_today") is not None and d.get("ib_daily_pnl") is not None:
            return
        jr = self._journal_closed_realized_for_calendar_day(day)
        if jr is None:
            return
        if d.get("realized_pnl_today") is None:
            d["realized_pnl_today"] = jr
            d["realized_pnl_today_src"] = "journal"
        if d.get("ib_daily_pnl") is None:
            d["ib_daily_pnl"] = jr
            d["ib_daily_pnl_src"] = "journal"

    def get_latest_daily_snapshot(self) -> dict[str, Any] | None:
        """Latest row from ``daily_snapshots`` (IB reqPnL + NL + positions)."""
        with self._conn() as con:
            row = con.execute(
                """
                SELECT id, date, time, net_liquidation, unrealized_pnl, realized_pnl_today, ib_daily_pnl, positions_json
                FROM daily_snapshots
                ORDER BY time DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        d = self._daily_snapshot_row_to_dict(row)
        self._enrich_snapshot_pnl_fallbacks(d)
        return d

    def get_daily_snapshot_last_for_date(self, date_yyyy_mm_dd: str) -> dict[str, Any] | None:
        """Last ``daily_snapshots`` row for calendar ``date`` column (matches persisted snapshot day)."""
        if not date_yyyy_mm_dd or len(date_yyyy_mm_dd) < 10:
            return None
        day = date_yyyy_mm_dd[:10]
        with self._conn() as con:
            row = con.execute(
                """
                SELECT id, date, time, net_liquidation, unrealized_pnl, realized_pnl_today, ib_daily_pnl, positions_json
                FROM daily_snapshots
                WHERE date = ?
                ORDER BY time DESC
                LIMIT 1
                """,
                (day,),
            ).fetchone()
        if row is None:
            return None
        d = self._daily_snapshot_row_to_dict(row)
        self._enrich_snapshot_pnl_fallbacks(d)
        return d

    def get_daily_snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        """Single ``daily_snapshots`` row by primary key (for UI ``id=…`` lines)."""
        with self._conn() as con:
            row = con.execute(
                """
                SELECT id, date, time, net_liquidation, unrealized_pnl, realized_pnl_today, ib_daily_pnl, positions_json
                FROM daily_snapshots
                WHERE id = ?
                """,
                (int(snapshot_id),),
            ).fetchone()
        if row is None:
            return None
        d = self._daily_snapshot_row_to_dict(row)
        self._enrich_snapshot_pnl_fallbacks(d)
        return d

    def get_recent_daily_snapshots(self, limit: int = 100) -> list[dict[str, Any]]:
        """Most recent ``daily_snapshots`` rows (newest first)."""
        lim = max(1, min(int(limit), 500))
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT id, date, time, net_liquidation, unrealized_pnl, realized_pnl_today, ib_daily_pnl, positions_json
                FROM daily_snapshots
                ORDER BY time DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()
        out = []
        for row in rows:
            d = self._daily_snapshot_row_to_dict(row)
            self._enrich_snapshot_pnl_fallbacks(d)
            out.append(d)
        return out

    def get_recent_daily_snapshots_for_date(self, date_yyyy_mm_dd: str, limit: int = 100) -> list[dict[str, Any]]:
        """``daily_snapshots`` rows for a single calendar ``date`` (newest first)."""
        if not date_yyyy_mm_dd or len(date_yyyy_mm_dd) < 10:
            return []
        day = date_yyyy_mm_dd[:10]
        lim = max(1, min(int(limit), 500))
        with self._conn() as con:
            rows = con.execute(
                """
                SELECT id, date, time, net_liquidation, unrealized_pnl, realized_pnl_today, ib_daily_pnl, positions_json
                FROM daily_snapshots
                WHERE date = ?
                ORDER BY time DESC
                LIMIT ?
                """,
                (day, lim),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = self._daily_snapshot_row_to_dict(row)
            self._enrich_snapshot_pnl_fallbacks(d)
            out.append(d)
        return out

    def get_journal_exit_day_aggregate(self, date_yyyy_mm_dd: str) -> dict[str, Any]:
        """
        Account-wide totals from ``trades`` for rows **closed** on calendar day ``date_yyyy_mm_dd``
        (``exit_fill_time`` ISO date prefix). Compare to IB ``daily_snapshots`` P&amp;L when
        diagnosing dashboard mismatches.
        """
        with self._conn() as con:
            row = con.execute(
                """
                SELECT
                    COUNT(*) AS n_closed,
                    COALESCE(SUM(realized_pnl), 0) AS sum_realized,
                    SUM(CASE WHEN UPPER(TRIM(side)) = 'LONG' THEN 1 ELSE 0 END) AS n_closed_long,
                    SUM(CASE WHEN UPPER(TRIM(side)) = 'SHORT' THEN 1 ELSE 0 END) AS n_closed_short
                FROM trades
                WHERE LOWER(TRIM(status)) = 'closed'
                  AND exit_fill_time IS NOT NULL
                  AND length(trim(exit_fill_time)) >= 10
                  AND substr(trim(exit_fill_time), 1, 10) = ?
                """,
                (date_yyyy_mm_dd,),
            ).fetchone()
            open_n = con.execute(
                """
                SELECT COUNT(*) FROM trades WHERE LOWER(TRIM(status)) = 'open'
                """
            ).fetchone()
        n_closed = int(row["n_closed"] or 0) if row else 0
        return {
            "date": date_yyyy_mm_dd,
            "closed_trade_rows": n_closed,
            "closed_long": int(row["n_closed_long"] or 0) if row else 0,
            "closed_short": int(row["n_closed_short"] or 0) if row else 0,
            "sum_realized_pnl": float(row["sum_realized"] or 0.0) if row else 0.0,
            "open_trade_rows_total": int(open_n[0] or 0) if open_n else 0,
        }

    def get_activity_by_symbol(self, date_yyyy_mm_dd: str | None = None) -> list[dict[str, Any]]:
        """
        Per-ticker counts from ``trades``: open positions, closed trades, wins / losses / flat,
        and sum of realized P&amp;L on closed trades. Win = closed with ``realized_pnl &gt; 0``, etc.

        Each row also includes ``open_long`` / ``open_short`` and ``closed_long`` / ``closed_short``
        from the ``side`` column.

        ``opened`` / ``exited``: with a **day** filter, ``opened`` = rows whose ``entry_fill_time``
        falls on that calendar day; ``exited`` = rows closed that day (same count as ``closed`` in
        that mode). **All-time:** ``opened`` = total ``trades`` rows for the symbol; ``exited`` =
        lifetime closed count.

        If ``date_yyyy_mm_dd`` is provided (ISO YYYY-MM-DD), filters closed-trade stats to that
        calendar day (based on ``exit_fill_time``). Open positions are always current.
        """
        date_filter = ""
        params: list[str] = []
        opened_expr: str
        exited_expr: str
        closed_expr: str
        closed_long_expr: str
        closed_short_expr: str
        wins_expr: str
        losses_expr: str
        flat_expr: str
        total_realized_expr: str
        order_realized_expr: str
        if date_yyyy_mm_dd:
            date_filter = """
                  AND (
                      (LOWER(TRIM(status)) != 'closed')
                      OR (
                          exit_fill_time IS NOT NULL
                          AND length(trim(exit_fill_time)) >= 10
                          AND substr(trim(exit_fill_time), 1, 10) = ?
                      )
                  )
            """
            opened_expr = """
                    SUM(
                        CASE
                            WHEN entry_fill_time IS NOT NULL
                                 AND length(trim(entry_fill_time)) >= 10
                                 AND substr(trim(entry_fill_time), 1, 10) = ?
                            THEN 1 ELSE 0
                        END
                    ) AS opened_count,
            """
            exited_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                            THEN 1 ELSE 0
                        END
                    ) AS exited_count,
            """
            closed_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                            THEN 1 ELSE 0
                        END
                    ) AS closed,
            """
            closed_long_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND UPPER(TRIM(side)) = 'LONG'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                            THEN 1 ELSE 0
                        END
                    ) AS closed_long,
            """
            closed_short_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND UPPER(TRIM(side)) = 'SHORT'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                            THEN 1 ELSE 0
                        END
                    ) AS closed_short,
            """
            wins_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                                 AND realized_pnl IS NOT NULL
                                 AND realized_pnl > 0
                            THEN 1 ELSE 0
                        END
                    ) AS wins,
            """
            losses_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                                 AND realized_pnl IS NOT NULL
                                 AND realized_pnl < 0
                            THEN 1 ELSE 0
                        END
                    ) AS losses,
            """
            flat_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                                 AND (realized_pnl IS NULL OR realized_pnl = 0)
                            THEN 1 ELSE 0
                        END
                    ) AS flat,
            """
            total_realized_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                            THEN COALESCE(realized_pnl, 0)
                            ELSE 0
                        END
                    ) AS total_realized_pnl
            """
            order_realized_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND exit_fill_time IS NOT NULL
                                 AND length(trim(exit_fill_time)) >= 10
                                 AND substr(trim(exit_fill_time), 1, 10) = ?
                            THEN COALESCE(realized_pnl, 0)
                            ELSE 0
                        END
                    ) DESC,
            """
        else:
            opened_expr = "COUNT(*) AS opened_count,\n"
            exited_expr = """
                    SUM(CASE WHEN LOWER(TRIM(status)) = 'closed' THEN 1 ELSE 0 END) AS exited_count,
            """
            closed_expr = "SUM(CASE WHEN LOWER(TRIM(status)) = 'closed' THEN 1 ELSE 0 END) AS closed,\n"
            closed_long_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND UPPER(TRIM(side)) = 'LONG'
                            THEN 1 ELSE 0
                        END
                    ) AS closed_long,
            """
            closed_short_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND UPPER(TRIM(side)) = 'SHORT'
                            THEN 1 ELSE 0
                        END
                    ) AS closed_short,
            """
            wins_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND realized_pnl IS NOT NULL
                                 AND realized_pnl > 0
                            THEN 1 ELSE 0
                        END
                    ) AS wins,
            """
            losses_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND realized_pnl IS NOT NULL
                                 AND realized_pnl < 0
                            THEN 1 ELSE 0
                        END
                    ) AS losses,
            """
            flat_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                                 AND (realized_pnl IS NULL OR realized_pnl = 0)
                            THEN 1 ELSE 0
                        END
                    ) AS flat,
            """
            total_realized_expr = """
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'closed'
                            THEN COALESCE(realized_pnl, 0)
                            ELSE 0
                        END
                    ) AS total_realized_pnl
            """
            order_realized_expr = """
                    SUM(CASE WHEN LOWER(TRIM(status)) = 'closed' THEN COALESCE(realized_pnl, 0) ELSE 0 END) DESC,
            """

        if date_yyyy_mm_dd:
            having_clause = """
                HAVING SUM(CASE WHEN LOWER(TRIM(status)) = 'open' THEN 1 ELSE 0 END) > 0
                    OR SUM(CASE WHEN LOWER(TRIM(status)) = 'closed' THEN 1 ELSE 0 END) > 0
                    OR SUM(
                        CASE
                            WHEN entry_fill_time IS NOT NULL
                                 AND length(trim(entry_fill_time)) >= 10
                                 AND substr(trim(entry_fill_time), 1, 10) = ?
                            THEN 1 ELSE 0
                        END
                    ) > 0
            """
            # Placeholder order follows SQL text order: SELECT expressions first, then WHERE,
            # then HAVING, then ORDER BY.
            params = [
                date_yyyy_mm_dd,  # opened
                date_yyyy_mm_dd,  # exited
                date_yyyy_mm_dd,  # closed
                date_yyyy_mm_dd,  # closed_long
                date_yyyy_mm_dd,  # closed_short
                date_yyyy_mm_dd,  # wins
                date_yyyy_mm_dd,  # losses
                date_yyyy_mm_dd,  # flat
                date_yyyy_mm_dd,  # total_realized_pnl
                date_yyyy_mm_dd,  # WHERE date_filter
                date_yyyy_mm_dd,  # HAVING opened
                date_yyyy_mm_dd,  # ORDER BY realized
            ]
        else:
            having_clause = """
                HAVING SUM(CASE WHEN LOWER(TRIM(status)) = 'open' THEN 1 ELSE 0 END) > 0
                    OR SUM(CASE WHEN LOWER(TRIM(status)) = 'closed' THEN 1 ELSE 0 END) > 0
            """
            params = []

        with self._conn() as con:
            con.row_factory = sqlite3.Row
            query = f"""
                SELECT
                    UPPER(TRIM(symbol)) AS symbol,
                    SUM(CASE WHEN LOWER(TRIM(status)) = 'open' THEN 1 ELSE 0 END) AS open_count,
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'open'
                                 AND UPPER(TRIM(side)) = 'LONG'
                            THEN 1 ELSE 0
                        END
                    ) AS open_long,
                    SUM(
                        CASE
                            WHEN LOWER(TRIM(status)) = 'open'
                                 AND UPPER(TRIM(side)) = 'SHORT'
                            THEN 1 ELSE 0
                        END
                    ) AS open_short,
                    {opened_expr}
                    {exited_expr}
                    {closed_expr}
                    {closed_long_expr}
                    {closed_short_expr}
                    {wins_expr}
                    {losses_expr}
                    {flat_expr}
                    {total_realized_expr}
                FROM trades
                WHERE 1=1 {date_filter}
                GROUP BY UPPER(TRIM(symbol))
                {having_clause}
                ORDER BY
                    {order_realized_expr}
                    symbol ASC
                """
            rows = con.execute(query, params).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "symbol": r["symbol"],
                    "open": int(r["open_count"] or 0),
                    "open_long": int(r["open_long"] or 0),
                    "open_short": int(r["open_short"] or 0),
                    "opened": int(r["opened_count"] or 0),
                    "exited": int(r["exited_count"] or 0),
                    "closed": int(r["closed"] or 0),
                    "closed_long": int(r["closed_long"] or 0),
                    "closed_short": int(r["closed_short"] or 0),
                    "wins": int(r["wins"] or 0),
                    "losses": int(r["losses"] or 0),
                    "flat": int(r["flat"] or 0),
                    "total_realized_pnl": float(r["total_realized_pnl"] or 0.0),
                    "date_filter": date_yyyy_mm_dd or "all-time",
                }
            )
        return out

    @staticmethod
    def _compact_param_value(value: Any, max_str: int = 96, depth: int = 0) -> Any:
        """Shrink nested structures for dashboard / API payloads."""
        if depth > 3:
            return "…"
        if isinstance(value, dict):
            items = list(value.items())[:28]
            return {str(k): TradeJournal._compact_param_value(v, max_str, depth + 1) for k, v in items}
        if isinstance(value, (list, tuple)):
            seq = list(value)[:24]
            return [TradeJournal._compact_param_value(v, max_str, depth + 1) for v in seq]
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        if isinstance(value, str) and len(value) > max_str:
            return value[: max_str - 1] + "…"
        return value

    @staticmethod
    def _split_indicator_entry_exit(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Split ``indicator_snapshot_json`` into entry vs exit-ish keys (``exit_*`` prefix)."""
        entry: dict[str, Any] = {}
        exitish: dict[str, Any] = {}
        for k, v in snapshot.items():
            ks = str(k).lower()
            if ks.startswith("exit_"):
                exitish[str(k)] = v
            else:
                entry[str(k)] = v
        return entry, exitish

    def get_trades_touching_calendar_day(
        self, date_yyyy_mm_dd: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        """
        Rows from ``trades`` whose **entry** or **exit** fill timestamp falls on ``date_yyyy_mm_dd``
        (calendar prefix on ISO strings). Includes ``fills`` quantity sums and parsed indicator
        snapshot (entry params vs any ``exit_*`` keys stored in the same JSON).

        Sorted by symbol, then most recent exit or entry time.
        """
        day = date_yyyy_mm_dd.strip()[:10]
        if len(day) != 10:
            return []
        lim = max(1, min(int(limit), 800))
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT
                    t.trade_id,
                    t.run_id,
                    t.symbol,
                    t.side,
                    t.status,
                    t.signal_value,
                    t.intended_entry_price,
                    t.intended_shares,
                    t.stop_loss,
                    t.target,
                    t.entry_fill_price,
                    t.entry_fill_time,
                    t.exit_fill_price,
                    t.exit_fill_time,
                    t.exit_reason,
                    t.realized_pnl,
                    t.commission,
                    t.order_id,
                    t.indicator_snapshot_json,
                    (
                        SELECT COALESCE(SUM(f.qty), 0)
                        FROM fills f
                        WHERE f.trade_id = t.trade_id AND f.is_entry = 1
                    ) AS entry_qty_sum,
                    (
                        SELECT COALESCE(SUM(f.qty), 0)
                        FROM fills f
                        WHERE f.trade_id = t.trade_id AND f.is_entry = 0
                    ) AS exit_qty_sum
                FROM trades t
                WHERE
                    (
                        t.entry_fill_time IS NOT NULL
                        AND length(trim(t.entry_fill_time)) >= 10
                        AND substr(trim(t.entry_fill_time), 1, 10) = ?
                    )
                    OR (
                        t.exit_fill_time IS NOT NULL
                        AND length(trim(t.exit_fill_time)) >= 10
                        AND substr(trim(t.exit_fill_time), 1, 10) = ?
                    )
                ORDER BY UPPER(TRIM(t.symbol)) ASC,
                    COALESCE(t.exit_fill_time, t.entry_fill_time, t.created_at) DESC
                LIMIT ?
                """,
                (day, day, lim),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            raw_json = r["indicator_snapshot_json"] or "{}"
            try:
                snap = json.loads(raw_json) if isinstance(raw_json, str) else {}
            except json.JSONDecodeError:
                snap = {}
            if not isinstance(snap, dict):
                snap = {}
            entry_keys, exit_keys = self._split_indicator_entry_exit(snap)
            et = (r["entry_fill_time"] or "").strip()
            xt = (r["exit_fill_time"] or "").strip()
            entry_day = et[:10] if len(et) >= 10 else None
            exit_day = xt[:10] if len(xt) >= 10 else None
            touch_parts: list[str] = []
            if entry_day == day:
                touch_parts.append("entry")
            if exit_day == day:
                touch_parts.append("exit")
            out.append(
                {
                    "trade_id": r["trade_id"],
                    "run_id": r["run_id"],
                    "symbol": (r["symbol"] or "").strip().upper(),
                    "side": (r["side"] or "").strip().upper(),
                    "status": (r["status"] or "").strip().lower(),
                    "signal_value": int(r["signal_value"] or 0),
                    "intended_shares": int(r["intended_shares"] or 0),
                    "intended_entry_price": float(r["intended_entry_price"] or 0.0),
                    "stop_loss": float(r["stop_loss"]) if r["stop_loss"] is not None else None,
                    "target": float(r["target"]) if r["target"] is not None else None,
                    "entry_fill_qty": float(r["entry_qty_sum"] or 0.0),
                    "exit_fill_qty": float(r["exit_qty_sum"] or 0.0),
                    "entry_fill_price": float(r["entry_fill_price"]) if r["entry_fill_price"] is not None else None,
                    "entry_fill_time": r["entry_fill_time"],
                    "exit_fill_price": float(r["exit_fill_price"]) if r["exit_fill_price"] is not None else None,
                    "exit_fill_time": r["exit_fill_time"],
                    "exit_reason": r["exit_reason"],
                    "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else None,
                    "commission": float(r["commission"]) if r["commission"] is not None else None,
                    "order_id": int(r["order_id"]) if r["order_id"] is not None else None,
                    "entry_calendar_day": entry_day,
                    "exit_calendar_day": exit_day,
                    "touch": "+".join(touch_parts) if touch_parts else "",
                    "entry_params": self._compact_param_value(entry_keys),
                    "exit_params_from_snapshot": self._compact_param_value(exit_keys),
                }
            )
        return out

    def get_trade_full(self, trade_id: str) -> dict[str, Any] | None:
        """
        Return one trade with **un-compacted** ``entry_snapshot`` / ``exit_snapshot`` (full
        ``indicator_snapshot_json`` split) plus fills, for review / API (e.g. journal dashboard hover).
        """
        tid = (trade_id or "").strip()
        if not tid:
            return None
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            r = con.execute(
                """
                SELECT
                    t.trade_id,
                    t.run_id,
                    t.symbol,
                    t.side,
                    t.status,
                    t.signal_value,
                    t.intended_entry_price,
                    t.intended_shares,
                    t.stop_loss,
                    t.target,
                    t.entry_fill_price,
                    t.entry_fill_time,
                    t.exit_fill_price,
                    t.exit_fill_time,
                    t.exit_reason,
                    t.realized_pnl,
                    t.commission,
                    t.order_id,
                    t.stop_order_id,
                    t.target_order_id,
                    t.error_message,
                    t.indicator_snapshot_json,
                    t.memory_consulted,
                    t.memory_verdict_json,
                    t.created_at,
                    t.updated_at,
                    (
                        SELECT COALESCE(SUM(f.qty), 0)
                        FROM fills f
                        WHERE f.trade_id = t.trade_id AND f.is_entry = 1
                    ) AS entry_qty_sum,
                    (
                        SELECT COALESCE(SUM(f.qty), 0)
                        FROM fills f
                        WHERE f.trade_id = t.trade_id AND f.is_entry = 0
                    ) AS exit_qty_sum
                FROM trades t
                WHERE t.trade_id = ?
                """,
                (tid,),
            ).fetchone()
        if r is None:
            return None
        raw_json = r["indicator_snapshot_json"] or "{}"
        try:
            snap = json.loads(raw_json) if isinstance(raw_json, str) else {}
        except json.JSONDecodeError:
            snap = {}
        if not isinstance(snap, dict):
            snap = {}
        entry_keys, exit_keys = self._split_indicator_entry_exit(snap)
        mem_parsed: Any = None
        mraw = r["memory_verdict_json"]
        if mraw:
            try:
                mem_parsed = json.loads(mraw) if isinstance(mraw, str) else mraw
            except json.JSONDecodeError:
                mem_parsed = {"_raw": str(mraw)}
        fills_out: list[dict[str, Any]] = []
        with self._conn() as con:
            con.row_factory = sqlite3.Row
            for fr in con.execute(
                """
                SELECT fill_id, order_id, exec_id, symbol, side, qty, price, commission, time, is_entry
                FROM fills
                WHERE trade_id = ?
                ORDER BY time ASC
                """,
                (tid,),
            ).fetchall():
                fills_out.append(
                    {
                        "fill_id": fr["fill_id"],
                        "order_id": fr["order_id"],
                        "exec_id": fr["exec_id"],
                        "symbol": fr["symbol"],
                        "side": fr["side"],
                        "qty": float(fr["qty"] or 0.0),
                        "price": float(fr["price"] or 0.0),
                        "commission": fr["commission"],
                        "time": fr["time"],
                        "is_entry": bool(int(fr["is_entry"] or 0)),
                    }
                )
        return {
            "trade_id": r["trade_id"],
            "run_id": r["run_id"],
            "symbol": (r["symbol"] or "").strip().upper(),
            "side": (r["side"] or "").strip().upper(),
            "status": (r["status"] or "").strip().lower(),
            "signal_value": int(r["signal_value"] or 0),
            "intended_shares": int(r["intended_shares"] or 0),
            "intended_entry_price": float(r["intended_entry_price"] or 0.0),
            "stop_loss": float(r["stop_loss"]) if r["stop_loss"] is not None else None,
            "target": float(r["target"]) if r["target"] is not None else None,
            "order_id": int(r["order_id"]) if r["order_id"] is not None else None,
            "stop_order_id": int(r["stop_order_id"]) if r["stop_order_id"] is not None else None,
            "target_order_id": int(r["target_order_id"]) if r["target_order_id"] is not None else None,
            "entry_fill_price": float(r["entry_fill_price"]) if r["entry_fill_price"] is not None else None,
            "entry_fill_time": r["entry_fill_time"],
            "exit_fill_price": float(r["exit_fill_price"]) if r["exit_fill_price"] is not None else None,
            "exit_fill_time": r["exit_fill_time"],
            "exit_fill_qty": float(r["exit_qty_sum"] or 0.0),
            "entry_fill_qty": float(r["entry_qty_sum"] or 0.0),
            "exit_reason": r["exit_reason"],
            "realized_pnl": float(r["realized_pnl"]) if r["realized_pnl"] is not None else None,
            "commission": float(r["commission"]) if r["commission"] is not None else None,
            "error_message": r["error_message"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "memory_consulted": bool(int(r["memory_consulted"] or 0)),
            "memory_verdict": mem_parsed,
            "entry_snapshot": entry_keys,
            "exit_snapshot": exit_keys,
            "fills": fills_out,
        }

    def purge_journal_before_calendar_date(
        self, first_day_to_keep: str, *, strict_trade_time: bool = False
    ) -> dict[str, int]:
        """
        Remove misleading history **strictly before** ``first_day_to_keep`` (``YYYY-MM-DD``).

        Deletes ``daily_snapshots`` with ``date`` before that day; ``runs`` whose ``started_at``
        date is before that day (and dependent ``signals``, ``trades``, ``fills``, ``orders``,
        ``ib_executions`` rows); ``market_regimes`` and ``execution_quality`` rows dated before
        that day; and any remaining ``ib_executions`` with fill ``time`` before that day.

        Rows on or after ``first_day_to_keep`` are kept.

        When ``strict_trade_time`` is True, an additional pass deletes trade lifecycles where
        the trade's own timestamps are before the boundary even if their run date does not
        satisfy the run-based deletion criteria.
        """
        day = first_day_to_keep.strip()[:10]
        if len(day) != 10 or day[4] != "-" or day[7] != "-":
            raise ValueError(f"first_day_to_keep must be YYYY-MM-DD, got {first_day_to_keep!r}")
        stats: dict[str, int] = {}
        with self._conn() as con:
            cur = con.execute("DELETE FROM daily_snapshots WHERE date < ?", (day,))
            stats["daily_snapshots_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM market_regimes
                WHERE length(trim(timestamp)) >= 10
                  AND substr(trim(timestamp), 1, 10) < ?
                """,
                (day,),
            )
            stats["market_regimes_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM execution_quality
                WHERE length(trim(updated_at)) >= 10
                  AND substr(trim(updated_at), 1, 10) < ?
                """,
                (day,),
            )
            stats["execution_quality_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM fills WHERE trade_id IN (
                    SELECT trade_id FROM trades WHERE run_id IN (
                        SELECT run_id FROM runs
                        WHERE length(trim(started_at)) >= 10
                          AND substr(trim(started_at), 1, 10) < ?
                    )
                )
                """,
                (day,),
            )
            stats["fills_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM orders WHERE trade_id IN (
                    SELECT trade_id FROM trades WHERE run_id IN (
                        SELECT run_id FROM runs
                        WHERE length(trim(started_at)) >= 10
                          AND substr(trim(started_at), 1, 10) < ?
                    )
                )
                """,
                (day,),
            )
            stats["orders_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM ib_executions WHERE trade_id IN (
                    SELECT trade_id FROM trades WHERE run_id IN (
                        SELECT run_id FROM runs
                        WHERE length(trim(started_at)) >= 10
                          AND substr(trim(started_at), 1, 10) < ?
                    )
                )
                """,
                (day,),
            )
            stats["ib_executions_deleted_for_old_trades"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM trades WHERE run_id IN (
                    SELECT run_id FROM runs
                    WHERE length(trim(started_at)) >= 10
                      AND substr(trim(started_at), 1, 10) < ?
                )
                """,
                (day,),
            )
            stats["trades_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM signals WHERE run_id IN (
                    SELECT run_id FROM runs
                    WHERE length(trim(started_at)) >= 10
                      AND substr(trim(started_at), 1, 10) < ?
                )
                """,
                (day,),
            )
            stats["signals_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM runs
                WHERE length(trim(started_at)) >= 10
                  AND substr(trim(started_at), 1, 10) < ?
                """,
                (day,),
            )
            stats["runs_deleted"] = cur.rowcount

            cur = con.execute(
                """
                DELETE FROM ib_executions
                WHERE length(trim(time)) >= 10
                  AND substr(trim(time), 1, 10) < ?
                """,
                (day,),
            )
            stats["ib_executions_deleted_by_fill_time"] = cur.rowcount

            if strict_trade_time:
                cur = con.execute(
                    """
                    DELETE FROM fills
                    WHERE trade_id IN (
                        SELECT trade_id
                        FROM trades
                        WHERE length(trim(COALESCE(exit_fill_time, entry_fill_time, created_at))) >= 10
                          AND substr(trim(COALESCE(exit_fill_time, entry_fill_time, created_at)), 1, 10) < ?
                    )
                    """,
                    (day,),
                )
                stats["strict_fills_deleted_by_trade_time"] = cur.rowcount

                cur = con.execute(
                    """
                    DELETE FROM orders
                    WHERE trade_id IN (
                        SELECT trade_id
                        FROM trades
                        WHERE length(trim(COALESCE(exit_fill_time, entry_fill_time, created_at))) >= 10
                          AND substr(trim(COALESCE(exit_fill_time, entry_fill_time, created_at)), 1, 10) < ?
                    )
                    """,
                    (day,),
                )
                stats["strict_orders_deleted_by_trade_time"] = cur.rowcount

                cur = con.execute(
                    """
                    DELETE FROM ib_executions
                    WHERE trade_id IN (
                        SELECT trade_id
                        FROM trades
                        WHERE length(trim(COALESCE(exit_fill_time, entry_fill_time, created_at))) >= 10
                          AND substr(trim(COALESCE(exit_fill_time, entry_fill_time, created_at)), 1, 10) < ?
                    )
                    """,
                    (day,),
                )
                stats["strict_ib_exec_deleted_by_trade_time"] = cur.rowcount

                cur = con.execute(
                    """
                    DELETE FROM trades
                    WHERE length(trim(COALESCE(exit_fill_time, entry_fill_time, created_at))) >= 10
                      AND substr(trim(COALESCE(exit_fill_time, entry_fill_time, created_at)), 1, 10) < ?
                    """,
                    (day,),
                )
                stats["strict_trades_deleted_by_trade_time"] = cur.rowcount

                cur = con.execute("DELETE FROM fills WHERE trade_id NOT IN (SELECT trade_id FROM trades)")
                stats["strict_fills_orphan_deleted"] = cur.rowcount
                cur = con.execute("DELETE FROM orders WHERE trade_id NOT IN (SELECT trade_id FROM trades)")
                stats["strict_orders_orphan_deleted"] = cur.rowcount
                cur = con.execute("DELETE FROM ib_executions WHERE trade_id NOT IN (SELECT trade_id FROM trades)")
                stats["strict_ib_exec_orphan_deleted"] = cur.rowcount

        return stats

    def get_available_dates(self) -> list[str]:
        """Return distinct calendar dates (YYYY-MM-DD) from daily_snapshots and trades, newest first."""
        with self._conn() as con:
            rows = con.execute("""
                SELECT DISTINCT date
                FROM (
                    SELECT date FROM daily_snapshots
                    UNION
                    SELECT substr(trim(exit_fill_time), 1, 10) as date FROM trades
                        WHERE exit_fill_time IS NOT NULL AND length(trim(exit_fill_time)) >= 10
                    UNION
                    SELECT substr(trim(entry_fill_time), 1, 10) as date FROM trades
                        WHERE entry_fill_time IS NOT NULL AND length(trim(entry_fill_time)) >= 10
                ) dates
                WHERE date IS NOT NULL AND date != ''
                ORDER BY date DESC
                LIMIT 90
            """).fetchall()
        return [row[0] for row in rows if row[0]]
