---
name: smc-analyst
description: Smart Money Concepts (SMC / ICT) market analysis for any stock, crypto or forex pair (e.g. AAPL, BTCUSDT, EURUSD, XAUUSD). Use when the user asks for SMC or ICT analysis, a top-down multi-timeframe read, market structure (BOS, CHoCH), liquidity sweeps, order blocks, fair value gaps, supply and demand, premium/discount, or a trade setup with entry, stop loss, targets and hedging. Produces a stage-by-stage report and launches a live candlestick dashboard with every concept drawn on the chart.
---

# SMC Analyst

Reads a market **top-down the way a discretionary Smart Money Concepts trader does**:
1. **Weekly:** bias, POIs, draw on liquidity.
2. **Daily:** confirm the bias, pick the target POI in discount/premium.
3. **4H:** refine the POI, find the inducement.
4. **15m:** wait for the POI tap, the sweep and the CHoCH.
5. **5m/1m:** the entry zone.

It then prices a trade plan (entry, stop, TP1–TP3, R:R, size, opposite scenario, correlated hedge) and opens a **live dashboard** with everything drawn on real-time candles.

All analysis comes from the bundled Python engine. **Never improvise levels, zones or signals yourself.** If the engine can't run, say so.

`<skill-dir>` below is this skill's base directory (the folder containing this file).

## Workflow

### 1. Identify the instrument
Take the symbol from the user's message (`BTCUSDT`, `btc/usdt`, `EURUSD`, `XAUUSD`/gold, `AAPL`, `RELIANCE.NS`, `NAS100`, `NASDAQ:SOL`, …). If no symbol is given, ask for one. Then check how it will be read (offline, instant):

```bash
<py> "<skill-dir>/scripts/analyze.py" --symbol SOL --resolve --json
```

- `"ambiguous": true`: **ask the user** which one they mean, listing the default reading and each item in `alternatives` (e.g. "Solana (SOLUSDT) or the stock SOL?"). Pass their choice as `--asset-class <class>` in every later command.
- Not ambiguous: continue without asking.
- An exchange prefix (`NASDAQ:`, `NYSE:`, `NSE:`, `BINANCE:`, `FX:`) already settles the asset class.

### 2. Make sure the environment is ready
Find a Python 3.12+ interpreter (`python`, then `python3`, then on Windows `py -3.12`) and run:

```bash
python "<skill-dir>/scripts/setup.py" --json
```

- The last output line is JSON. On `"status": "ready"`, use its `venv_python` as `<py>` for **every** other command.
- The first run installs dependencies and can take several minutes on a slow connection. Tell the user it's a one-time setup.
- Later runs return in under a second.
- On `"status": "error"`, show the `message` (e.g. install Python 3.12+) and stop.

Run this before step 1's `--resolve` if you don't yet know `venv_python`.

### 3. Run the analysis

```bash
<py> "<skill-dir>/scripts/analyze.py" --symbol EURUSD --json
```

Optional flags:
- `--asset-class stock|crypto|forex|index|commodity`: the clarified reading from step 1
- `--account 10000 --risk-pct 1`: position sizing. Only when the user gives an account size or risk; don't ask otherwise.
- `--track`: add a valid plan to the signal journal. Only when the user asks to track or follow the signal.
- `--no-hedge`: skips the hedge lookup (faster); use it if the user doesn't want a hedge.

Notes:
- Typically takes 5–15 s.
- Output is one JSON object: `status`, `summary` (headline, bias, setup_status, confluence, waiting_for, plan_status), `cascade`, `plan`, `market`, `dashboard_args` and **`report_markdown`**.
- **Exit codes:** 0 ok, 1 bad symbol (show `message` and `hint`), 2 market-data error (retry once, then report), 3 environment not set up (go to step 2).

### 4. Read references only when needed

| Need | File |
|---|---|
| How to present the report, the wording for each situation | `references/output-format.md` (read before your first report in a conversation) |
| Exact definition of BOS, CHoCH, OB, FVG, sweep, inducement, premium/discount | `references/smc-methodology.md` |
| How the 5 stages, gate rule, statuses and confluence score work | `references/top-down-workflow.md` |
| Entry/SL/TP rules, sizing, opposite scenario, hedge, tracking model | `references/trade-plan-rules.md` |
| Accepted symbol formats, hedge partner map | `references/symbols.md` |

