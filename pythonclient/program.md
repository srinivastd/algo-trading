# Algo-Trading Autoresearch Program

**Goal:** Systematically find which stocks and parameter combinations produce
the best risk-adjusted returns under the current Groq supply/demand strategy.

## Architecture

```
baseline.json                ← current best params per symbol
    ↓
autoresearch.py              ← orchestrator loop (N iterations)
    ├─ batch_backtest.py --sweep   ← runs main.py many times, writes to Chroma
    ├─ analyze.py                  ← composite scoring, sensitivity, baseline update
    └─ next_experiment.json        ← proposed perturbations for next iteration
```

## Files

| File | Purpose |
|------|---------|
| `baseline.json` | Current best params per symbol + composite score |
| `next_experiment.json` | Auto-generated: which param to perturb next |
| `autoresearch.py` | Orchestrator: sweep → analyze → update → repeat |
| `src/batch_backtest.py` | Batch runner: loops symbols, param grid, CSV + JSON |
| `src/analyze.py` | Composite ranking, sensitivity report, baseline update |
| `src/main.py` | Single-symbol backtest entry point |
| `src/strategies/groq_recommendations_strategy.py` | The strategy being tuned |
| `watchlist.txt` | Default symbol universe |

## Tunable parameters (all exposed as CLI flags)

| Parameter | CLI flag | Default | Sweep range |
|-----------|----------|---------|-------------|
| stop_loss_pct | --stop-loss | 0.75 | 0.25 – 3.0 |
| target_pct | --target | 6.0 | 2.0 – 12.0 |
| max_duration_hours | --max-duration | 72 | 6 – 168 |
| risk_per_trade | --risk-per-trade | 1000 | 500 – 3000 |
| zone_lookback | --zone-lookback | 4 | 2 – 8 |
| adx_min | --adx-min | 15 | 10 – 30 |
| zone_freshness_hours | --zone-freshness-hours | 12 | 6 – 48 |
| volume_breakout_mult | --volume-breakout-mult | 1.1 | 1.0 – 1.5 |
| volume_confirm_mult | --volume-confirm-mult | 1.05 | 1.0 – 1.3 |
| position_size_cap | --position-size-cap | 0.95 | 0.5 – 0.95 |
| bar_size | --bar-size | "4 hours" | "1 hour", "4 hours" |
| duration | --duration | "1 Y" | "6 M", "1 Y", "2 Y" |

## Quick start

All commands assume `cwd = pythonclient/` and use the project venv.

### 1. Run autoresearch (recommended)

```bash
./py autoresearch.py --symbol QBTS --iterations 5 --strategy groq
```

This runs 5 rounds of sweep → analyze → update-baseline automatically.

### 2. Manual single backtest

```bash
./py src/main.py QBTS --backtest-only --strategy groq \
  --duration "1 Y" --bar-size "4 hours" \
  --risk-per-trade 1000 --max-duration 72 --stop-loss 0.75 --target 6.0 \
  --initial-capital 50000
```

### 3. Manual parameter sweep

```bash
./py src/batch_backtest.py QBTS --sweep \
  --sweep-stop-loss 0.5,0.75,1.0 --sweep-target 4.0,6.0,8.0 \
  --sweep-max-duration 24,48,72 --sweep-zone-lookback 3,4,5 \
  --strategy groq --duration "1 Y" --bar-size "4 hours" \
  --initial-capital 50000 -o runs/sweep_qbts.csv --json --continue-on-error
```

### 4. Analyze + rank with composite score

```bash
./py src/analyze.py --strategy groq --symbol QBTS \
  --sensitivity --next-experiment --update-baseline
```

### 5. Rank all Chroma results (quick view)

```bash
./py src/batch_backtest.py --rank --strategy groq --top 20
```

## Autoresearch loop logic

Each iteration:

1. **Load params:** Read `next_experiment.json` (if exists) or `baseline.json`.
2. **Build sweep:** Take best params and perturb the most sensitive parameter
   (± 1-2 steps based on `PARAM_RANGES` in `analyze.py`).
3. **Run sweep:** `batch_backtest.py --sweep` → writes results to ChromaDB.
4. **Analyze:** `analyze.py --update-baseline --next-experiment --sensitivity`
   - Composite score = 0.30×Return + 0.25×PF + 0.20×WinRate + 0.15×(1-MaxDD) + 0.10×TradeCount
   - Sensitivity: for each param, compute mean composite per unique value.
   - If best composite > baseline → update `baseline.json`.
   - Propose perturbations for the most sensitive param → `next_experiment.json`.
5. **Repeat** until iterations exhausted or time cap reached.

## Constraints

- IBKR Gateway must be running on port 4002 for historical data.
- Each `main.py` call takes ~30–90s (IBKR pacing).
- A 27-combo sweep on one symbol ≈ 15–40 min.
- Only tune strategy params. Don't modify plumbing (`main.py` args, `historical_data.py`).
