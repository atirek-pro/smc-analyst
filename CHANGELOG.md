# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
