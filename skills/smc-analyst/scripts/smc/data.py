"""Market data layer: symbol resolution, timeframes, OHLCV fetch, cache and market status.

Public API
    resolve_symbol(query)            -> Instrument     (offline, heuristic)
    validate_instrument(inst)        -> Instrument     (online: confirms the symbol, refines tick size)
    get_ohlcv(symbol, tf, limit)     -> pandas.DataFrame[open, high, low, close, volume] (UTC index)
    get_ladder(symbol, tfs, limit)   -> {tf: DataFrame}
    market_status(inst, now)         -> {"state": "open"|"closed"|"unknown", "reason", "next_open"}
    normalize_timeframe(tf)          -> canonical tf ("1M","1W","1D","4H","1H","15m","5m","1m")

Data sources (free, no API key)
    crypto       Binance public REST (via ccxt implicit endpoints, so no huge market list
                 is downloaded). Falls back to the data-api.binance.vision mirror, then Yahoo.
    stocks/forex/indices/commodities   Yahoo Finance via yfinance.

CLI (run from the scripts/ directory):
    python -m smc.data BTCUSDT --tf 1H --limit 5
    python -m smc.data EURUSD --resolve-only --json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd

from .config import cache_dir, get_config

log = logging.getLogger("smc.data")
T = TypeVar("T")
NY = ZoneInfo("America/New_York")
OHLCV = ["open", "high", "low", "close", "volume"]


# =============================================================================
# Errors
# =============================================================================

class DataError(RuntimeError):
    """Market data could not be fetched (network, provider or empty-data problem)."""


class SymbolError(DataError, ValueError):
    """The symbol is empty, malformed or unknown to the provider."""


# =============================================================================
# Timeframes
# =============================================================================

TIMEFRAMES: tuple[str, ...] = ("1M", "1W", "1D", "4H", "1H", "15m", "5m", "1m")
TF_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400,
    "1D": 86400, "1W": 604800, "1M": 2592000,  # 1M nominal 30 days; real bars use calendar months
}
_TF_ALIASES = {
    "1min": "1m", "m1": "1m", "1": "1m",
    "5min": "5m", "m5": "5m", "5": "5m",
    "15min": "15m", "m15": "15m", "15": "15m",
    "1h": "1H", "60m": "1H", "60min": "1H", "h1": "1H", "60": "1H", "hourly": "1H",
    "4h": "4H", "240m": "4H", "240min": "4H", "h4": "4H", "240": "4H",
    "1d": "1D", "d": "1D", "d1": "1D", "daily": "1D", "day": "1D",
    "1w": "1W", "w": "1W", "w1": "1W", "1wk": "1W", "weekly": "1W", "week": "1W",
    "1mo": "1M", "1mth": "1M", "1mon": "1M", "mn": "1M", "monthly": "1M", "month": "1M",
}


def normalize_timeframe(tf: str) -> str:
    """Map user/provider spellings to the canonical set.

    Case matters only for the exact strings ``1M`` (month) and ``1m`` (minute).
    """
    s = (tf or "").strip()
    if s in TIMEFRAMES:
        return s
    key = s.lower()
    if key in _TF_ALIASES:
        return _TF_ALIASES[key]
    raise ValueError(f"Unknown timeframe '{tf}'. Use one of: {', '.join(TIMEFRAMES)}")


def bar_close_time(open_time: pd.Timestamp, tf: str) -> pd.Timestamp:
    """Exclusive end of a bar that opened at ``open_time`` (calendar-aware for 1M)."""
    tf = normalize_timeframe(tf)
    if tf == "1M":
        return open_time + pd.DateOffset(months=1)
    return open_time + pd.Timedelta(seconds=TF_SECONDS[tf])


def is_bar_closed(open_time: pd.Timestamp, tf: str, now: datetime | None = None) -> bool:
    now_ts = pd.Timestamp(now or datetime.now(timezone.utc))
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    return bar_close_time(open_time, tf) <= now_ts


# =============================================================================
# Instruments and symbol resolution
# =============================================================================

@dataclass(frozen=True)
class Instrument:
    query: str              # what the user typed
    symbol: str             # canonical display symbol, e.g. BTCUSDT, EURUSD, AAPL, XAUUSD
    asset_class: str        # crypto | forex | stock | index | commodity
    provider: str           # binance | yahoo
    provider_symbol: str    # BTCUSDT (Binance) | EURUSD=X / AAPL / GC=F / ^GSPC (Yahoo)
    tick_size: float        # minimum price increment used for rounding
    session: str            # 24x7 | forex | futures | equity
    session_tz: str = "UTC"           # exchange timezone (equity sessions)
    session_hours: str = ""           # "09:30-16:00" (equity sessions)
    realtime: bool = False
    data_note: str = ""
    validated: bool = False           # True once confirmed online by validate_instrument()

    @property
    def price_precision(self) -> int:
        return tick_decimals(self.tick_size)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["price_precision"] = self.price_precision
        return d


FIAT = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK", "DKK", "SGD",
    "HKD", "CNH", "CNY", "MXN", "ZAR", "TRY", "PLN", "INR", "HUF", "CZK", "ILS", "KRW",
    "THB", "BRL",
}
CRYPTO_QUOTES = ("FDUSD", "USDT", "USDC", "TUSD", "BUSD", "BTC", "ETH", "BNB", "EUR", "TRY")
STABLE_QUOTES = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD"}
KNOWN_CRYPTO = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK", "POL", "MATIC",
    "LTC", "TRX", "TON", "SHIB", "BCH", "NEAR", "UNI", "ATOM", "XLM", "APT", "ARB", "OP",
    "SUI", "PEPE", "FIL", "INJ", "AAVE", "ETC", "HBAR", "ICP", "RENDER", "SEI", "TIA", "WIF",
    "BONK", "FET", "ALGO", "XMR", "TAO", "ENA", "JUP", "WLD", "STX", "IMX", "MKR", "RUNE",
}
# symbol -> (yahoo symbol, asset_class, tick, session, note)
ALIASES: dict[str, tuple[str, str, float, str, str]] = {
    "XAUUSD": ("GC=F", "commodity", 0.1, "futures", "Gold futures (COMEX GC) used as spot proxy"),
    "GOLD": ("GC=F", "commodity", 0.1, "futures", "Gold futures (COMEX GC) used as spot proxy"),
    "XAGUSD": ("SI=F", "commodity", 0.005, "futures", "Silver futures (COMEX SI) used as spot proxy"),
    "SILVER": ("SI=F", "commodity", 0.005, "futures", "Silver futures (COMEX SI) used as spot proxy"),
    "USOIL": ("CL=F", "commodity", 0.01, "futures", "WTI crude futures"),
    "WTI": ("CL=F", "commodity", 0.01, "futures", "WTI crude futures"),
    "UKOIL": ("BZ=F", "commodity", 0.01, "futures", "Brent crude futures"),
    "BRENT": ("BZ=F", "commodity", 0.01, "futures", "Brent crude futures"),
    "NATGAS": ("NG=F", "commodity", 0.001, "futures", "Natural gas futures"),
    "DXY": ("DX-Y.NYB", "index", 0.001, "futures", "US Dollar Index (ICE)"),
    "SPX": ("^GSPC", "index", 0.01, "equity", "S&P 500 index"),
    "SPX500": ("^GSPC", "index", 0.01, "equity", "S&P 500 index"),
    "US500": ("^GSPC", "index", 0.01, "equity", "S&P 500 index"),
    "NDX": ("^NDX", "index", 0.01, "equity", "Nasdaq-100 index"),
    "NAS100": ("^NDX", "index", 0.01, "equity", "Nasdaq-100 index"),
    "US100": ("^NDX", "index", 0.01, "equity", "Nasdaq-100 index"),
    "DJI": ("^DJI", "index", 0.01, "equity", "Dow Jones Industrial Average"),
    "US30": ("^DJI", "index", 0.01, "equity", "Dow Jones Industrial Average"),
    "VIX": ("^VIX", "index", 0.01, "equity", "CBOE Volatility Index"),
    "NIFTY": ("^NSEI", "index", 0.05, "equity", "NIFTY 50 index"),
    "NIFTY50": ("^NSEI", "index", 0.05, "equity", "NIFTY 50 index"),
    "BANKNIFTY": ("^NSEBANK", "index", 0.05, "equity", "NIFTY Bank index"),
    "SENSEX": ("^BSESN", "index", 0.01, "equity", "BSE SENSEX index"),
    "DAX": ("^GDAXI", "index", 0.01, "equity", "DAX 40 index"),
    "GER40": ("^GDAXI", "index", 0.01, "equity", "DAX 40 index"),
    "FTSE": ("^FTSE", "index", 0.01, "equity", "FTSE 100 index"),
    "UK100": ("^FTSE", "index", 0.01, "equity", "FTSE 100 index"),
    "NIKKEI": ("^N225", "index", 0.01, "equity", "Nikkei 225 index"),
    "JP225": ("^N225", "index", 0.01, "equity", "Nikkei 225 index"),
}
# Yahoo suffix / index -> (timezone, regular session). Holidays are not modelled.
EXCHANGE_SESSIONS: dict[str, tuple[str, str]] = {
    "": ("America/New_York", "09:30-16:00"),
    ".NS": ("Asia/Kolkata", "09:15-15:30"), ".BO": ("Asia/Kolkata", "09:15-15:30"),
    ".L": ("Europe/London", "08:00-16:30"), ".DE": ("Europe/Berlin", "09:00-17:30"),
    ".F": ("Europe/Berlin", "08:00-20:00"), ".PA": ("Europe/Paris", "09:00-17:30"),
    ".AS": ("Europe/Amsterdam", "09:00-17:30"), ".T": ("Asia/Tokyo", "09:00-15:30"),
    ".HK": ("Asia/Hong_Kong", "09:30-16:00"), ".TO": ("America/Toronto", "09:30-16:00"),
    ".AX": ("Australia/Sydney", "10:00-16:00"), ".SI": ("Asia/Singapore", "09:00-17:00"),
}
INDEX_SESSIONS = {
    "^GSPC": "", "^NDX": "", "^DJI": "", "^VIX": "", "^NSEI": ".NS", "^NSEBANK": ".NS",
    "^BSESN": ".BO", "^GDAXI": ".DE", "^FTSE": ".L", "^N225": ".T",
}

_STOCK_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-&]{0,14}$")
_PAIR_RE = re.compile(r"^([A-Z0-9]{2,10})[/\-_]([A-Z]{3,5})$")


def _crypto(query: str, base: str, quote: str) -> Instrument:
    if quote == "USD":
        quote = "USDT"  # Binance spot quotes in USDT, not USD
    sym = f"{base}{quote}"
    return Instrument(
        query=query, symbol=sym, asset_class="crypto", provider="binance", provider_symbol=sym,
        tick_size=0.01, session="24x7", realtime=True,
        data_note="Real-time (Binance public market data)",
    )


def _forex(query: str, base: str, quote: str) -> Instrument:
    tick = 0.001 if quote == "JPY" else 0.00001
    return Instrument(
        query=query, symbol=f"{base}{quote}", asset_class="forex", provider="yahoo",
        provider_symbol=f"{base}{quote}=X", tick_size=tick, session="forex",
        session_tz="America/New_York", realtime=False,
        data_note="Near real-time, polled from Yahoo Finance",
    )


def _equity_session(yahoo_symbol: str) -> tuple[str, str]:
    if yahoo_symbol in INDEX_SESSIONS:
        return EXCHANGE_SESSIONS[INDEX_SESSIONS[yahoo_symbol]]
    m = re.search(r"(\.[A-Z]{1,2})$", yahoo_symbol)
    suffix = m.group(1) if m else ""
    return EXCHANGE_SESSIONS.get(suffix, ("", ""))  # unknown exchange: hours unknown


def _yahoo(query: str, symbol: str, yahoo_symbol: str, asset_class: str, tick: float,
           session: str, note: str = "") -> Instrument:
    tz, hours = ("UTC", "")
    if session == "equity":
        tz, hours = _equity_session(yahoo_symbol)
        tz = tz or "UTC"
    elif session in ("forex", "futures"):
        tz = "America/New_York"
    default_note = {
        "stock": "Delayed ~1 min (Yahoo Finance, polled); some exchanges delay up to 15-20 min",
        "index": "Delayed (Yahoo Finance, polled)",
        "commodity": "Delayed (Yahoo Finance futures, polled)",
    }.get(asset_class, "Polled from Yahoo Finance")
    return Instrument(
        query=query, symbol=symbol, asset_class=asset_class, provider="yahoo",
        provider_symbol=yahoo_symbol, tick_size=tick, session=session, session_tz=tz,
        session_hours=hours, realtime=False, data_note=note or default_note,
    )


def resolve_symbol(query: str) -> Instrument:
    """Turn user input into an :class:`Instrument` (offline, no network calls).

    Examples: ``BTCUSDT``, ``btc/usdt``, ``BTC``, ``BINANCE:ETHUSDT`` → crypto;
    ``EURUSD``, ``EUR/USD``, ``EURUSD=X`` → forex; ``XAUUSD`` → gold futures;
    ``AAPL``, ``RELIANCE.NS``, ``BRK-B`` → stocks; ``SPX``, ``NAS100``, ``DXY`` → indices.
    """
    raw = (query or "").strip()
    if not raw:
        raise SymbolError("Empty symbol. Try something like BTCUSDT, EURUSD, XAUUSD or AAPL.")
    s = raw.upper().replace(" ", "")
    if ":" in s:  # TradingView-style "EXCHANGE:SYMBOL"
        s = s.split(":", 1)[1]
    if not s:
        raise SymbolError(f"Could not read a symbol from '{raw}'.")

    if s in ALIASES:
        ysym, cls, tick, session, note = ALIASES[s]
        return _yahoo(raw, s, ysym, cls, tick, session, note)

    # Yahoo-native spellings
    if s.endswith("=X"):
        pair = s[:-2]
        if len(pair) == 6 and pair[:3] in FIAT and pair[3:] in FIAT:
            return _forex(raw, pair[:3], pair[3:])
        raise SymbolError(f"'{raw}' looks like a Yahoo FX symbol but is not a known currency pair.")
    if s.endswith("=F"):
        return _yahoo(raw, s, s, "commodity", 0.01, "futures")
    if s.startswith("^"):
        return _yahoo(raw, s, s, "index", 0.01, "equity")

    # Separated pairs: BTC/USDT, BTC-USD, EUR/USD
    m = _PAIR_RE.match(s)
    if m:
        base, quote = m.groups()
        if base in FIAT and quote in FIAT:
            return _forex(raw, base, quote)
        if base in KNOWN_CRYPTO or quote in STABLE_QUOTES or (quote == "USD" and base not in FIAT):
            return _crypto(raw, base, quote)

    # Six-letter forex: EURUSD, USDJPY
    if len(s) == 6 and s[:3] in FIAT and s[3:] in FIAT:
        return _forex(raw, s[:3], s[3:])

    # Concatenated crypto: BTCUSDT, ETHBTC, PEPEFDUSD, BTCUSD
    for quote in CRYPTO_QUOTES:
        if s.endswith(quote) and len(s) > len(quote) + 1:
            base = s[: -len(quote)]
            if base in KNOWN_CRYPTO or quote in STABLE_QUOTES:
                return _crypto(raw, base, quote)
    if s.endswith("USD") and s[:-3] in KNOWN_CRYPTO:
        return _crypto(raw, s[:-3], "USDT")
    if s in KNOWN_CRYPTO:  # bare coin: "BTC" -> BTCUSDT
        return _crypto(raw, s, "USDT")

    if _STOCK_RE.match(s):
        cls = "stock"
        return _yahoo(raw, s, s, cls, 0.01, "equity")

    raise SymbolError(
        f"Could not recognise '{raw}'. Use a ticker like AAPL / RELIANCE.NS, a pair like "
        f"EURUSD, or a crypto pair like BTCUSDT."
    )


# =============================================================================
# Price rounding
# =============================================================================

def tick_decimals(tick: float) -> int:
    """Number of decimals implied by a tick size (0.00001 -> 5, 0.5 -> 1, 1 -> 0)."""
    exp = Decimal(str(tick)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def round_to_tick(value: float, tick: float) -> float:
    """Round to the nearest multiple of ``tick`` without float noise."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return value
    return round(round(value / tick) * tick, tick_decimals(tick))


