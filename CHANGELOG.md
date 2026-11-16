# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.0.0] - 2026-11-16

First stable release: top-down SMC analysis, trade plan, live dashboard and signal tracking, driven by Claude
through `SKILL.md`. Highlights are in `docs/release-notes/v1.0.0.md`.

### Added (Phase 7: quality and release polish)
- **CI** (`.github/workflows/ci.yml`):
  - lint: ruff, plus manifest / SKILL.md frontmatter / JS syntax checks
  - offline test matrix: Linux, Windows and macOS on Python 3.12, plus Linux on 3.13, with deprecations as errors
  - a `setup.py` smoke test
- `smc/schema.py`: dependency-free JSON contract and validator for `analyze.py` (analysis, resolve and error shapes).
- **Golden files** (`tests/golden/`): the full JSON and markdown report for deterministic READY and WAITING markets, regenerated with `SMC_UPDATE_GOLDEN=1`.
- `tools/make_media.py`: regenerates the README screenshots and hero GIF from the live dashboard (headless Edge/Chrome plus Pillow).
- `docs/images/`: hero GIF and 9 screenshots (structure, order blocks, FVG, liquidity, premium/discount, HTF projection, position tool, full dashboard, light theme).
- README rewritten:
  - hero GIF, CI badge, example prompts, both install routes
  - screenshot grid, report excerpt, symbols and data limits, configuration table
  - CLI, layout, development, roadmap, disclaimer, credits
- `RELEASING.md` release checklist; `docs/release-notes/v1.0.0.md`.
- Dashboard: `?layers=` deep link; the price scale always includes the plan's entry, stop and TP1 (series-primitive `autoscaleInfo`).
- `pyproject.toml` (ruff + pytest config), `.gitattributes` (LF line endings for cross-OS golden files), dev pins `ruff==0.17.3` and `pillow==13.0.0`.

### Changed
- Codebase linted with ruff (imports sorted, `datetime.UTC`, PEP 695 generic `with_retries`, explicit loop-variable binding in the tracker, `raise … from None`).
- Plugin manifest: `homepage` and `repository`; version 1.0.0.

## [0.6.0] - 2026-11-16

### Added (Phase 6: Claude workflow)
- `SKILL.md`: the complete workflow Claude follows (137 lines):
  1. identify the instrument, asking when ambiguous
  2. environment setup
  3. `analyze.py`
  4. references on demand
  5. present the report verbatim
  6. launch the dashboard in the background with link and stop command
  7. disclaimer
  It also covers follow-up requests, error handling and guardrails.
- `scripts/analyze.py`: the single entry point.
  - `--resolve` (offline symbol reading plus ambiguity)
  - full analysis with `--json` / `--format markdown`, `--asset-class`, `--account`, `--risk-pct`, `--track`, `--no-hedge`
  - one JSON contract (`summary`, `cascade`, `plan`, `dashboard_args`, `report_markdown`)
  - exit codes 0/1/2/3
  - a clear error when run outside the skill `.venv`
- `references/output-format.md`: report structure, what Claude may add, the dashboard section, wording per situation, multi-symbol and follow-up guidance.
- Symbol resolution:
  - `resolve_symbol(query, asset_class=None)` forces a reading
  - TradingView exchange prefixes set the class (`NASDAQ:`, `NYSE:`, `NSE:`→`.NS`, `LSE:`→`.L`, `BINANCE:`, `FX:` …)
  - `alternative_readings()` flags ambiguous bare inputs (crypto coins that are also stock tickers; `GOLD`)
- `mtf.analyze_symbol` and the dashboard accept a pre-resolved instrument; `serve.py --asset-class`.
- `serve.py --detach`: starts the dashboard as an independent process (new process group/session, log in `.server.log`), waits until it answers, prints the URL and returns. The dashboard then survives the end of the Claude turn that launched it; background shell jobs were killed when the turn ended.
- Dashboard accepts lowercase deep links (`?tf=4h`, `1d`, `1w`).
- 31 new tests (`tests/test_analyze.py`, plus `--detach` tests in `tests/test_serve.py`).

### Fixed
- A plan whose TP1 had already traded was still reported as `valid`; it is now `no_valid_setup` ("the move has played out").
- Rationale now says "below/above the range" instead of "discount (-0%)" when price has left the dealing range.

### Changed
- Version 0.6.0.

## [0.5.0] - 2026-11-15

### Added (Phase 5: live dashboard)
- `scripts/serve.py`: FastAPI server for one symbol with these endpoints:
  - `GET /api/meta`, `/api/candles?tf=`, `/api/analysis?tf=`, `/api/journal`
  - `POST /api/track`, `/api/refresh`, `/api/shutdown`
  - `WS /ws/stream`: live candles for the viewed timeframe, and status, `analysis_updated` and `journal` notifications
- Live data: on-demand upstream feeds per timeframe (the viewed timeframes, plus the pinned confirmation and tracker timeframes). A candle close recomputes that timeframe's cached analysis, refreshes stale HTF frames, and rebuilds the cascade and plan. A reconnect triggers a backfill. Live candles feed the journal tracker.
- Process management: free-port selection, a PID file (duplicate prevention), `--stop`, `--status`, `--replace`, `--json`, `--track`, and the browser opens automatically once the server is ready.
- `assets/dashboard/` (no build step): `index.html`, `styles.css`, `app.js`, `overlays.js`, and vendored TradingView Lightweight Charts 5.2.1 (Apache-2.0).
  - Custom series primitive drawing BOS/CHoCH lines, OB/FVG/S&D boxes that end on mitigation, BSL/SSL/EQH/EQL/PDH... liquidity, sweep ✕ marks, inducement, premium/discount/EQ/OTE, HTF POIs projected onto lower timeframes, the position tool (entry/SL/TP boxes) and the opposite scenario as a ghost.
  - UI: 1W–1m switcher, layer toggles, side panel (setup and confluence, trade plan, opposite scenario, hedge, top-down rationale, alignment matrix, journal), dark/light theme, keyboard shortcuts, `?tf=` / `?theme=` deep links, disclaimer footer.
