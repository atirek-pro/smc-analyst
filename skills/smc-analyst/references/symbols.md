# Symbols: accepted formats and hedge partners

## Accepted symbol formats (`smc/data.py` → `resolve_symbol`)
| Asset class | Examples you can type | Data source | Notes |
|---|---|---|---|
| Crypto | `BTCUSDT`, `btc/usdt`, `BTC-USD`, `BTCUSD`, `BTC`, `BINANCE:ETHUSDT`, `ETHBTC` | Binance (real-time) | USD is mapped to USDT; falls back to Yahoo `BTC-USD` if Binance is unreachable |
| Forex | `EURUSD`, `eur/usd`, `FX:GBPJPY`, `USDJPY=X` | Yahoo `EURUSD=X` (polled) | Tick 0.00001; 0.001 for JPY pairs |
| Metals / energy | `XAUUSD`/`GOLD`, `XAGUSD`/`SILVER`, `USOIL`/`WTI`, `UKOIL`/`BRENT`, `NATGAS`, `CL=F` | Yahoo futures (`GC=F`, `SI=F`, `CL=F`, `BZ=F`, `NG=F`) | Futures stand in for spot |
| Indices | `SPX`, `NAS100`, `US30`, `DXY`, `VIX`, `NIFTY`, `BANKNIFTY`, `SENSEX`, `DAX`, `FTSE`, `NIKKEI`, `^GSPC` | Yahoo | |
| Stocks | `AAPL`, `NASDAQ:aapl`, `RELIANCE.NS`, `BRK-B`, `SHEL.L` | Yahoo | Session from the suffix (`.NS` IST, `.L` London, …) |

## Correlated-hedge partners (`smc/signal.py` → `CORRELATION_MAP`)
For each symbol, the engine fetches the partners' daily closes and computes a 30-day correlation and beta on aligned daily returns. It picks the partner with the highest |correlation|.
- **Suppressed** if |corr| < `hedge.min_abs_correlation` (0.5).
- **Direction:** a long position with a positively correlated partner is hedged by *shorting* the partner. Negative correlation means going long the partner. Short positions are the mirror image.
- **Size:** hedge notional = |beta| × position notional (the minimum-variance hedge ratio). Effectiveness ≈ corr², the share of variance offset.

| Symbol | Hedge partners (priority order) |
|---|---|
| `EURUSD` | `DXY`, `USDCHF`, `GBPUSD` |
| `GBPUSD` | `EURUSD`, `DXY` |
| `AUDUSD` | `NZDUSD`, `DXY` |
| `NZDUSD` | `AUDUSD`, `DXY` |
| `USDJPY` | `DXY`, `EURJPY` |
| `USDCHF` | `EURUSD`, `DXY` |
| `USDCAD` | `USOIL`, `DXY` |
| `EURJPY` | `GBPJPY`, `USDJPY` |
| `GBPJPY` | `EURJPY`, `USDJPY` |
| `XAUUSD` | `DXY`, `XAGUSD` |
| `XAGUSD` | `XAUUSD`, `DXY` |
| `USOIL` | `UKOIL`, `USDCAD` |
| `UKOIL` | `USOIL` |
| `BTCUSDT` | `ETHUSDT` |
| `ETHUSDT` | `BTCUSDT` |
| `SOLUSDT` | `ETHUSDT`, `BTCUSDT` |
| `SPX` | `NAS100`, `US30` |
| `NAS100` | `SPX` |
| `US30` | `SPX` |
| `DXY` | `EURUSD` |
| `AAPL` | `QQQ`, `SPY` |
| `MSFT` | `QQQ`, `SPY` |
| `NVDA` | `QQQ`, `SPY` |
| `AMZN` | `QQQ`, `SPY` |
| `GOOGL` | `QQQ`, `SPY` |
| `META` | `QQQ`, `SPY` |
| `TSLA` | `QQQ`, `SPY` |

### Defaults for unmapped symbols
| Asset class | Partners |
|---|---|
| crypto | `BTCUSDT`, `ETHUSDT` (excluding itself) |
| forex | `DXY` |
| commodity | `DXY` |
| index | `SPX` |
| stock (US / other) | `SPY` |
| stock (`.NS` / `.BO`) | `NIFTY` |
