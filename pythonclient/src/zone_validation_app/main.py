"""
Minimal FastAPI server for zone + journal charts.

Run (from ``pythonclient`` with deps installed):
  PYTHONPATH=src uvicorn zone_validation_app.main:app --reload --host 127.0.0.1 --port 8765

Open http://127.0.0.1:8765/
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from zone_validation.service import (
    ZoneChartError,
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


@app.get("/api/chart")
def api_chart(
    symbol: str = Query(..., min_length=1, max_length=16),
    day: str = Query(..., description="Session calendar day YYYY-MM-DD (ET bar index)"),
    bar_size: str = Query("15 mins"),
    no_journal: bool = Query(False),
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