def round_frame(df: pd.DataFrame, tick: float) -> pd.DataFrame:
    out = df.copy()
    dec = tick_decimals(tick)
    for col in ("open", "high", "low", "close"):
        out[col] = ((out[col] / tick).round() * tick).round(dec)
    return out


# =============================================================================
# Retry helper
# =============================================================================

def with_retries(fn: Callable[[], T], *, attempts: int = 4, base_delay: float = 1.5,
                 retry_on: tuple[type[BaseException], ...] = (Exception,),
                 no_retry: tuple[type[BaseException], ...] = (SymbolError,),
                 sleep: Callable[[float], None] = time.sleep) -> T:
    """Call ``fn`` with exponential backoff (1.5 s, 3 s, 6 s, …)."""
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except no_retry:
            raise
        except retry_on as exc:  # noqa: PERF203
            last = exc
            if i < attempts - 1:
                delay = base_delay * (2 ** i)
                log.warning("attempt %d/%d failed (%s); retrying in %.1fs", i + 1, attempts, exc, delay)
                sleep(delay)
    assert last is not None
    raise last


# =============================================================================
# Binance (via ccxt implicit public endpoints; no load_markets download)
# =============================================================================

BINANCE_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1h", "4H": "4h",
                    "1D": "1d", "1W": "1w", "1M": "1M"}
