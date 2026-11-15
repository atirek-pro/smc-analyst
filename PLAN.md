# PLAN: `smc-analyst`, a Claude Code skill for Smart Money Concepts analysis with a live dashboard

## 1. Context and goal
The goal is a polished, open-source Claude Code skill hosted on GitHub. People install it with one command:
- `npx skills add <github-user>/smc-analyst`, or
- `/plugin marketplace add <github-user>/smc-analyst`

Once it is installed, a user can say *"Do an SMC analysis on BTCUSDT"* (or AAPL, or EURUSD). Claude then:
1. Runs a **top-down, multi-timeframe SMC analysis** the way a manual trader does: Weekly → Daily → 4H → 1H/15m → 5m/1m. It covers swings, BOS, CHoCH, liquidity (BSL/SSL and sweeps), inducement, order blocks, FVGs, supply and demand, premium/discount/OTE, and LTF confirmation.
2. Launches a **local live dashboard** with candlesticks that update in real time. Every detected element is drawn on the chart, and a timeframe switcher covers **1W / 1D / 4H / 1H / 15m / 5m / 1m**.
3. Produces a **trade plan** with entry, SL, TP1/TP2/TP3, R:R and position size. It also gives an **opposite-bias scenario** and a **correlated-asset hedge**. All of it is drawn on the chart and **tracked live** (pending → triggered → TP/SL hit) so the user can check whether the signal worked.

**Decisions already made**
- Engine: **Python 3.12**
- Chart: **TradingView Lightweight Charts**, a pinned exact version (v4.1+ for series primitives), vendored so it works offline
- Data: **free, no API key**. Crypto uses the Binance public REST API and WebSocket (real-time). Stocks and forex use yfinance (about 1 min delay).
- Hedging: **opposite-bias scenario + correlated-asset hedge**

**Environment:** the project root is `D:\AI Engineering\SMC-Concepts-Skill` (it becomes the git repo). Node 22 and Python 3.12 are installed. `smc-skill-env/` is a local dev venv and is git-ignored. The shipped skill creates its own `.venv` with `setup.py`.

---

## 2. Repository layout
```
SMC-Concepts-Skill/            (repo root → GitHub: smc-analyst)
├── PLAN.md
├── README.md                    # hero GIF, badges, install, usage, screenshots, disclaimer
├── LICENSE                      # MIT
├── CHANGELOG.md
├── .gitignore                   # smc-skill-env/, .venv/, __pycache__/, cache/, journal.json
├── .claude-plugin/
│   ├── plugin.json              # plugin manifest (name, version, description, author)
│   └── marketplace.json         # enables /plugin marketplace add
├── skills/smc-analyst/          # everything that ships with the skill lives here
│   ├── SKILL.md                 # frontmatter + workflow Claude follows
│   ├── config.default.json      # swing lookbacks, ATR mults, EQH tolerance, min RR, risk %
│   ├── references/              # loaded on demand (progressive disclosure)
│   │   ├── smc-methodology.md   # exact, written rules for every concept
│   │   ├── top-down-workflow.md # the 5-stage cascade + gate rules
│   │   ├── trade-plan-rules.md  # entry/SL/TP/RR/sizing, opposite scenario, hedges
│   │   ├── symbols.md           # symbol formats per asset class + correlation map
│   │   └── output-format.md     # report template Claude prints
│   ├── scripts/
│   │   ├── setup.py             # cross-platform venv create + pip install (Win Scripts/ vs bin/)
│   │   ├── requirements.txt     # pinned: pandas, numpy, yfinance, ccxt, fastapi, uvicorn, websockets
│   │   ├── analyze.py           # CLI → analysis JSON (no server)
│   │   ├── serve.py             # FastAPI dashboard + live stream (+ --stop)
│   │   └── smc/                 # engine package
│   │       ├── config.py        # load defaults + user overrides
│   │       ├── data.py          # symbol resolver, OHLCV fetch (1M..1m), cache, live feeds
│   │       ├── indicators.py    # ATR, displacement, returns/correlation/beta helpers
│   │       ├── swings.py        # fractal pivots; internal vs swing structure
│   │       ├── structure.py     # BOS / CHoCH, trend state
│   │       ├── liquidity.py     # EQH/EQL, BSL/SSL, sweeps, PDH/PDL/PWH/PWL
│   │       ├── inducement.py    # bait liquidity before a POI
│   │       ├── zones.py         # order blocks, FVG, supply/demand, mitigation state
│   │       ├── pd_array.py      # dealing range, EQ, premium/discount, OTE 62–79%
│   │       ├── sessions.py      # Asia/London/NY killzones, market-hours awareness
│   │       ├── mtf.py           # top-down cascade orchestration + gate rules
│   │       ├── signal.py        # entry/SL/TPs, RR, sizing, opposite scenario, hedge
│   │       ├── tracker.py       # live signal state machine + journal.json (with catch-up)
│   │       └── schema.py        # dataclasses → JSON contract for dashboard
│   └── assets/dashboard/
│       ├── index.html
│       ├── app.js               # chart, TF switch, WS client, panels
│       ├── overlays.js          # primitives: boxes, lines, labels, markers, position tool
│       ├── styles.css           # dark/light theme
│       └── vendor/lightweight-charts.standalone.production.js
├── tests/                       # pytest, deterministic synthetic OHLCV fixtures
├── examples/                    # sample JSON outputs + screenshots
└── .github/workflows/ci.yml     # ruff + pytest + SKILL.md frontmatter validation
```

