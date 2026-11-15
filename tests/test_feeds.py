"""Phase 2: live-feed parsing and polling diff logic (offline)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pandas as pd

from smc import data, feeds

BTC = data.resolve_symbol("BTCUSDT")
EUR = data.resolve_symbol("EURUSD")

KLINE = {
    "e": "kline", "E": 1794728052000, "s": "BTCUSDT",
    "k": {"t": 1794728040000, "T": 1794728099999, "s": "BTCUSDT", "i": "1m",
          "o": "64287.16000000", "c": "64261.51000000", "h": "64287.16000000",
          "l": "64261.51000000", "v": "9.43620000", "x": False},
}


def test_parse_binance_kline():
    ev = feeds.parse_binance_kline(json.dumps(KLINE), BTC, "1m")
    assert ev["type"] == "candle" and ev["time"] == 1794728040
    assert (ev["open"], ev["close"], ev["volume"]) == (64287.16, 64261.51, 9.4362)
    assert ev["closed"] is False and ev["realtime"] is True and ev["source"] == "binance-ws"


def test_parse_binance_kline_combined_envelope_and_non_kline():
    assert feeds.parse_binance_kline({"stream": "x", "data": KLINE}, BTC, "1m")["time"] == 1794728040
    assert feeds.parse_binance_kline({"result": None, "id": 1}, BTC, "1m") is None


def test_stream_url():
    assert feeds.binance_stream_url("wss://h/ws", "BTCUSDT", "4H") == "wss://h/ws/btcusdt@kline_4h"
    assert feeds.binance_stream_url("wss://h/ws", "BTCUSDT", "1M") == "wss://h/ws/btcusdt@kline_1M"


def bars(closes, start="2026-11-16 10:00"):
    idx = pd.date_range(start, periods=len(closes), freq="15min", tz="UTC")
    return pd.DataFrame({"open": 1.1, "high": 1.2, "low": 1.0, "close": closes, "volume": 0.0},
                        index=idx)


def test_diff_bars_emits_only_changes():
    seen: dict = {}
    now = datetime(2026, 11, 16, 10, 40, tzinfo=timezone.utc)
    first = feeds.diff_bars(bars([1.10, 1.11, 1.12]), seen, EUR, "15m", "yahoo-poll", now)
    assert len(first) == 3
    assert [e["closed"] for e in first] == [True, True, False]
    assert feeds.diff_bars(bars([1.10, 1.11, 1.12]), seen, EUR, "15m", "yahoo-poll", now) == []
    changed = feeds.diff_bars(bars([1.10, 1.11, 1.13]), seen, EUR, "15m", "yahoo-poll", now)
    assert len(changed) == 1 and changed[0]["close"] == 1.13


def test_diff_bars_marks_bar_closed_when_time_passes():
    seen: dict = {}
    df = bars([1.10, 1.11, 1.12])
    feeds.diff_bars(df, seen, EUR, "15m", "yahoo-poll", datetime(2026, 11, 16, 10, 40, tzinfo=timezone.utc))
    later = feeds.diff_bars(df, seen, EUR, "15m", "yahoo-poll",
                            datetime(2026, 11, 16, 10, 46, tzinfo=timezone.utc))
    assert len(later) == 1 and later[0]["closed"] is True


def test_poll_stream_emits_status_then_candles(monkeypatch):
    monkeypatch.setattr(data, "market_status", lambda inst, now=None: {"state": "open"})
    monkeypatch.setattr(data, "fetch_recent", lambda inst, tf, n: bars([1.1, 1.2]))

    async def run():
        stop = asyncio.Event()
        out = []
        async for ev in feeds.stream(EUR, "15m", stop, poll_interval=0.01):
            out.append(ev)
            if len(out) == 3:
                stop.set()
                break
        return out

    events = asyncio.run(run())
    assert events[0]["type"] == "status" and events[0]["state"] == "connected"
    assert [e["type"] for e in events[1:]] == ["candle", "candle"]
    assert events[1]["source"] == "yahoo-poll" and events[1]["realtime"] is False


def test_poll_stream_reports_degraded_on_errors(monkeypatch):
    monkeypatch.setattr(data, "market_status", lambda inst, now=None: {"state": "open"})

    def boom(*a):
        raise ConnectionError("rate limited")

    monkeypatch.setattr(data, "fetch_recent", boom)

    async def run():
        async for ev in feeds.stream(EUR, "15m", asyncio.Event(), poll_interval=0.01):
            return ev

    ev = asyncio.run(run())
    assert ev["state"] == "degraded" and "rate limited" in ev["message"]