BINANCE_HOSTS = ("https://api.binance.com/api/v3", "https://data-api.binance.vision/api/v3")
BINANCE_MAX_LIMIT = 1000
_binance_clients: dict[str, object] = {}
_tick_cache: dict[str, float] = {}


def _binance_client(host: str):
    if host not in _binance_clients:
        import ccxt  # imported lazily: heavy module
        ex = ccxt.binance({"enableRateLimit": True, "timeout": 30000})
        ex.urls["api"]["public"] = host
        _binance_clients[host] = ex
    return _binance_clients[host]


def _binance_call(method: str, params: dict):
    """Call a public Binance endpoint, trying the main host then the mirror."""
    import ccxt
    last: BaseException | None = None
    for host in BINANCE_HOSTS:
        client = _binance_client(host)

        def call(client=client):
            return getattr(client, method)(params)

        try:
            return with_retries(call, attempts=3, retry_on=(ccxt.NetworkError,))
        except ccxt.BadSymbol as exc:
            raise SymbolError(f"Binance does not list '{params.get('symbol')}'.") from exc
        except ccxt.ExchangeError as exc:
            if "-1121" in str(exc) or "Invalid symbol" in str(exc):
                raise SymbolError(f"Binance does not list '{params.get('symbol')}'.") from exc
            last = exc  # e.g. region restriction (HTTP 451) -> try mirror
        except ccxt.BaseError as exc:
            last = exc
        log.warning("Binance host %s failed: %s", host, last)
    raise DataError(f"Binance unavailable: {last}")


