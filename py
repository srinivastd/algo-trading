#!/usr/bin/env sh
# Run project Python from repo root (venv + deps live under pythonclient/).
# Examples:
#   ./py src/main.py QBTS --backtest-only --strategy groq --duration "6 M" --bar-size "15 mins"
#   ./py autoresearch.py QBTS --iterations 2
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
cd "$ROOT/pythonclient" || exit 1
exec ./py "$@"
