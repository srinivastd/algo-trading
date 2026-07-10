#!/usr/bin/env bash
# Paper trading — Jun22 entries/exits with shorts ENABLED (paper default).
# Launch: ./run_paper_shorts.sh
# Attach: screen -r papershorts
# Stop:   screen -S papershorts -X quit
# Long-only: add --disable-shorts to the python args below.

set -euo pipefail
cd "$(dirname "$0")"

SYMS=$(grep -v '^#' watchlist.txt | grep -v '^[[:space:]]*$' | tr '\n' ' ')
LOG="logs/paper_shorts_$(date +%Y%m%d_%H%M%S).log"
mkdir -p logs

if screen -list | grep -q '\.papershorts'; then
  echo "Session papershorts already running. Use: screen -r papershorts"
  exit 1
fi

screen -dmS papershorts bash -c "
  cd '$(pwd)' &&
  PYTHONPATH=src ./py src/main.py \\
    --strategy groq --paper --watch \\
    --bar-size '1 hour' --duration '3 Y' \\
    --zone-lookback 20 --risk-per-trade 2000.0 \\
    --stop-loss 1.1 --target 4.8 \\
    --max-duration 12.0 --adx-min 28.5 --check-interval 60 \\
    --zone-freshness-hours 8 \\
    $SYMS \\
    2>&1 | tee '$LOG'
"

echo "Started papershorts (Jun22 params, longs+shorts) → $LOG"
echo "Attach: screen -r papershorts"