---

## 3. Step-by-step build plan

### Phase 1: Scaffold and distribution
1. `git init` the project root. Add the skeleton, LICENSE, `.gitignore` and an empty CHANGELOG.
2. Write the `SKILL.md` frontmatter: `name: smc-analyst` and a trigger-rich `description` ("Use when the user asks for Smart Money Concepts / SMC / ICT analysis, order blocks, BOS, CHoCH, liquidity, FVG, or a trade setup on a stock, crypto or forex pair…").
3. Add `.claude-plugin/plugin.json` and `marketplace.json`. Before release, check the current `npx skills` CLI (vercel-labs/skills) layout requirements.
4. Write `setup.py` (it detects the OS, creates `.venv`, installs the pinned requirements and is idempotent) and `config.default.json`.

### Phase 2: Data layer (`data.py`, `indicators.py`)
5. Symbol resolver: `BTCUSDT` or `BTC/USDT` → crypto via ccxt Binance. `EURUSD` → `EURUSD=X` and `XAUUSD` → `GC=F`, both on yfinance. `AAPL` → yfinance. Unknown or empty symbols raise a clear error.
6. `get_ohlcv(symbol, tf, limit)` for **1M, 1W, 1D, 4H, 1H, 15m, 5m, 1m**:
   - respect yfinance limits (1m ≈ 7 days, 5m/15m ≈ 60 days, 4H resampled from 1H)
   - add an on-disk cache and back off when rate-limited
   - round to each instrument's tick size and price precision
7. Live feeds: the **Binance kline WebSocket** for crypto and a **yfinance poller** (every 15–60 s) for stocks and forex. Stock and forex prices are labelled "delayed ~1m". When the market is closed, show "Market closed".
8. `indicators.py` provides ATR, a displacement check (body > k×ATR) and rolling returns, correlation and beta.

### Phase 3: SMC engine (strict, documented rules; write unit tests alongside each module)
9. `swings.py`: fractal pivots with a left/right lookback per timeframe from config. Mark **swing** (external) and **internal** structure separately.
10. `structure.py`: **BOS** = a candle *body close* beyond the last swing high or low in the trend direction. **CHoCH** = the first break against the prevailing trend. Output the trend state and break history.
11. `liquidity.py`: equal highs/lows (tolerance = x×ATR), BSL/SSL pools, **sweeps** (a wick through the level, then a close back inside) and PDH/PDL/PWH/PWL.
12. `zones.py`:
    - **Order block** = the last opposite candle before a displacement leg that caused a BOS or CHoCH
    - **FVG** = a 3-candle imbalance
    - **Supply/demand** = base-and-departure zones
    - each zone has a state: fresh, mitigated or invalidated
13. `pd_array.py`: the dealing range from the latest swing leg, EQ (50%), premium/discount and OTE (62–79%).
14. `inducement.py`: the first minor internal pivot before a POI. A setup is only valid after it has been swept. `sessions.py`: Asia/London/NY killzones for timing entries on forex and indices.
15. `mtf.py`: the **top-down cascade, copying the manual trader's routine**. Each stage passes its output down and records a written rationale.
    - **Stage 1, Weekly (Monthly optional): the big picture.** Major swing highs and lows give the external range, and the sequence of swing BOS gives the trend. Mark the weekly dealing range (P/D/EQ), HTF OBs and FVGs, and PWH/PWL plus old highs/lows as external liquidity. Output: **HTF bias, HTF POIs and the draw on liquidity (DOL)**.
    - **Stage 2, Daily: confirm or refine.** Does daily structure agree with the weekly, or is a daily CHoCH starting a reversal? Is price in premium or discount? Pick the daily POI that sits inside or next to a weekly POI, and add PDH/PDL. Output: **confirmed bias, target POI and the daily DOL**.
    - **Stage 3, 4H: the intermediate structure.** Separate swing and internal structure, mark inducement, and refine the POI to the 4H OB or FVG inside it. Output: **refined POI and inducement level**.
    - **Stage 4, 1H/15m: wait for price to reach the POI.** The status goes `waiting` → `in_poi`. Look for the sweep or inducement grab, then the **LTF CHoCH** in the HTF direction. Output: **confirmation event**.
    - **Stage 5, 5m/1m: entry refinement.** The entry is the OB or FVG created by the confirming CHoCH. The SL goes beyond the sweep wick. Targets are internal liquidity first, then the external DOL.
    - **Gate rule:** if any stage fails, stop and report "no setup — waiting for X", naming the condition still missing. Never force a trade.
    - **Output:** a per-stage rationale, an alignment matrix (TF × trend/bias) and a confluence score from 0 to 100.

