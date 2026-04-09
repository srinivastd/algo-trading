"""
Persistent trade memory (Phase 2): auto-recall and auto-capture.

This module stores closed-trade outcomes in a dedicated ChromaDB collection and
supports pre-trade similarity recall for log-only feedback.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


@dataclass
class RecallResult:
    similar_trades: list[dict[str, Any]]
    win_rate_similar: float | None
    avg_pnl_similar: float | None
    confidence_adjustment: float
    regime_warning: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "similar_trades": self.similar_trades,
            "win_rate_similar": self.win_rate_similar,
            "avg_pnl_similar": self.avg_pnl_similar,
            "confidence_adjustment": self.confidence_adjustment,
            "regime_warning": self.regime_warning,
        }


class TradeMemory:
    """
    Chroma-backed memory for closed trades.

    - `capture_closed_trade`: auto-capture on trade close
    - `pre_trade_recall`: auto-recall before entry (log-only recommendations)
    """

    def __init__(
        self,
        db_path: Path,
        *,
        collection_name: str = "trade_decisions",
    ):
        self.db_path = Path(db_path)
        self.collection_name = collection_name
        self._collection = None
        self._init_error: str | None = None
        self._init_collection()

    @property
    def enabled(self) -> bool:
        return self._collection is not None

    @property
    def init_error(self) -> str | None:
        return self._init_error

    def _init_collection(self) -> None:
        try:
            import chromadb
            from chromadb.utils import embedding_functions

            client = chromadb.PersistentClient(path=str(self.db_path))
            self._collection = client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(
                    model_name="all-MiniLM-L6-v2"
                ),
            )
        except Exception as e:
            self._init_error = str(e)
            self._collection = None

    @staticmethod
    def _to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _build_query_text(
        *,
        symbol: str,
        strategy: str,
        signal_value: int,
        entry_price: float | None,
        indicators: Mapping[str, Any],
    ) -> str:
        regime = "unknown"
        adx = TradeMemory._to_float(indicators.get("adx"))
        if adx is not None and adx >= 30:
            regime = "trending"
        elif adx is not None and adx <= 20:
            regime = "ranging"

        rsi = TradeMemory._to_float(indicators.get("rsi"))
        bb = TradeMemory._to_float(indicators.get("bb_position"))
        vol_ratio = TradeMemory._to_float(indicators.get("vol_ratio"))

        return (
            f"{strategy} {symbol.upper()} signal={signal_value} entry={entry_price} "
            f"regime={regime} adx={adx} rsi={rsi} bb_position={bb} vol_ratio={vol_ratio}"
        )

    def pre_trade_recall(
        self,
        *,
        symbol: str,
        strategy: str,
        signal_value: int,
        entry_price: float | None,
        indicators: Mapping[str, Any],
        top_k: int = 10,
    ) -> RecallResult:
        if not self.enabled:
            return RecallResult([], None, None, 0.0, None)

        query = self._build_query_text(
            symbol=symbol,
            strategy=strategy,
            signal_value=signal_value,
            entry_price=entry_price,
            indicators=indicators,
        )

        try:
            resp = self._collection.query(
                query_texts=[query],
                n_results=top_k,
                where={"symbol": symbol.upper()},
            )
        except Exception:
            return RecallResult([], None, None, 0.0, None)

        metadatas = (resp.get("metadatas") or [[]])[0]
        distances = (resp.get("distances") or [[]])[0]
        if not metadatas:
            return RecallResult([], None, None, 0.0, None)

        rows: list[dict[str, Any]] = []
        wins = 0
        pnl_sum = 0.0
        pnl_count = 0
        trending_losses = 0
        for i, meta in enumerate(metadatas):
            if not meta:
                continue
            realized = self._to_float(meta.get("realized_pnl"))
            if realized is not None:
                pnl_sum += realized
                pnl_count += 1
                if realized > 0:
                    wins += 1
            if str(meta.get("entry_regime", "")).lower() == "trending" and (realized or 0) < 0:
                trending_losses += 1
            rows.append(
                {
                    "trade_id": meta.get("trade_id"),
                    "realized_pnl": realized,
                    "entry_regime": meta.get("entry_regime"),
                    "distance": distances[i] if i < len(distances) else None,
                    "exit_reason": meta.get("exit_reason"),
                }
            )

        win_rate = (wins / pnl_count) if pnl_count else None
        avg_pnl = (pnl_sum / pnl_count) if pnl_count else None
        confidence_adjustment = 0.0
        if win_rate is not None:
            if win_rate >= 0.65:
                confidence_adjustment = 0.1
            elif win_rate <= 0.35:
                confidence_adjustment = -0.1

        regime_warning = None
        if trending_losses >= 3:
            regime_warning = "Several similar trades lost in trending regime."

        return RecallResult(
            similar_trades=rows,
            win_rate_similar=win_rate,
            avg_pnl_similar=avg_pnl,
            confidence_adjustment=confidence_adjustment,
            regime_warning=regime_warning,
        )

    def capture_closed_trade(
        self,
        *,
        trade_id: str,
        symbol: str,
        strategy: str,
        side: str,
        signal_value: int,
        entry_price: float | None,
        exit_price: float | None,
        realized_pnl: float | None,
        shares: float | None,
        entry_time: Any,
        exit_time: Any,
        entry_regime: str | None,
        exit_reason: str | None,
        indicators: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return

        indicators = indicators or {}
        doc = (
            f"{strategy} {symbol.upper()} {side} signal={signal_value} "
            f"entry={entry_price} exit={exit_price} pnl={realized_pnl} shares={shares} "
            f"entry_regime={entry_regime} exit_reason={exit_reason} "
            f"rsi={self._to_float(indicators.get('rsi'))} adx={self._to_float(indicators.get('adx'))} "
            f"bb_position={self._to_float(indicators.get('bb_position'))} "
            f"vol_ratio={self._to_float(indicators.get('vol_ratio'))}"
        )

        eid = f"{trade_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        meta = {
            "trade_id": str(trade_id),
            "symbol": symbol.upper(),
            "strategy": strategy,
            "side": side,
            "signal_value": int(signal_value),
            "entry_price": self._to_float(entry_price),
            "exit_price": self._to_float(exit_price),
            "realized_pnl": self._to_float(realized_pnl),
            "shares": self._to_float(shares),
            "entry_time": str(entry_time),
            "exit_time": str(exit_time),
            "entry_regime": entry_regime or "unknown",
            "exit_reason": exit_reason or "unknown",
            "rsi": self._to_float(indicators.get("rsi")),
            "adx": self._to_float(indicators.get("adx")),
            "bb_position": self._to_float(indicators.get("bb_position")),
            "vol_ratio": self._to_float(indicators.get("vol_ratio")),
        }

        try:
            self._collection.add(
                ids=[eid],
                documents=[doc],
                metadatas=[meta],
            )
        except Exception:
            # Best-effort path: memory capture should not block trade processing.
            return
