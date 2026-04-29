"""
Minimal FastAPI server for zone + journal charts.

Run from ``pythonclient`` (uses project venv via ``./py``, same as other tools):

  ./scripts/run_zone_validation_server.sh

Or:

  PYTHONPATH=src ./py -m uvicorn zone_validation_app.main:app --reload --host 127.0.0.1 --port 8766

Use a free port (e.g. 8766) if ``journal_dashboard`` is on 8765. Do not use Homebrew's
``uvicorn`` — it runs a different Python without ``pandas`` / project deps.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from trade_journal import TradeJournal

from zone_validation.data import list_matching_cache_csvs
from zone_validation.service import (
    ZoneChartError,
    groq_strategy_kwargs_from_run_params,
    parse_strategy_params_json,
    render_zone_trade_chart,
    resolve_csv_under_data_dir,
)

APP_DIR = Path(__file__).resolve().parent


def _pythonclient_root() -> Path:
    return Path(__file__).resolve().parents[2]


app = FastAPI(title="Zone validation", version="0.1.0")

app.mount(
    "/static",
    StaticFiles(directory=APP_DIR / "static"),
    name="static",
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/cache-files")
def api_cache_files(
    symbol: str = Query(..., min_length=1, max_length=16),
    bar_size: str = Query("15 mins"),
) -> dict[str, object]:
    """List ``historical_data`` CSVs for symbol + bar size (newest first)."""
    root = _pythonclient_root()
    data_dir = root / "historical_data"
    files = list_matching_cache_csvs(data_dir, symbol.strip().upper(), bar_size)
    return {
        "symbol": symbol.strip().upper(),
        "bar_size": bar_size,
        "data_dir": str(data_dir),
        "files": files,
    }


@app.get("/api/runs")
def api_runs(limit: int = Query(30, ge=1, le=200)) -> dict[str, object]:
    """Recent ``trade_journal.runs`` rows (newest first) for run_id autofill."""
    root = _pythonclient_root()
    jpath = root / "trade_journal.db"
    if not jpath.is_file():
        return {"runs": [], "journal_db": str(jpath), "note": "journal database not found"}
    journal = TradeJournal(jpath)
    return {"runs": journal.list_recent_runs(limit=limit), "journal_db": str(jpath)}


@app.get("/api/run-params")
def api_run_params(
    run_id: str = Query(..., min_length=1, description="Journal run_id (runs.params_json)"),
) -> JSONResponse:
    """Return Groq strategy kwargs derived from ``trade_journal.runs`` (for textarea prefill)."""
    root = _pythonclient_root()
    jpath = root / "trade_journal.db"
    if not jpath.is_file():
        raise HTTPException(status_code=400, detail=f"Journal DB not found: {jpath}")
    journal = TradeJournal(jpath)
    raw = journal.get_run_params(run_id.strip())
    if raw is None:
        raise HTTPException(status_code=404, detail=f"Unknown run_id: {run_id!r}")
    groq = groq_strategy_kwargs_from_run_params(raw)
    return JSONResponse(
        content={"run_id": run_id.strip(), "groq_params": groq},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/chart")
def api_chart(
    symbol: str = Query(..., min_length=1, max_length=16),
    day: str = Query(..., description="Session calendar day YYYY-MM-DD (ET bar index)"),
    bar_size: str = Query("15 mins"),
    no_journal: bool = Query(False),
    run_id: str | None = Query(
        None,
        description="Journal run: load default Groq kwargs from runs.params_json (JSON overrides on top)",
    ),
    csv_file: str | None = Query(
        None,
        description="Optional cache filename only (must live under historical_data/)",
    ),
    strategy_params_json: str | None = Query(
        None,
        description="Optional JSON object of GroqRecommendationsStrategy kwargs",
    ),
) -> JSONResponse:
    root = _pythonclient_root()
    data_dir = root / "historical_data"

    csv_path = None
    if csv_file:
        try:
            csv_path = resolve_csv_under_data_dir(data_dir, csv_file)
        except ZoneChartError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        strat = parse_strategy_params_json(strategy_params_json)
        fig, meta = render_zone_trade_chart(
            symbol=symbol,
            day_yyyy_mm_dd=day,
            pythonclient_root=root,
            data_dir=data_dir,
            csv_path=csv_path,
            bar_size=bar_size,
            journal_db=root / "trade_journal.db",
            no_journal=no_journal,
            run_id=run_id,
            strategy_params=strat,
        )
    except ZoneChartError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    fig_dict = json.loads(fig.to_json())
    return JSONResponse(
        content={"figure": fig_dict, "meta": meta},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html", media_type="text/html; charset=utf-8")