def _binance_klines(symbol: str, tf: str, limit: int) -> pd.DataFrame:
    interval = BINANCE_INTERVAL[tf]
    rows: list[list] = []
    end_time: int | None = None
    remaining = limit
    while remaining > 0:
        params = {"symbol": symbol, "interval": interval, "limit": min(BINANCE_MAX_LIMIT, remaining)}
        if end_time is not None:
            params["endTime"] = end_time
        batch = _binance_call("public_get_klines", params)
        if not batch:
            break
        rows = batch + rows
        remaining -= len(batch)
        end_time = int(batch[0][0]) - 1
        if len(batch) < params["limit"]:
            break  # reached the start of the symbol's history
    if not rows:
        raise SymbolError(f"Binance returned no candles for {symbol} {tf}.")
    df = pd.DataFrame(
        [[int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in rows],
        columns=["time", *OHLCV],
    )
    df.index = pd.to_datetime(df.pop("time"), unit="ms", utc=True)
    df.index.name = "time"
    return df


def binance_tick_size(symbol: str) -> float:
    """Exact PRICE_FILTER tick size for a Binance spot symbol (also validates the symbol)."""
    if symbol not in _tick_cache:
        info = _binance_call("public_get_exchangeinfo", {"symbol": symbol})
        sym = (info.get("symbols") or [None])[0]
        if not sym:
            raise SymbolError(f"Binance does not list '{symbol}'.")
        tick = next((float(f["tickSize"]) for f in sym.get("filters", [])
                     if f.get("filterType") == "PRICE_FILTER"), 0.01)
        _tick_cache[symbol] = tick
    return _tick_cache[symbol]


# =============================================================================
# Yahoo Finance (yfinance)
# =============================================================================

YF_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1h", "4H": "1h",
               "1D": "1d", "1W": "1wk", "1M": "1mo"}
