#!/usr/bin/env bash
# Run minimal zone-validation FastAPI app from pythonclient/.
cd "$(dirname "$0")/.." || exit 1
export PYTHONPATH="${PWD}/src${PYTHONPATH:+:$PYTHONPATH}"
exec uvicorn zone_validation_app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}" "$@"
