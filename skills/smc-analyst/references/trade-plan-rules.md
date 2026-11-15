# Trade-plan rules (`smc/signal.py`, `smc/tracker.py`)

A priced plan is built **only** when the top-down cascade's status is `ready`. Anything else returns a `no_setup` plan that carries the cascade's `waiting_for`; the gate rule is never bypassed. Every number below comes from `config.default.json`.

## Levels
| Item | Rule | Config |
|---|---|---|
| **Entry** | Proximal edge of the entry zone (top of a bullish zone, bottom of a bearish one), or its 50% | `trade.entry_mode` = `ob_edge` / `ob_50` |
| **Entry type** | `limit` if price must retrace to the entry; `market` if price is already at or inside the zone | — |
| **Stop loss** | Beyond the further of (zone distal edge, CHoCH-leg invalidation extreme), plus `sl_atr_buffer` × ATR of the confirmation timeframe | `trade.sl_atr_buffer` (0.25), `structure.confirmation_timeframe` |
| **TP1** | Nearest opposing liquidity beyond the entry: the cascade's internal target, or a resting 15m/5m pool | — |
| **TP2** | Next external swing beyond TP1 (1H, then 4H swing-high/low pools) | — |
| **TP3** | External draw on liquidity (daily/HTF DOL), or the nearest opposing HTF POI (1D/1W) beyond TP2 | — |
| **R:R** | (target − entry) / (entry − stop) for each target | — |
| **Validity** | `valid` when at least one target reaches `min_rr`; the first such target is the **primary target**. Otherwise `no_valid_setup`. | `trade.min_rr` (2.0) |

- Targets keep their semantic names. If no 1H/4H swing qualifies, a plan can have TP1 and TP3 only.
- If price has already covered ≥ 50% of the way from entry to TP1, the plan notes that the limit entry needs a retrace.

## Position sizing
`units = (account × risk_pct / 100) / (|entry − stop| × quote→account rate)`

| Asset class | Units | Lots / notes |
|---|---|---|
| Crypto | coins, floored to 6 decimals | USDT/USDC treated as USD |
| Stocks | whole shares (floored) | warns if the budget can't buy one share |
| Forex | base-currency units | standard lots (100,000). USD-quoted pairs convert at 1; USD-based pairs (USDJPY) at 1/price; crosses (GBPJPY) through the USD/quote rate from Yahoo |
| Gold / silver | ounces | lots of 100 oz (XAU) / 5,000 oz (XAG); priced from the futures proxy |
| Indices | units per point (CFD) | — |

- A warning is added when exposure exceeds the account size, meaning leverage or margin is required.
- Defaults are `risk.account_size` (unset), `risk.risk_pct` (1%) and `risk.account_currency` (USD).

## Opposite-bias scenario
- **Trigger:** a confirmation-timeframe candle **closes** beyond the plan's invalidation level.
- **Flipped bias:** the counter-direction trade.
- **POI:**
  - the nearest opposing zone (15m/1H/4H) between the trigger and the plan's entry
  - otherwise the **failed entry zone acting as a flip zone (breaker)**
- **Entry, stop and target:** entry at the POI's proximal edge; stop beyond its distal edge plus the ATR buffer; target at the nearest resting liquidity beyond the trigger.
- **Low R:R:** a scenario below `min_rr` is labelled "information only". The dashboard draws the scenario as a ghost.

## Correlated hedge
See `symbols.md` for the partner map.
1. Fetch the partners' daily closes (`hedge.history_days`, 120).
2. Align them by date with the traded symbol's daily closes.
3. Compute the correlation and beta of daily returns over `hedge.correlation_window_days` (30).
4. Pick the partner with the highest |correlation|.
   - If |corr| < `hedge.min_abs_correlation` (0.5), the status is `suppressed`, with the reason.
   - If there isn't enough data, the status is `unavailable`.
5. **Direction:** a long position with a positively correlated partner is hedged by shorting the partner; negative correlation means going long the partner; short positions are the mirror image.
6. **Size:** hedge notional = |beta| × position notional. Effectiveness = corr² (the share of variance offset).

## Tracking and journal
- **States:** `pending → triggered → tp1_hit → tp2_hit → tp3_hit`, plus `sl_hit`, `stopped_breakeven` and `expired`.
- **Management model:**
  - The position is split equally across n targets. After TP1 the stop moves to breakeven.
  - Realised R = Σ rr(hit targets)/n, plus the remaining fraction × (−1 before TP1, 0 after).
  - A candle touching both the stop and a target counts as the stop. On the fill candle, only the stop is checked.
- **Expiry:** a pending signal expires after `tracker.signal_expiry_hours` (48), or immediately if TP1 trades before the entry fills ("missed").
- **Catch-up:**
  - Each signal stores the last processed closed candle.
  - `refresh()` replays candles since then on `tracker.timeframe` (5m), up to `tracker.max_catchup_bars` (5000).
  - Replays are idempotent.
- **Stats:**
  - **hit rate** = wins / (wins + losses), where a win means at least TP1 was reached
  - **average R** and **total R** are over closed signals
  - expired signals are excluded
- **Journal:** `tracker.journal_path` (default `journal.json` in the skill folder, git-ignored). The `SMC_ANALYST_JOURNAL` environment variable overrides it.
