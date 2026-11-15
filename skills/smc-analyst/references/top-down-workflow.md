# Top-down workflow: the 5-stage cascade (`smc/mtf.py`)

The engine reads a chart the way a discretionary SMC trader does: from the weekly down to the 1-minute. Each stage hands its result to the next, and each one writes a plain-English rationale.

**Gate rule:** the cascade stops at the first stage that fails or is still waiting. It reports exactly what is missing in `waiting_for`, and it never forces a setup.

| Stage | Timeframes | Question | Passes when | Output |
|---|---|---|---|---|
| 1 | 1W (+1M optional) | What is the HTF bias? | Weekly has a confirmed BOS/CHoCH. If weekly is undetermined, the daily is used. | bias, HTF POIs, draw on liquidity |
| 2 | 1D | Does the daily agree, and where is the POI? | Daily trend is not against the bias, **and** an active daily POI of the bias direction sits in **discount** (longs) or **premium** (shorts) on the entry side of price | target POI, daily DOL |
| 3 | 4H | Can the POI be refined? Where is the inducement? | The daily POI wasn't closed through on 4H, and the 4H isn't making a counter-trend BOS beyond the POI | refined POI, inducement |
| 4 | 15m (fallback 1H) | Has price tapped the POI and confirmed? | Tap, then a CHoCH in the bias direction, with no close beyond the POI's distal edge | tap, sweep, confirmation |
| 5 | 5m (+1m) | Where exactly is the entry zone? | The setup hasn't been invalidated, and its first target hasn't been reached since confirmation | entry zone, invalidation level, targets |

## Selection rules
- **Daily target POI.** Candidates are ranked by:
  1. inside the current daily dealing range
  2. overlapping or next to an HTF POI (within 0.25 × daily ATR)
  3. zone type: OB > FVG > supply/demand
  4. distance from price
- **4H refinement:** the 4H zone that overlaps the daily POI (OB first, then the largest overlap). If there is none, the daily POI is kept.
- **Confirmation:**
  - The **latest** CHoCH after the tap counts, because an earlier attempt may have failed while the POI held.
  - If there is no CHoCH (the LTF was already trending with the bias), the first BOS after the tap counts. It scores lower.
- **Sweep:** an opposite-side liquidity pool lying in or within 1 ATR of the POI, swept between the tap and the confirmation.
- **Entry zone:** an OB, then an FVG, created on the entry timeframe between the tap and the confirmation, plus 3 bars of slack. It may be refined to an overlapping 1m zone. If none is found, the refined POI is used.
- **Invalidation level:** the extreme of the leg that produced the CHoCH (the break's origin bar).
- **Targets:**
  - **internal:** the nearest pool beyond the confirmation close that was known at confirmation time
  - **external:** the daily DOL, then the HTF DOL

## Statuses
| Status | Meaning |
|---|---|
| `no_bias` | No confirmed HTF structure |
| `no_setup` | A stage failed: daily against weekly, no POI in discount/premium, POI invalidated, or the setup already invalidated or played out |
| `waiting` | Everything aligned; waiting for price to reach the refined POI |
| `in_poi` | POI tapped; waiting for the LTF CHoCH |
| `ready` | Confirmed setup with entry zone, invalidation level and targets. Phase 4 turns this into entry, SL and TP prices. |

## Confluence score (0–100)
| Factor | Max points |
|---|---|
| HTF bias from weekly (8 if from daily) | 15 |
| Daily aligned with bias (5 if undetermined) | 15 |
| Daily POI in discount/premium | 10 |
| Daily POI overlaps an HTF POI | 10 |
| 4H zone refines the POI | 10 |
| Inducement swept | 10 |
| Liquidity sweep at the POI (5 if only the inducement was taken) | 10 |
| LTF CHoCH (5 if only a BOS) | 10 |
| LTF OB/FVG entry zone | 5 |
| Timing: killzone (forex/indices/commodities), or 1H internal trend aligned (crypto/stocks) | 5 |

## Alignment matrix
For each timeframe: swing trend, internal trend, last break, and premium/discount position of price, plus whether that timeframe is aligned with the bias.
