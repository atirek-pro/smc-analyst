# SMC Analyst: a Smart Money Concepts skill for Claude Code

> Top-down **Smart Money Concepts (SMC / ICT)** analysis for stocks, crypto and forex. It opens a **live candlestick dashboard** with every concept drawn on the chart and produces a **tracked trade plan**.

![status](https://img.shields.io/badge/status-phase%202%20of%207-orange) ![license](https://img.shields.io/badge/license-MIT-blue) ![python](https://img.shields.io/badge/python-3.12%2B-blue)

> 🚧 **Work in progress.** Phase 1 (scaffold and environment setup) and Phase 2 (market-data layer) are complete. The analysis engine, trade plan and dashboard land in later phases. See [PLAN.md](PLAN.md).

## What it will do

Ask Claude _"Do an SMC analysis on BTCUSDT"_ and the skill will:

1. Read the market **top-down** the way a discretionary trader does: Weekly → Daily → 4H → 1H/15m → 5m/1m. It covers swings, BOS, CHoCH, liquidity and sweeps, inducement, order blocks, FVGs, supply and demand, and premium/discount/OTE.
2. Open a **live dashboard** (TradingView Lightweight Charts) with a 1W–1m timeframe switcher and every element drawn on the chart.
3. Give a **trade plan**: entry, SL, TP1–TP3, R:R, position size, an opposite-bias scenario and a correlated hedge. The dashboard tracks it live so you can see whether it worked.

## Install

Replace `atirek-pro` with the GitHub account that hosts this repo.

**Option A, skills CLI**

```bash
npx skills add atirek-pro/smc-analyst -a claude-code
```

**Option B, Claude Code plugin marketplace**

```text
/plugin marketplace add atirek-pro/smc-analyst
/plugin install smc-analyst@smc-analyst-marketplace
```

## Requirements

- Python **3.12+**. The skill builds its own isolated virtualenv on first use.
- An internet connection for market data. No API key is needed: crypto comes from Binance public data, and stocks and forex from Yahoo Finance.

## Repository layout

```
.claude-plugin/          plugin + marketplace manifests
skills/smc-analyst/
  SKILL.md               instructions Claude follows
  config.default.json    tunable defaults (override with config.json)
  references/            methodology docs (Phase 3+)
  scripts/setup.py       environment bootstrap
  scripts/smc/           engine: data layer (Phase 2), SMC detection (Phase 3+)
  assets/dashboard/      live chart UI (Phase 5)
tests/                   pytest suite
```

## Development

```bash
python -m venv smc-skill-env && smc-skill-env/Scripts/activate   # Windows; use bin/activate elsewhere
pip install -r requirements-dev.txt
python -m pytest -q
python skills/smc-analyst/scripts/setup.py --json
```

## Disclaimer

Educational analysis only. **Not financial advice.** Trading involves substantial risk of loss.

## License

[MIT](LICENSE)