# Longest history Yahoo serves per interval (1m: 7 days per request; 5m/15m: 60 days; 1h: 730 days)
YF_MAX_PERIOD = {"1m": "7d", "5m": "60d", "15m": "60d", "1h": "730d",
                 "1d": "max", "1wk": "max", "1mo": "max"}
YF_RECENT_PERIOD = {"1m": "1d", "5m": "5d", "15m": "5d", "1H": "5d", "4H": "1mo",
                    "1D": "1mo", "1W": "6mo", "1M": "2y"}


def _yf_period_for(tf: str, limit: int) -> str:
    interval = YF_INTERVAL[tf]
    if interval == "1d":
        return "5y" if limit <= 1000 else "max"
    return YF_MAX_PERIOD[interval]


_yf_configured = False


def _configure_yfinance(yf) -> None:
    """Make yfinance raise instead of returning empty frames, and stop it logging
    errors that we already handle and report ourselves."""
    global _yf_configured
    if _yf_configured:
        return
    try:
        yf.config.debug.hide_exceptions = False  # yfinance >= 1.x replacement for raise_errors=True
    except AttributeError:  # pragma: no cover - older yfinance
        pass
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    _yf_configured = True


def _yf_history(yahoo_symbol: str, interval: str, period: str) -> pd.DataFrame:
    """Raw yfinance download with backoff on rate limits / transient errors."""
    import warnings

    import yfinance as yf
    from yfinance import exceptions as yfe

    _configure_yfinance(yf)

    def call():
        with warnings.catch_warnings():  # silence yfinance-internal pandas deprecation noise
            warnings.simplefilter("ignore", FutureWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            return yf.Ticker(yahoo_symbol).history(
                period=period, interval=interval, auto_adjust=False, actions=False, prepost=False,
            )

    # Retry only transient failures. curl_cffi's HTTPError subclasses OSError, so OSError
    # must NOT be retried wholesale (a 404 for a bad symbol would be retried 4 times).
    retry_on: tuple = (yfe.YFRateLimitError, ConnectionError, TimeoutError)
    try:
        from curl_cffi.requests import exceptions as cce
        retry_on += (cce.ConnectionError, cce.Timeout)
    except ImportError:  # pragma: no cover
        pass
    missing = (yfe.YFTickerMissingError, yfe.YFPricesMissingError, yfe.YFTzMissingError)
    try:
        return with_retries(call, attempts=4, base_delay=2.0, retry_on=retry_on)
    except missing as exc:
        raise SymbolError(f"Yahoo Finance has no data for '{yahoo_symbol}'. Check the symbol.") from exc
    except retry_on as exc:
        raise DataError(f"Yahoo Finance unavailable for {yahoo_symbol}: {exc}") from exc
    except Exception as exc:  # yfinance raises assorted errors for bad symbols
        msg = str(exc)
        if any(k in msg.lower() for k in ("delisted", "no data", "not found", "no timezone",
                                          "symbol may be", "404")):
            raise SymbolError(f"Yahoo Finance has no data for '{yahoo_symbol}'. Check the symbol.") from exc
        raise DataError(f"Yahoo Finance error for {yahoo_symbol}: {msg}") from exc


def normalize_yahoo_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """yfinance frame -> canonical OHLCV with a UTC index, sorted and de-duplicated."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=OHLCV, index=pd.DatetimeIndex([], tz="UTC", name="time"))
    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):  # yf.download style
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower() for c in df.columns]
    missing = [c for c in OHLCV if c not in df.columns]
    if "volume" in missing:
        df["volume"] = 0.0
        missing.remove("volume")
    if missing:
        raise DataError(f"Yahoo frame missing columns: {missing}")
    df = df[OHLCV].astype(float)
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    df.index = idx
    df.index.name = "time"
    df = df.dropna(subset=["open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _session_offset(local_index: pd.DatetimeIndex) -> pd.Timedelta:
    """Offset so 4H bins start at the session open (e.g. 09:30 → bins 09:30, 13:30)."""
    first_bars = pd.Series(local_index, index=local_index).groupby(local_index.date).min()
    tod = first_bars.map(lambda t: pd.Timedelta(hours=t.hour, minutes=t.minute)).mode()
    open_td = tod.iloc[0] if not tod.empty else pd.Timedelta(0)
    return open_td % pd.Timedelta(hours=4)


def resample_ohlcv(df: pd.DataFrame, tf: str, session_tz: str | None = None) -> pd.DataFrame:
    """Aggregate lower-timeframe bars into ``tf`` bars.

    For 24-hour markets (``session_tz`` None), bins align to UTC midnight (00, 04, 08 …).
    For session markets, bins align to the regular session open in the exchange's timezone.
    """
    rule = {"4H": "4h", "1H": "1h", "1D": "1D"}.get(tf)
    if rule is None:
        raise ValueError(f"resampling to {tf} is not supported")
    if df.empty:
        return df.copy()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if session_tz:
        local = df.tz_convert(session_tz)
        offset = _session_offset(local.index)
        out = local.resample(rule, offset=offset, label="left", closed="left").agg(agg)
        out.index = out.index.tz_convert("UTC")
    else:
        out = df.resample(rule, origin="epoch", label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out.index.name = "time"
    return out


def _yahoo_ohlcv(inst: Instrument, tf: str, limit: int, recent: bool = False) -> pd.DataFrame:
    interval = YF_INTERVAL[tf]
    period = YF_RECENT_PERIOD[tf] if recent else _yf_period_for(tf, limit)
    df = normalize_yahoo_frame(_yf_history(inst.provider_symbol, interval, period))
    if df.empty:
        raise SymbolError(f"Yahoo Finance returned no candles for '{inst.provider_symbol}' ({tf}).")
    if tf == "4H":
        tz = inst.session_tz if inst.session == "equity" else None
        df = resample_ohlcv(df, "4H", session_tz=tz)
    return df


# =============================================================================
# Cache
# =============================================================================

def cache_ttl_seconds(tf: str, cfg: dict | None = None) -> int:
    """How long a cached frame stays fresh: tf/12, clamped to 15 s … 1 h (unless configured)."""
    cfg = cfg or get_config()
    configured = cfg.get("data", {}).get("cache_ttl_seconds")
    if configured is not None:
        return int(configured)
    return int(min(3600, max(15, TF_SECONDS[tf] / 12)))


def _cache_path(inst: Instrument, tf: str) -> Path:
    key = re.sub(r"[^A-Za-z0-9]+", "_", f"{inst.provider}_{inst.provider_symbol}_{tf}").strip("_")
    return cache_dir() / f"{key}.csv"


def _cache_read(path: Path, tf: str, limit: int) -> pd.DataFrame | None:
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age > cache_ttl_seconds(tf):
        return None
    try:
        df = pd.read_csv(path, index_col="time")
        df.index = pd.to_datetime(df.index, utc=True)
    except (OSError, ValueError, KeyError):
        return None
    if len(df) < limit:
        return None
    return df[OHLCV].astype(float)


def _cache_write(path: Path, df: pd.DataFrame) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        out = df.copy()
        out.index = out.index.strftime("%Y-%m-%dT%H:%M:%SZ")
        out.index.name = "time"
        out.to_csv(tmp)
        os.replace(tmp, path)
    except OSError as exc:  # caching is best-effort
        log.warning("could not write cache %s: %s", path, exc)


# =============================================================================
# Public fetch API
# =============================================================================

def validate_instrument(inst: Instrument) -> Instrument:
    """Confirm the symbol exists online and refine its tick size (crypto: exact exchange tick).

    Yahoo symbols are validated implicitly by the first fetch.
    """
    if inst.validated:
        return inst
    if inst.provider == "binance":
        return replace(inst, tick_size=binance_tick_size(inst.provider_symbol), validated=True)
    return inst


def _yahoo_fallback_for(inst: Instrument) -> Instrument | None:
    if inst.provider != "binance":
        return None
    for q in ("USDT", "USDC", "FDUSD", "TUSD", "BUSD"):
        if inst.symbol.endswith(q):
            base = inst.symbol[: -len(q)]
            return replace(inst, provider="yahoo", provider_symbol=f"{base}-USD", realtime=False,
                           data_note="Binance unreachable: delayed fallback from Yahoo Finance")
    return None


def get_ohlcv(symbol: str | Instrument, tf: str, limit: int | None = None, *,
              use_cache: bool = True, round_prices: bool = True) -> pd.DataFrame:
    """Fetch the last ``limit`` candles for ``symbol`` on timeframe ``tf``.

    Returns a DataFrame indexed by bar **open** time (UTC) with float columns
    ``open, high, low, close, volume``. The last row may be the still-forming bar;
    ``df.attrs["last_bar_closed"]`` says which. ``df.attrs`` also carries the instrument,
    provider, timeframe and any fallback notes.

    Raises :class:`SymbolError` for unknown symbols and :class:`DataError` for outages.
    """
    tf = normalize_timeframe(tf)
    inst = symbol if isinstance(symbol, Instrument) else resolve_symbol(symbol)
    limit = int(limit or get_config()["timeframes"]["history_bars"])
    if limit < 1:
        raise ValueError("limit must be >= 1")

    if inst.provider == "binance":
        try:
            inst = validate_instrument(inst)
        except DataError as exc:
            if isinstance(exc, SymbolError):
                raise
            fb = _yahoo_fallback_for(inst)
            if fb is None:
                raise
            log.warning("Binance unavailable (%s); falling back to Yahoo %s", exc, fb.provider_symbol)
            inst = fb

    path = _cache_path(inst, tf)
    df = _cache_read(path, tf, limit) if use_cache else None
    from_cache = df is not None
    if df is None:
        if inst.provider == "binance":
            df = _binance_klines(inst.provider_symbol, tf, limit)
        else:
            df = _yahoo_ohlcv(inst, tf, limit)
        if use_cache:
            _cache_write(path, df)

    df = df.tail(limit).copy()
    if round_prices:
        df = round_frame(df, inst.tick_size)
    last_open = df.index[-1]
    df.attrs.update({
        "instrument": inst.to_dict(),
        "timeframe": tf,
        "provider": inst.provider,
        "from_cache": from_cache,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_bar_closed": bool(is_bar_closed(last_open, tf)),
        "bars": len(df),
    })
    if len(df) < limit:
        df.attrs["note"] = (f"Only {len(df)} of {limit} requested bars available "
                            f"({inst.provider} history limit for {tf}).")
    return df


def get_ladder(symbol: str | Instrument, tfs: Iterable[str] | None = None,
               limit: int | None = None, **kw) -> dict[str, pd.DataFrame]:
    """Fetch several timeframes (default: the configured top-down ladder)."""
    inst = symbol if isinstance(symbol, Instrument) else resolve_symbol(symbol)
    tfs = list(tfs or get_config()["timeframes"]["ladder"])
    return {normalize_timeframe(tf): get_ohlcv(inst, tf, limit, **kw) for tf in tfs}


def fetch_recent(inst: Instrument, tf: str, bars: int = 3) -> pd.DataFrame:
    """Latest few bars, bypassing the cache (used by the live pollers)."""
    tf = normalize_timeframe(tf)
    if inst.provider == "binance":
        df = _binance_klines(inst.provider_symbol, tf, bars)
    else:
        df = _yahoo_ohlcv(inst, tf, bars, recent=True)
    return round_frame(df.tail(bars), inst.tick_size)


# =============================================================================
# Market hours
# =============================================================================

def _parse_hours(hours: str) -> tuple[dtime, dtime]:
    a, b = hours.split("-")
    return dtime.fromisoformat(a), dtime.fromisoformat(b)


def _is_open_at(inst: Instrument, t: datetime) -> bool | None:
    """Open/closed at aware datetime ``t``; None when the session is unknown."""
    if inst.session == "24x7":
        return True
    if inst.session == "forex":  # Sun 17:00 → Fri 17:00 New York
        ny = t.astimezone(NY)
        wd, hm = ny.weekday(), ny.time()
        if wd == 5:
            return False
        if wd == 6:
            return hm >= dtime(17, 0)
        if wd == 4:
            return hm < dtime(17, 0)
        return True
    if inst.session == "futures":  # Sun 18:00 → Fri 17:00 NY, daily 17:00–18:00 break
        ny = t.astimezone(NY)
        wd, hm = ny.weekday(), ny.time()
        if wd == 5:
            return False
        if wd == 6:
            return hm >= dtime(18, 0)
        if wd == 4 and hm >= dtime(17, 0):
            return False
        return not (dtime(17, 0) <= hm < dtime(18, 0))
    if inst.session == "equity":
        if not inst.session_hours:
            return None
        local = t.astimezone(ZoneInfo(inst.session_tz))
        if local.weekday() >= 5:
            return False
        start, end = _parse_hours(inst.session_hours)
        return start <= local.time() < end
    return None


def market_status(inst: Instrument, now: datetime | None = None) -> dict:
    """``{"state": open|closed|unknown, "reason": str, "next_open": iso-UTC|None}``.

    Exchange holidays are not modelled; a holiday shows as "open" with no new candles.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    is_open = _is_open_at(inst, now)
    if is_open is None:
        return {"state": "unknown", "reason": "exchange hours unknown for this symbol", "next_open": None}
    if is_open:
        reason = "24/7 market" if inst.session == "24x7" else "within trading session"
        return {"state": "open", "reason": reason, "next_open": None}
    # step forward in 5-minute increments (max 4 days) to find the next open
    t = now.replace(second=0, microsecond=0)
    t += timedelta(minutes=5 - t.minute % 5)
    for _ in range(4 * 24 * 12):
        if _is_open_at(inst, t):
            return {"state": "closed", "reason": "outside trading session",
                    "next_open": t.astimezone(timezone.utc).isoformat(timespec="minutes")}
        t += timedelta(minutes=5)
    return {"state": "closed", "reason": "outside trading session", "next_open": None}


# =============================================================================
# CLI
# =============================================================================

def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m smc.data", description="Fetch OHLCV candles.")
    ap.add_argument("symbol")
    ap.add_argument("--tf", default="1H", help="timeframe (1M 1W 1D 4H 1H 15m 5m 1m)")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--resolve-only", action="store_true", help="only resolve the symbol (offline)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    try:
        inst = resolve_symbol(args.symbol)
        if args.resolve_only:
            out = {"instrument": inst.to_dict(), "market": market_status(inst)}
            print(json.dumps(out, indent=None if args.json else 2))
            return 0
        df = get_ohlcv(inst, args.tf, args.limit, use_cache=not args.no_cache)
    except (DataError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}))
        return 1

    inst_d = df.attrs["instrument"]
    meta = {k: v for k, v in df.attrs.items() if k != "instrument"}
    if args.json:
        candles = [{"time": int(ts.timestamp()), **{c: row[c] for c in OHLCV}}
                   for ts, row in df.iterrows()]
        print(json.dumps({"status": "ok", "instrument": inst_d,
                          "market": market_status(resolve_symbol(args.symbol)),
                          "meta": meta, "candles": candles}))
    else:
        print(f"{inst_d['symbol']} [{inst_d['asset_class']}] via {inst_d['provider']} "
              f"({inst_d['provider_symbol']}), tick {inst_d['tick_size']} | {inst_d['data_note']}")
        print(f"market: {market_status(resolve_symbol(args.symbol))}")
        print(f"meta: {meta}")
        with pd.option_context("display.width", 120, "display.max_columns", 10):
            print(df)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