- 15 new tests (`tests/test_serve.py`).

### Changed
- `zones.max_zones_per_timeframe` default 10 → 6 (chart readability; the cascade still searches every active zone).
- Dev dependency `httpx2==2.16.0` for FastAPI's TestClient.
- Version 0.5.0.

## [0.4.0] - 2026-11-15

### Added (Phase 4: trade plan and tracking)
- `smc/signal.py`: priced trade plans built from READY cascades. Includes entry (zone edge or 50%), SL (distal or invalidation, plus ATR buffer), TP1 (nearest liquidity), TP2 (next swing), TP3 (external DOL / opposing HTF POI), R:R per target, a validity gate (`min_rr`), limit-vs-market entry, and a note when most of the move to TP1 is done.
- Position sizing for crypto, stocks, forex (lots, with quote-to-USD conversion), gold/silver and indices, with a leverage warning.
- Opposite-bias scenario: trigger on a close beyond invalidation; nearest opposing zone or flip zone; entry, SL, TP and R:R.
- Correlated hedge: partner map, 30-day correlation and beta, direction, beta-weighted size, suppression below |0.5|.
- `smc/tracker.py`: signal state machine (pending, triggered, TP1-3, SL, breakeven stop, expired), scale-out plus breakeven management model, JSON journal, idempotent catch-up replay, live-candle updates, hit rate and average R.
- CLIs: `python -m smc.signal SYMBOL [--account --risk-pct --track --json]` and `python -m smc.tracker [--refresh --json]`.
- `references/trade-plan-rules.md` and `references/symbols.md`.
- 42 new tests (`test_signal.py`, `test_tracker.py`).

### Changed
- New config keys: `risk.account_currency`, `hedge.history_days`, `tracker.timeframe`, `tracker.journal_path`, `tracker.max_catchup_bars`.
- Version 0.4.0.

## [0.3.0] - 2026-11-15

### Added (Phase 3: SMC engine)
- `smc/swings.py`: fractal swing and internal pivots with confirmation index (no look-ahead), HH/HL/LH/LL labels.
- `smc/structure.py`: BOS / CHoCH on body close (or wick), trend state, break history with impulse origin.
- `smc/liquidity.py`: BSL/SSL pools, equal highs/lows (ATR tolerance), PDH/PDL/PWH/PWL, swept vs taken status.
- `smc/zones.py`: order blocks (displacement- or FVG-validated), FVGs, supply/demand, with fresh/mitigated/invalidated state.
- `smc/pd_array.py`: dealing range, EQ, premium/discount, OTE.
- `smc/inducement.py` and `smc/sessions.py`: inducement detection and sweep, ICT killzones, session ranges.
- `smc/mtf.py`: per-timeframe analysis plus the 5-stage top-down cascade with gate rule, per-stage rationale, alignment matrix, 0–100 confluence score, and a `python -m smc.mtf` CLI.
- `references/smc-methodology.md` and `references/top-down-workflow.md`: the exact rules.
- 61 new tests (`test_swings_structure.py`, `test_liquidity_zones.py`, `test_mtf.py`).

### Changed
- New config keys: `structure.confirmation_timeframe`, `structure.entry_timeframe`, `zones.ob_search_back_bars`, `zones.base_max_body_atr`, `zones.base_max_candles`, `pd_array.equilibrium_band`.
- Plugin version 0.3.0.

## [0.2.0] - 2026-11-15

### Added (Phase 2: market-data layer)
- `smc/config.py`: deep-merges `config.default.json` with an optional user `config.json`.
- `smc/data.py`: offline symbol resolver (crypto, forex, stocks, indices, metals and commodities), timeframe normalisation (1M to 1m), Binance REST klines with pagination and a mirror-host fallback, Yahoo Finance via yfinance, session-aligned 4H resampling, tick-size rounding, on-disk CSV cache with TTL, retry with backoff, market-hours status with next open, and a `python -m smc.data` CLI.
- `smc/feeds.py`: live candles via Binance kline WebSocket (reconnect, mirror host, REST-poll fallback) and Yahoo polling (market-closed aware, degraded-mode backoff), plus a `python -m smc.feeds` CLI.
- `smc/indicators.py`: TradingView-matching ATR (Wilder), displacement, returns, daily alignment, rolling correlation and beta.
- 100 offline unit tests (`tests/test_config.py`, `test_data.py`, `test_feeds.py`, `test_indicators.py`).

### Changed
- `requirements-dev.txt` now includes the runtime pins so tests can import the engine.
- Plugin version 0.2.0.

## [0.1.0] - 2026-11-15

### Added
- Phase 1 scaffold: repository layout, MIT license, `.gitignore`.
- `skills/smc-analyst/SKILL.md` with trigger-rich frontmatter.
- Claude Code plugin manifest (`.claude-plugin/plugin.json`) and marketplace (`.claude-plugin/marketplace.json`).
- `scripts/setup.py`: cross-platform, idempotent virtualenv bootstrap with pinned `requirements.txt`.
- `config.default.json`: default engine, trade, risk, data and server settings.
- `tests/test_scaffold.py`: structural checks for manifests, SKILL.md, config and setup.
