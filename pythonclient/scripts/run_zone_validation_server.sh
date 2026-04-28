#!/usr/bin/env bash
# Run minimal zone-validation FastAPI app from pythonclient/.
# Uses ./py (.bt_run_venv) so deps match journal_dashboard/main — NOT brew uvicorn.
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
exec ./py -m uvicorn zone_validation_app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}" "$@"
