---
name: smc-analyst
description: Smart Money Concepts (SMC / ICT) market analysis for any stock, crypto or forex pair (e.g. AAPL, BTCUSDT, EURUSD, XAUUSD). Use when the user asks for SMC or ICT analysis, a top-down multi-timeframe read, market structure (BOS, CHoCH), liquidity sweeps, order blocks, fair value gaps, supply and demand, premium/discount, or a trade setup with entry, stop loss, targets and hedging. Produces a stage-by-stage report and launches a live candlestick dashboard with every concept drawn on the chart.
---

# SMC Analyst

> **Build status: Phase 5 (live dashboard).** The full report workflow arrives in Phase 6. Until then this skill can prepare its environment, fetch market data, run the engine's cascade, trade plan and signal tracker, and serve the live dashboard for development and testing. If the user asks for an analysis now, say the engine is not built yet. Do not improvise an analysis that looks as if it came from this skill.

## What this skill will do
Analyse an instrument top-down the way a discretionary SMC trader does:
1. **Weekly**: big picture, HTF bias, POIs and draw on liquidity
2. **Daily**: confirm or refine
3. **4H**: intermediate structure and inducement
4. **1H / 15m**: wait for the POI tap, the sweep and the LTF CHoCH
5. **5m / 1m**: refine the entry

It then opens a live dashboard and a tracked trade plan.

## Step 1: Prepare the environment (available now)
Run this once per install. It is safe to repeat, because it skips anything already done:

```bash
python "<skill-dir>/scripts/setup.py" --json
```

- `<skill-dir>` is the folder that contains this `SKILL.md`.
- Use `python3` if `python` is not 3.12 or newer.
- The last line of output is JSON. `"status": "ready"` means the environment works, and `"venv_python"` is the interpreter that every later skill script must run with.
- `--check` reports the status without installing anything. `--force` rebuilds the environment.
- If the status is `"error"`, show the user the `message` field. It explains the fix, for example installing Python 3.12 or newer.

Settings defaults are in `config.default.json`. A user can override any key by creating `config.json` next to it.

## Disclaimer
Every analysis output must end with: *"Educational analysis only — not financial advice."*
