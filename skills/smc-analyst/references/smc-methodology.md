# SMC Methodology: the exact rules the engine applies

SMC is partly discretionary. This file pins down **one consistent, rules-based reading** of it. Every threshold named here is a key in `config.default.json`, so a user can change it in `config.json`.

General principles:
- Structure is judged on **closed candles only**. The forming bar is dropped first.
- A pivot is used only after it is confirmed, so the engine never looks ahead.
- All prices are rounded to the instrument's tick size.

---

## 1. Swings (`smc/swings.py`)
- **Swing high:** the bar's high is *strictly* above the `L` highs before it and *at least* as high as the `L` highs after it. When bars tie, the first one is the pivot.
- **Swing low:** the mirror image, using lows.
- **Confirmation:** a pivot at bar *i* is known only at the close of bar *i + L*.
- **Two levels per timeframe:**
  - **swing (external) structure:** `L = swings.lookback[tf]` (1W/1D: 3, 4H: 4, 1H and below: 5)
  - **internal structure:** `L = swings.internal_lookback` (2)
- **Labels:** each high is HH (higher high) or LH (lower high) against the previous high; each low is HL (higher low) or LL (lower low) against the previous low.

## 2. Structure: BOS and CHoCH (`smc/structure.py`)
The engine walks the bars in order, tracking the latest confirmed, still-unbroken swing high and swing low.

| Event | Rule |
|---|---|
| Bullish break | a candle **closes** above the tracked swing high (`structure.break_confirmation = "body_close"`; `"wick"` uses the high) |
| Bearish break | a candle closes below the tracked swing low |
| **BOS** | a break in the direction of the current trend, or the first break when the trend is unknown |
| **CHoCH** | a break *against* the current trend. It flips the trend. |

- Each swing level can be broken only once.
- Every break records its **origin**: the lowest low (for a bullish break) or highest high (for a bearish break) between the broken pivot and the break bar. That is where the impulsive leg started.
- The **trend** of a timeframe is the direction of its last break.

## 3. Liquidity (`smc/liquidity.py`)
- **BSL** (buy-side liquidity) rests above swing highs, equal highs, PDH and PWH. **SSL** (sell-side liquidity) rests below swing lows, equal lows, PDL and PWL.
- **Equal highs/lows:** consecutive internal pivots of the same kind within `liquidity.equal_level_tolerance_atr` × ATR (0.1) of each other. Chains of two or more form one pool, priced at the extreme.
- **PDH/PDL** come from the last *closed* daily bar, and **PWH/PWL** from the last closed weekly bar. They become active once that period closes.
- **Status:** the first bar that trades through a level decides it.
  - **swept:** the wick went through and the bar closed back on the original side (a stop hunt)
  - **taken:** the bar closed beyond the level
  - With `sweep_requires_close_back_inside = false`, any trade-through counts as swept.

## 4. Zones / POIs (`smc/zones.py`)
| Zone | Rule |
|---|---|
| **Order block** | For each BOS/CHoCH, the **last opposite-colour candle** at or up to `zones.ob_search_back_bars` (3) bars before the break's origin; the origin candle if none is found. The zone is that candle's full high–low range. If `ob_requires_displacement_or_fvg` is set, the leg must contain a displacement candle (body ≥ `displacement_body_atr` × previous ATR, 1.2) or a same-direction FVG. |
| **FVG** | 3-candle imbalance. Bullish when `low[i] > high[i-2]` (zone = high[i-2] to low[i]); bearish when `high[i] < low[i-2]`. Gaps smaller than `fvg_min_size_atr` × ATR (0.1) are ignored. |
| **Demand / supply** | 1 to `base_max_candles` (3) candles with body ≤ `base_max_body_atr` × ATR (0.5), followed by a displacement candle. Demand: proximal = highest base body, distal = lowest base wick. Supply is the mirror image. |

- **States:** **fresh** → **mitigated** (price trades back into the zone) → **invalidated** (a candle *closes* beyond the distal edge).
- Invalidated and zero-height zones are discarded.
- Charts show at most `max_zones_per_timeframe` (10) of the most recent zones per kind and direction. The cascade always searches all active zones.

## 5. Premium / discount (`smc/pd_array.py`)
- **Dealing range:** the last two alternating external pivots.
  - It extends if price makes a new extreme beyond the leg (the forming bar counts).
  - It flips direction if price breaks the leg's start.
- **EQ** is at `pd_array.equilibrium` (50%). Within ± `equilibrium_band` (5%) of the range around EQ, price is in "equilibrium".
- Above EQ is **premium**, where you look for sells. Below EQ is **discount**, where you look for buys.
- **OTE** is the 62–79% retracement of the leg (`pd_array.ote`).

## 6. Inducement (`smc/inducement.py`)
- **For a long:** the most recent **internal swing low above the demand POI** that formed after the POI was created.
- **For a short:** the latest internal swing high below the supply POI.
- It is **swept** once price trades beyond it. A setup is stronger after the inducement has been taken.

## 7. Killzones (`smc/sessions.py`)
- New York time, so daylight saving is handled: Asia 20:00–00:00, London 02:00–05:00, New York 07:00–10:00.
- They are used for timing on forex, indices and commodities.
- They are informational for crypto and don't apply to single stocks.
