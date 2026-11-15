# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

### Phase 3

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

### Phase 2

### Added (Phase 2: market-data layer)
- `smc/config.py`: deep-merges `config.default.json` with an optional user `config.json`.
- `smc/data.py`: offline symbol resolver (crypto, forex, stocks, indices, metals and commodities), timeframe normalisation (1M to 1m), Binance REST klines with pagination and a mirror-host fallback, Yahoo Finance via yfinance, session-aligned 4H resampling, tick-size rounding, on-disk CSV cache with TTL, retry with backoff, market-hours status with next open, and a `python -m smc.data` CLI.
- `smc/feeds.py`: live candles via Binance kline WebSocket (reconnect, mirror host, REST-poll fallback) and Yahoo polling (market-closed aware, degraded-mode backoff), plus a `python -m smc.feeds` CLI.
- `smc/indicators.py`: TradingView-matching ATR (Wilder), displacement, returns, daily alignment, rolling correlation and beta.
- 100 offline unit tests (`tests/test_config.py`, `test_data.py`, `test_feeds.py`, `test_indicators.py`).

### Changed
- `requirements-dev.txt` now includes the runtime pins so tests can import the engine.
- Plugin version 0.2.0.

### Phase 1

### Added
- Phase 1 scaffold: repository layout, MIT license, `.gitignore`.
- `skills/smc-analyst/SKILL.md` with trigger-rich frontmatter.
- Claude Code plugin manifest (`.claude-plugin/plugin.json`) and marketplace (`.claude-plugin/marketplace.json`).
- `scripts/setup.py`: cross-platform, idempotent virtualenv bootstrap with pinned `requirements.txt`.
- `config.default.json`: default engine, trade, risk, data and server settings.
- `tests/test_scaffold.py`: structural checks for manifests, SKILL.md, config and setup.
