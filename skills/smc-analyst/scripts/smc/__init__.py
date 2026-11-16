"""smc-analyst engine package.

Phase 2 provides the data layer:
    config      load defaults + user overrides
    data        symbol resolution, timeframes, OHLCV fetch, cache, market status
    feeds       live candle streams (Binance WebSocket, Yahoo polling)
    indicators  ATR, displacement, returns / correlation / beta

Phase 3 adds the SMC engine:
    swings      fractal pivots (swing + internal)
    structure   BOS / CHoCH, trend state
    liquidity   BSL/SSL, equal highs/lows, PDH/PDL/PWH/PWL, sweeps
    zones       order blocks, FVGs, supply/demand, mitigation state
    pd_array    dealing range, premium/discount, OTE
    inducement  bait liquidity in front of a POI
    sessions    ICT killzones
    mtf         per-timeframe analysis + the 5-stage top-down cascade

Phase 4 adds trade planning and tracking:
    signal      entry / SL / TP1-3 / R:R, position sizing, opposite scenario, correlated hedge
    tracker     signal state machine, JSON journal, catch-up replay, hit rate / average R
"""

__version__ = "1.0.0"
