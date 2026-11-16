"""Phase 2: data layer (offline; network calls are monkeypatched)."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import pandas as pd
import pytest

from smc import data
from smc.data import DataError, SymbolError

# --- timeframes -----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1m", "1m"), ("1M", "1M"), ("1min", "1m"), ("1mo", "1M"), ("monthly", "1M"),
    ("1h", "1H"), ("60m", "1H"), ("H4", "4H"), ("240", "4H"), ("1d", "1D"), ("daily", "1D"),
    ("1wk", "1W"), ("W", "1W"), (" 15m ", "15m"), ("5min", "5m"),
])
def test_normalize_timeframe(raw, expected):
    assert data.normalize_timeframe(raw) == expected


def test_normalize_timeframe_rejects_unknown():
    with pytest.raises(ValueError):
        data.normalize_timeframe("7h")


def test_bar_close_time_month_is_calendar_aware():
    t = pd.Timestamp("2026-02-01", tz="UTC")
    assert data.bar_close_time(t, "1M") == pd.Timestamp("2026-03-01", tz="UTC")
    assert data.bar_close_time(t, "4H") == pd.Timestamp("2026-02-01 04:00", tz="UTC")


def test_is_bar_closed():
    t = pd.Timestamp("2026-01-01 10:00", tz="UTC")
    assert not data.is_bar_closed(t, "1H", datetime(2026, 1, 1, 10, 59, tzinfo=UTC))
    assert data.is_bar_closed(t, "1H", datetime(2026, 1, 1, 11, 0, tzinfo=UTC))


# --- symbol resolution -----------------------------------------------------------

@pytest.mark.parametrize("query,symbol,cls,provider,psym", [
    ("BTCUSDT", "BTCUSDT", "crypto", "binance", "BTCUSDT"),
    ("btc/usdt", "BTCUSDT", "crypto", "binance", "BTCUSDT"),
    ("BTC-USD", "BTCUSDT", "crypto", "binance", "BTCUSDT"),
    ("BTCUSD", "BTCUSDT", "crypto", "binance", "BTCUSDT"),
    ("BTC", "BTCUSDT", "crypto", "binance", "BTCUSDT"),
    ("BINANCE:ETHUSDT", "ETHUSDT", "crypto", "binance", "ETHUSDT"),
    ("ETHBTC", "ETHBTC", "crypto", "binance", "ETHBTC"),
    ("NEWCOINUSDT", "NEWCOINUSDT", "crypto", "binance", "NEWCOINUSDT"),
    ("EURUSD", "EURUSD", "forex", "yahoo", "EURUSD=X"),
    ("eur/usd", "EURUSD", "forex", "yahoo", "EURUSD=X"),
    ("FX:GBPJPY", "GBPJPY", "forex", "yahoo", "GBPJPY=X"),
    ("USDJPY=X", "USDJPY", "forex", "yahoo", "USDJPY=X"),
    ("XAUUSD", "XAUUSD", "commodity", "yahoo", "GC=F"),
    ("CL=F", "CL=F", "commodity", "yahoo", "CL=F"),
    ("NAS100", "NAS100", "index", "yahoo", "^NDX"),
    ("DXY", "DXY", "index", "yahoo", "DX-Y.NYB"),
    ("^GSPC", "^GSPC", "index", "yahoo", "^GSPC"),
    ("AAPL", "AAPL", "stock", "yahoo", "AAPL"),
    ("NASDAQ:aapl", "AAPL", "stock", "yahoo", "AAPL"),
    ("RELIANCE.NS", "RELIANCE.NS", "stock", "yahoo", "RELIANCE.NS"),
    ("BRK-B", "BRK-B", "stock", "yahoo", "BRK-B"),
])
def test_resolve_symbol(query, symbol, cls, provider, psym):
    inst = data.resolve_symbol(query)
    assert (inst.symbol, inst.asset_class, inst.provider, inst.provider_symbol) == \
        (symbol, cls, provider, psym)
    assert inst.query == query


@pytest.mark.parametrize("query", ["", "   ", "!!!", "ABCDEF=X", "EXCHANGE:"])
def test_resolve_symbol_errors(query):
    with pytest.raises(SymbolError):
        data.resolve_symbol(query)


def test_forex_tick_sizes_and_sessions():
    assert data.resolve_symbol("EURUSD").tick_size == 0.00001
    assert data.resolve_symbol("USDJPY").tick_size == 0.001
    assert data.resolve_symbol("EURUSD").session == "forex"
    aapl = data.resolve_symbol("AAPL")
    assert (aapl.session_tz, aapl.session_hours) == ("America/New_York", "09:30-16:00")
    rel = data.resolve_symbol("RELIANCE.NS")
    assert rel.session_tz == "Asia/Kolkata"
    assert data.resolve_symbol("BTCUSDT").realtime is True


# --- rounding ----------------------------------------------------------------------

@pytest.mark.parametrize("tick,dec", [(0.00001, 5), (0.001, 3), (0.01, 2), (0.1, 1),
                                      (0.5, 1), (1, 0), (0.005, 3), (1e-8, 8)])
def test_tick_decimals(tick, dec):
    assert data.tick_decimals(tick) == dec


@pytest.mark.parametrize("value,tick,expected", [
    (1.0834499597549438, 0.00001, 1.08345), (3885.27, 0.1, 3885.3),
    (24.1234, 0.005, 24.125), (64261.515, 0.01, 64261.52), (149.9996, 0.001, 150.0),
])
def test_round_to_tick(value, tick, expected):
    assert data.round_to_tick(value, tick) == expected


# --- frame normalisation and resampling ------------------------------------------------

def test_normalize_yahoo_frame():
    idx = pd.DatetimeIndex(["2026-03-02 09:30", "2026-03-02 10:30", "2026-03-02 10:30",
                            "2026-03-02 08:30"], tz="America/New_York")
    raw = pd.DataFrame({"Open": [1, 2, 2.5, 0.5], "High": [2, 3, 3.5, 1], "Low": [0.5, 1, 1, 0],
                        "Close": [1.5, 2.5, 3, None], "Volume": [10, 20, 30, 5],
                        "Adj Close": [1, 1, 1, 1]}, index=idx)
    df = data.normalize_yahoo_frame(raw)
    assert list(df.columns) == data.OHLCV
    assert str(df.index.tz) == "UTC"
    assert len(df) == 2  # NaN row dropped, duplicate collapsed (keep last)
    assert df.iloc[-1]["close"] == 3
    assert df.index.is_monotonic_increasing


def test_normalize_yahoo_frame_fx_without_volume():
    idx = pd.date_range("2026-03-02", periods=2, freq="1h", tz="UTC")
    raw = pd.DataFrame({"Open": [1, 2], "High": [2, 3], "Low": [0, 1], "Close": [1, 2]}, index=idx)
    assert (data.normalize_yahoo_frame(raw)["volume"] == 0).all()


def hourly(start, n, tz="UTC"):
    idx = pd.date_range(start, periods=n, freq="1h", tz=tz)
    vals = range(1, n + 1)
    return pd.DataFrame({"open": vals, "high": [v + 0.5 for v in vals], "low": [v - 0.5 for v in vals],
                         "close": [v + 0.25 for v in vals], "volume": [1.0] * n}, index=idx).astype(float)


def test_resample_4h_24h_market_aligns_to_utc_epoch():
    df = hourly("2026-03-02 01:00", 9)  # 01:00 .. 09:00
    out = data.resample_ohlcv(df, "4H")
    assert [t.hour for t in out.index] == [0, 4, 8]
    first = out.iloc[0]  # bars 01,02,03
    assert (first.open, first.high, first.low, first.close, first.volume) == (1, 3.5, 0.5, 3.25, 3)


def test_resample_4h_equity_aligns_to_session_open():
    # Two US sessions of 7 hourly bars starting 09:30 New York.
    d1 = hourly("2026-03-02 09:30", 7, tz="America/New_York")
    d2 = hourly("2026-03-03 09:30", 7, tz="America/New_York")
    df = pd.concat([d1, d2]).tz_convert("UTC")
    out = data.resample_ohlcv(df, "4H", session_tz="America/New_York")
    local = out.index.tz_convert("America/New_York")
    assert [t.strftime("%H:%M") for t in local] == ["09:30", "13:30", "09:30", "13:30"]
    assert out.iloc[0].volume == 4 and out.iloc[1].volume == 3


# --- retries ------------------------------------------------------------------------

def test_with_retries_backoff_then_success():
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("boom")
        return "ok"

    assert data.with_retries(flaky, attempts=4, base_delay=1, retry_on=(ConnectionError,),
                             sleep=sleeps.append) == "ok"
    assert sleeps == [1, 2]


def test_with_retries_does_not_retry_symbol_errors():
    calls = []

    def bad():
        calls.append(1)
        raise SymbolError("nope")

    with pytest.raises(SymbolError):
        data.with_retries(bad, sleep=lambda s: None)
    assert len(calls) == 1


# --- Binance pagination ----------------------------------------------------------------

def test_binance_klines_paginates_backwards(monkeypatch):
    hour = 3_600_000
    now = 1_800_000_000_000
    calls = []

    def fake_call(method, params):
        # Like Binance: return the `limit` aligned bars whose open time is <= endTime.
        calls.append(params)
        last_open = (params.get("endTime", now) // hour) * hour
        n = params["limit"]
        return [[last_open - (n - 1 - i) * hour, "1", "2", "0.5", "1.5", "10"] for i in range(n)]

    monkeypatch.setattr(data, "_binance_call", fake_call)
    df = data._binance_klines("BTCUSDT", "1H", 2500)
    assert len(df) == 2500 and len(calls) == 3
    assert [c["limit"] for c in calls] == [1000, 1000, 500]
    assert df.index.is_monotonic_increasing and df.index.is_unique
    assert (df.index.to_series().diff().dropna() == pd.Timedelta(hours=1)).all()


def test_binance_klines_stops_at_history_start(monkeypatch):
    calls = []

    def fake_call(method, params):
        calls.append(params)
        # A young listing: only 10 bars exist, fewer than the 500 requested.
        return [[i * 3_600_000, "1", "2", "0.5", "1.5", "1"] for i in range(10)]

    monkeypatch.setattr(data, "_binance_call", fake_call)
    df = data._binance_klines("NEWUSDT", "1H", 500)
    assert len(df) == 10 and len(calls) == 1


# --- get_ohlcv orchestration, cache and fallback ---------------------------------------

def fake_frame(n=10, start="2026-03-02", freq="1h"):
    idx = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="time")
    return pd.DataFrame({"open": 100.123456, "high": 101.987654, "low": 99.5, "close": 100.555555,
                         "volume": 1.0}, index=idx)


def test_get_ohlcv_binance_rounds_tails_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "binance_tick_size", lambda s: 0.01)
    monkeypatch.setattr(data, "_binance_klines", lambda s, tf, lim: calls.append(1) or fake_frame(lim))
    df = data.get_ohlcv("BTCUSDT", "1h", limit=5)
    assert len(df) == 5 and df["open"].iloc[0] == 100.12 and df["high"].iloc[0] == 101.99
    assert df.attrs["timeframe"] == "1H" and df.attrs["provider"] == "binance"
    assert df.attrs["instrument"]["validated"] is True and df.attrs["from_cache"] is False
    df2 = data.get_ohlcv("BTCUSDT", "1H", limit=5)
    assert df2.attrs["from_cache"] is True and len(calls) == 1
    assert df2.equals(df)


def test_get_ohlcv_cache_bypass_and_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "binance_tick_size", lambda s: 0.01)
    monkeypatch.setattr(data, "_binance_klines", lambda s, tf, lim: calls.append(1) or fake_frame(lim))
    data.get_ohlcv("BTCUSDT", "1H", limit=5)
    data.get_ohlcv("BTCUSDT", "1H", limit=5, use_cache=False)
    assert len(calls) == 2
    path = data._cache_path(data.resolve_symbol("BTCUSDT"), "1H")
    old = time.time() - 10_000
    os.utime(path, (old, old))  # make it stale
    data.get_ohlcv("BTCUSDT", "1H", limit=5)
    assert len(calls) == 3


def test_cache_miss_when_too_few_rows(monkeypatch):
    monkeypatch.setattr(data, "binance_tick_size", lambda s: 0.01)
    monkeypatch.setattr(data, "_binance_klines", lambda s, tf, lim: fake_frame(lim))
    data.get_ohlcv("BTCUSDT", "1H", limit=5)
    df = data.get_ohlcv("BTCUSDT", "1H", limit=8)
    assert df.attrs["from_cache"] is False and len(df) == 8


def test_get_ohlcv_falls_back_to_yahoo_when_binance_down(monkeypatch):
    def down(symbol):
        raise DataError("Binance unavailable: 451")

    seen = {}

    def fake_yahoo(inst, tf, limit, recent=False):
        seen["symbol"] = inst.provider_symbol
        return fake_frame(limit)

    monkeypatch.setattr(data, "binance_tick_size", down)
    monkeypatch.setattr(data, "_yahoo_ohlcv", fake_yahoo)
    df = data.get_ohlcv("ETHUSDT", "1H", limit=3)
    assert seen["symbol"] == "ETH-USD"
    assert df.attrs["provider"] == "yahoo" and "fallback" in df.attrs["instrument"]["data_note"]


def test_get_ohlcv_symbol_error_is_not_masked_by_fallback(monkeypatch):
    def bad(symbol):
        raise SymbolError("Binance does not list it")

    monkeypatch.setattr(data, "binance_tick_size", bad)
    with pytest.raises(SymbolError):
        data.get_ohlcv("FAKECOINUSDT", "1H", limit=3)


def test_get_ohlcv_notes_short_history(monkeypatch):
    monkeypatch.setattr(data, "_yahoo_ohlcv", lambda inst, tf, lim, recent=False: fake_frame(4))
    df = data.get_ohlcv("AAPL", "1H", limit=10)
    assert len(df) == 4 and "Only 4 of 10" in df.attrs["note"]


# --- market hours (New York is on EST, UTC-5, in mid-November 2026) ----------------------------

def utc(*a):
    return datetime(*a, tzinfo=UTC)


def test_crypto_always_open():
    assert data.market_status(data.resolve_symbol("BTCUSDT"), utc(2026, 11, 14, 12))["state"] == "open"


def test_forex_weekend_closed_with_next_open():
    st = data.market_status(data.resolve_symbol("EURUSD"), utc(2026, 11, 14, 12))  # Saturday
    assert st["state"] == "closed" and st["next_open"] == "2026-11-15T22:00+00:00"   # Sun 17:00 NY
    assert data.market_status(data.resolve_symbol("EURUSD"), utc(2026, 11, 18, 3))["state"] == "open"
    assert data.market_status(data.resolve_symbol("EURUSD"), utc(2026, 11, 20, 22, 1))["state"] == "closed"


def test_futures_daily_break():
    gold = data.resolve_symbol("XAUUSD")
    assert data.market_status(gold, utc(2026, 11, 17, 22, 30))["state"] == "closed"  # 17:30 NY break
    assert data.market_status(gold, utc(2026, 11, 17, 23, 30))["state"] == "open"


def test_equity_session_and_next_open():
    aapl = data.resolve_symbol("AAPL")
    assert data.market_status(aapl, utc(2026, 11, 16, 15))["state"] == "open"  # Mon 10:00 NY
    st = data.market_status(aapl, utc(2026, 11, 16, 21, 30))                    # Mon 16:30 NY
    assert st["state"] == "closed" and st["next_open"] == "2026-11-17T14:30+00:00"


def test_unknown_exchange_hours():
    assert data.market_status(data.resolve_symbol("FOO.XX"), utc(2026, 11, 16, 15))["state"] == "unknown"
