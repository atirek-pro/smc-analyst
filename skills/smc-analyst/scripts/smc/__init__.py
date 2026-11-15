"""smc-analyst engine package.

Phase 2 provides the data layer:
    config      load defaults + user overrides
    data        symbol resolution, timeframes, OHLCV fetch, cache, market status
    feeds       live candle streams (Binance WebSocket, Yahoo polling)
    indicators  ATR, displacement, returns / correlation / beta
"""

__version__ = "0.2.0"