### 5. Write the report
Present `report_markdown` as it is. It already follows `references/output-format.md`:
- verdict, the top-down stages with rationale, the alignment table
- the trade-plan table or "no trade — waiting for X" with levels to watch
- the opposite scenario, the hedge, risks, disclaimer

You may add one or two plain-language sentences before it, based on `summary.headline`.

Rules:
- **Never change a number**, invent a level, or turn `no_setup` / `no_valid_setup` into a trade.
- When there is no setup, say so clearly and name what the engine is waiting for. That is a valid, useful answer.
- Don't add your own market opinion, news or predictions on top of the engine's output.

### 6. Launch the live dashboard
After the analysis (the market data is now cached, so the dashboard loads fast), start the server with `--detach`. Run it as a **normal (foreground) command**. It spawns an independent server process that keeps running after your turn ends, waits until it answers, prints one JSON line and returns within a few seconds. Do **not** use a background shell job: those are killed when the turn ends, taking the dashboard with them.

```bash
<py> "<skill-dir>/scripts/serve.py" <dashboard_args...> --detach --json
```

- `dashboard_args` comes from the analysis JSON, e.g. `--symbol EURUSD --account 10000`.
- The output is one JSON line:
  - `"started"` or `"already_running"`: use its `url`.
  - `"busy"` (a dashboard for another symbol is running): re-run with `--replace`, and mention that the previous chart was replaced.
  - `"error"`: report it with the `log_tail`.
- The browser opens automatically. Add the **Live dashboard** section from `output-format.md`: the link with a fitting `?tf=` and the stop command.

```bash
<py> "<skill-dir>/scripts/serve.py" --stop      # stop it
<py> "<skill-dir>/scripts/serve.py" --status    # is it running?
```

If the user only wants the analysis ("no dashboard", "just the levels"), skip this step.

### 7. Disclaimer
Every analysis must end with: *Educational analysis only — not financial advice. Trading involves substantial risk of loss.* `report_markdown` already includes it; keep it.

## Follow-up requests

| User asks | Do |
|---|---|
| Another symbol | Repeat steps 1, 3, 5 and 6 (with `--replace`) |
| Several symbols | One `analyze.py` per symbol, then a comparison table (symbol, bias, status, confluence, plan). One dashboard, for the main symbol |
| Size it for my account | Re-run `analyze.py` with `--account` / `--risk-pct` |
| Track / follow this signal | Re-run with `--track` (only valid plans can be tracked), or use the dashboard's **Track this signal** button |
| How did my signals do? | From `<skill-dir>/scripts`: `<py> -m smc.tracker --refresh`. Report hit rate, average R and open signals |
| Explain a concept or a stage | Answer from the stage rationale; definitions from `smc-methodology.md` |
| Different settings (stricter R:R, wick breaks, etc.) | Explain the key in `config.default.json`; the user can override it in `<skill-dir>/config.json` |
| Stop the chart | `serve.py --stop` |

## Errors

| Problem | Response |
|---|---|
| `setup.py` error / exit code 3 | Show the message; the usual fix is installing Python 3.12+ and re-running setup |
| `SymbolError` | Show the message and suggest a valid form (e.g. `BTCUSDT`, `EURUSD`, `NASDAQ:SOL`) |
| `DataError` | The data provider is unavailable or rate-limited; retry once, then tell the user to try again shortly |
| Stocks or forex market closed | Normal: the analysis uses the last session; mention the next open |
| Dashboard won't start | Report it and give the manual `serve.py` command; the analysis is still valid |

## Guardrails
- Educational tool, not financial advice. Never tell the user to place a trade or promise outcomes.
- Report the engine's output faithfully, including "no setup" answers.
- The journal and dashboard run locally; nothing is sent anywhere except market-data requests (Binance and Yahoo Finance public endpoints).
