# Algo-Trading Autoresearch Program

**Goal:** Systematically find which stocks and parameter combinations produce
the best risk-adjusted returns under the current Groq supply/demand strategy.

## Context

- **Strategy file:** `src/strategies/groq_recommendations_strategy.py`
  (supply/demand zone breakout with stop-loss / target brackets).
- **Backtest entry point:** `src/main.py` (one symbol at a time, `--backtest-only`).
- **Batch runner:** `src/batch_backtest.py` (loops symbols, writes CSV + JSON,
  ranks Chroma, sweeps parameters).
- **Results DB:** ChromaDB at `src/chroma_db/` (`backtest_results` collection).
- **Ranking / analysis scripts:** `src/query_chroma_best_returns.py`,
  `src/query_chroma_recent_trades.py`.

## How to run experiments

All commands assume `cwd = pythonclient/` and use `.bt_run_venv`.

### 1. Single backtest (baseline)

```bash
./.bt_run_venv/bin/python3 src/main.py QBTS --backtest-only --strategy groq \
  --duration "1 Y" --bar-size "4 hours" \
  --risk-per-trade 1000 --max-duration 72 --stop-loss 0.75 --target 6.0 \
  --initial-capital 50000
```

### 2. Batch backtest a watchlist

```bash
./.bt_run_venv/bin/python3 src/batch_backtest.py \
  -f watchlist.txt --continue-on-error \
  --strategy groq --duration "1 Y" --bar-size "4 hours" \
  --risk-per-trade 1000 --max-duration 72 --stop-loss 0.75 --target 6.0 \
  --initial-capital 50000 -o runs/batch.csv --json
```

### 3. Parameter sweep (autoresearch-style)

```bash
./.bt_run_venv/bin/python3 src/batch_backtest.py QBTS --sweep \
  --sweep-stop-loss 0.5,0.75,1.0 --sweep-target 4.0,6.0,8.0 \
  --sweep-max-duration 24,48,72 \
  --strategy groq --duration "1 Y" --bar-size "4 hours" \
  --initial-capital 50000 -o runs/sweep_qbts.csv --json --continue-on-error
```

### 4. Rank existing results

```bash
./.bt_run_venv/bin/python3 src/batch_backtest.py --rank --strategy groq --top 20
./.bt_run_venv/bin/python3 src/batch_backtest.py --rank --strategy groq --rank-symbol QBTS --json
```

## Experiment loop (for an AI agent)

1. **Pick a hypothesis.** Examples:
   - "Does tightening stop-loss from 0.75% to 0.5% improve profit factor on QBTS?"
   - "Which of these 10 tickers has the best return under the current params?"
   - "Does bar size 1h outperform 4h for AAPL?"

2. **Run the experiment.** Use `batch_backtest.py` (single run, batch, or sweep).

3. **Read results.** Check the CSV/JSON output or run `--rank`.

4. **Decide: keep or discard.**
   - If the new params beat the baseline on **return AND profit factor**
     (and total trades > 10), record the new best and update this file.
   - Otherwise discard and try the next hypothesis.

5. **Repeat.** Move to the next hypothesis or symbol.

## Current best known params (update as experiments improve)

| Symbol | SL%  | Tgt% | MaxDur(h) | Risk | Bar   | Duration | Return% | PF   | Trades |
|--------|------|------|-----------|------|-------|----------|---------|------|--------|
| QBTS   | 0.75 | 6.0  | 72        | 1000 | 4 hrs | 1 Y      | ~85%    | ~2.7 | 63     |

## Tunable parameters

| Parameter        | CLI flag            | Range to explore        |
|-----------------|---------------------|------------------------|
| stop_loss_pct   | --stop-loss         | 0.25 – 2.0             |
| target_pct      | --target            | 2.0 – 10.0             |
| max_duration_h  | --max-duration      | 12 – 168               |
| risk_per_trade  | --risk-per-trade    | 500 – 2000             |
| bar_size        | --bar-size          | "1 hour", "4 hours"    |
| duration        | --duration          | "6 M", "1 Y", "2 Y"   |

## Constraints

- IBKR Gateway must be running on port 4002 for backtest data.
- Each `main.py` call takes ~30–90s (fetches history from IBKR, 10s pacing between chunks).
- A 27-combo sweep (3×3×3) on one symbol ≈ 15–40 min.
- Don't modify `prepare.py`-equivalent files (`main.py` args, `historical_data.py`)
  — only strategy params, not the plumbing.