### Phase 4: Trade plan and tracking (`signal.py`, `tracker.py`)
16. Trade levels:
    - **Entry** at the refined OB/FVG edge, or the OB's 50% (configurable)
    - **SL** beyond the OB or sweep wick, plus an ATR buffer
    - **TP1** at the nearest opposing liquidity, **TP2** at the next swing, **TP3** at the HTF POI or external DOL
    - **R:R** for each target. If R:R is below the config minimum (default 2), report "no valid setup".
17. **Position sizing**: optional `--account` and `--risk-pct` flags give units or lots for each asset class.
18. **Opposite-bias scenario**: if price closes through the invalidation level, emit the counter setup (flipped bias, new POI, entry, SL and TP), drawn as a ghost.
19. **Correlated hedge**: start from the `symbols.md` map (EURUSD↔DXY `DX-Y.NYB`/USDCHF, BTC↔ETH, AAPL↔QQQ, XAUUSD↔DXY…). Fetch the partner's daily data and compute a live 30-day rolling correlation and beta. Output the hedge instrument, direction and β-based size ratio. If |corr| is below 0.5, suppress the hedge and say why.
20. `tracker.py`: a state machine (pending → triggered → TP1/TP2/TP3 hit, SL hit, or expired), persisted in `journal.json`. When the server restarts, it **catches up** by replaying the candles since the signal was created, so outcomes stay correct even if the dashboard was closed. It reports a hit rate and average R.

### Phase 5: Dashboard (`serve.py` + `assets/dashboard`)
21. FastAPI endpoints:
    - `GET /api/candles?tf=`
    - `GET /api/analysis?tf=`
    - `GET /api/journal`
    - `WS /ws/stream`, which pushes the forming candle and **recomputes the analysis on each candle close** (cached per TF)
22. Chart overlays, drawn with Lightweight Charts primitives:
    - BOS and CHoCH as labelled lines (colored by direction)
    - OB, FVG and supply/demand as shaded boxes that end when mitigated
    - liquidity as dashed lines, with ✕ markers for sweeps; inducement markers
    - premium/discount and EQ bands, plus OTE
    - a **position tool**: entry line, red SL box, green TP boxes, and the opposite scenario as a ghost
    - a **"Show HTF zones on LTF" toggle** that projects weekly, daily and 4H POIs onto lower timeframes, as traders do by hand
23. UI:
    - a TF switcher (1W/1D/4H/1H/15m/5m/1m) and layer toggles
    - a side panel with the **per-stage top-down rationale**, the alignment matrix, the trade plan card, the hedge card, a live status badge and a journal table
    - a dark/light theme, keyboard shortcuts and a disclaimer in the footer
24. `serve.py` picks a free port, opens the browser and prints the URL. `--stop` kills a running server, and a PID file prevents duplicate servers.

### Phase 6: Claude workflow (`SKILL.md`)
25. The steps Claude follows:
    1. Parse the symbol and asset class; ask if it is ambiguous.
    2. Run `setup.py` if `.venv` is missing.
    3. Run `analyze.py --symbol X --json` (plus optional account and risk).
    4. Read `references/*` only when needed.
    5. Write the report from `output-format.md`: the stage-by-stage top-down narrative, the trade plan table, the opposite scenario, the hedge and the risks, or "no setup — waiting for X".
    6. Launch `serve.py` in the background and give the user the link and the stop command.
    7. Always include a "not financial advice" disclaimer.
26. Keep SKILL.md under about 300 lines and put the detailed rules in `references/`.

### Phase 7: Quality and release polish
27. Tests:
    - synthetic fixtures with known swings, BOS, CHoCH, OB, FVG, sweeps and inducement
    - cascade gate tests (conflicting HTF → no setup)
    - tracker state tests
    - golden-file JSON for `analyze.py`
    - a CI workflow
28. README: a hero GIF, both install routes, example prompts, a screenshot per concept, a config table, data-limit notes, a disclaimer and a roadmap (API-key feeds, backtester, alerts).
29. Tag `v1.0.0`, publish a GitHub release, and submit to the skills registries and marketplaces.

---

## 4. Verification
- `pytest` passes: the detectors, the cascade gates and the tracker.
- `python scripts/analyze.py --symbol BTCUSDT` (also `AAPL`, `EURUSD`, `XAUUSD`) produces JSON that matches `schema.py`, with a rationale for each stage.
- Compare the engine's output by hand against a TradingView chart for 2–3 symbols: swings, BOS/CHoCH and OBs should line up.
- `python scripts/serve.py --symbol BTCUSDT`:
  - candles tick live
  - every TF switch, 1W through 1m, redraws its overlays
  - the HTF-projection toggle works
  - the position tool shows entry, SL and TP
- Fresh-machine test: `npx skills add <user>/smc-analyst` in a clean folder, then ask Claude Code "SMC analysis on ETHUSDT". Setup, report and dashboard should all run end to end.
- Run a live crypto signal on 1m/5m. The tracker should move through its states, write `journal.json`, and catch up correctly after a server restart.
