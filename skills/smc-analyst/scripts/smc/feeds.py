"""Live candle feeds.

``stream(instrument, tf)`` is an async generator that yields plain dicts:

    {"type": "candle", "symbol", "tf", "time" (bar open, epoch s), "open", "high", "low",
     "close", "volume", "closed" (bool), "source", "realtime" (bool), "received_at"}
    {"type": "status", "symbol", "tf", "state", "message", "received_at"}

Status ``state`` values:
    connected | reconnecting | fallback | market_closed | market_open | degraded

After a ``connected`` that follows ``reconnecting``, the consumer should backfill
candles over REST (``data.get_ohlcv``), because updates may have been missed.

Sources:
    crypto (Binance)  WebSocket kline stream (pushes every ~1–2 s). After repeated WS
                      failures it falls back to polling the REST API every few seconds.
    Yahoo symbols     polls ``data.fetch_recent`` every ``data.poll_interval_seconds``.
                      Polling slows down while the market is closed.

CLI (run from the scripts/ directory):
    python -m smc.feeds BTCUSDT --tf 1m --updates 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pandas as pd

from . import data
from .config import get_config
from .data import Instrument

log = logging.getLogger("smc.feeds")

BINANCE_WS_HOSTS = ("wss://stream.binance.com:9443/ws", "wss://data-stream.binance.vision/ws")
WS_FAILURES_BEFORE_FALLBACK = 4
REST_FALLBACK_INTERVAL = 5.0
CLOSED_MARKET_POLL = 300.0


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def status_event(inst: Instrument, tf: str, state: str, message: str = "") -> dict:
    return {"type": "status", "symbol": inst.symbol, "tf": tf, "state": state,
            "message": message, "received_at": _now_iso()}


def candle_event(inst: Instrument, tf: str, ts: pd.Timestamp | int, o: float, h: float, low: float,
                 c: float, v: float, closed: bool, source: str) -> dict:
    t = int(ts.timestamp()) if isinstance(ts, pd.Timestamp) else int(ts)
    r = lambda x: data.round_to_tick(float(x), inst.tick_size)  # noqa: E731
    return {"type": "candle", "symbol": inst.symbol, "tf": tf, "time": t,
            "open": r(o), "high": r(h), "low": r(low), "close": r(c), "volume": float(v),
            "closed": bool(closed), "source": source, "realtime": source == "binance-ws",
            "received_at": _now_iso()}


# -----------------------------------------------------------------------------
# Binance WebSocket
# -----------------------------------------------------------------------------

def binance_stream_url(host: str, symbol: str, tf: str) -> str:
    return f"{host}/{symbol.lower()}@kline_{data.BINANCE_INTERVAL[tf]}"


def parse_binance_kline(msg: str | bytes | dict, inst: Instrument, tf: str) -> dict | None:
    """Convert a Binance ``kline`` WS message into a candle event (None if not a kline)."""
    payload = json.loads(msg) if isinstance(msg, (str, bytes)) else msg
    if "data" in payload and isinstance(payload["data"], dict):  # combined-stream envelope
        payload = payload["data"]
    if payload.get("e") != "kline" or "k" not in payload:
        return None
    k = payload["k"]
    return candle_event(inst, tf, int(k["t"]) // 1000, k["o"], k["h"], k["l"], k["c"], k["v"],
                        bool(k["x"]), "binance-ws")


async def _binance_ws(inst: Instrument, tf: str, stop: asyncio.Event) -> AsyncIterator[dict]:
    from websockets.asyncio.client import connect

    failures, attempt, had_connection = 0, 0, False
    while not stop.is_set():
        host = BINANCE_WS_HOSTS[attempt % len(BINANCE_WS_HOSTS)]
        url = binance_stream_url(host, inst.provider_symbol, tf)
        try:
            async with connect(url, open_timeout=20, ping_interval=20, ping_timeout=20,
                               close_timeout=5) as ws:
                failures = 0
                yield status_event(inst, tf, "connected",
                                   f"Binance WebSocket ({host.split('//')[1].split('/')[0]})"
                                   + ("; backfill recommended" if had_connection else ""))
                had_connection = True
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    except TimeoutError:
                        raise ConnectionError("no data from Binance for 60 s") from None
                    event = parse_binance_kline(msg, inst, tf)
                    if event:
                        yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # network errors, handshake failures, closed connections
            if stop.is_set():
                break
            failures += 1
            attempt += 1
            if failures >= WS_FAILURES_BEFORE_FALLBACK:
                yield status_event(inst, tf, "fallback",
                                   f"WebSocket unavailable ({exc}); polling Binance REST instead")
                async for ev in _poll(inst, tf, stop, interval=REST_FALLBACK_INTERVAL):
                    yield ev
                return
            delay = min(60.0, 2.0 ** failures)
            yield status_event(inst, tf, "reconnecting", f"{exc}; retry in {delay:.0f}s")
            await _sleep(delay, stop)


# -----------------------------------------------------------------------------
# Polling (Yahoo symbols, or the Binance REST fallback)
# -----------------------------------------------------------------------------

async def _sleep(seconds: float, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def diff_bars(df: pd.DataFrame, seen: dict[int, tuple], inst: Instrument, tf: str,
              source: str, now: datetime | None = None) -> list[dict]:
    """Return candle events for bars that are new or changed since the last poll.

    ``seen`` maps bar open time (epoch s) to its last emitted (o, h, l, c, v, closed)
    and is updated in place.
    """
    events = []
    for ts, row in df.iterrows():
        t = int(ts.timestamp())
        closed = data.is_bar_closed(ts, tf, now)
        sig = (row["open"], row["high"], row["low"], row["close"], row["volume"], closed)
        if seen.get(t) != sig:
            seen[t] = sig
            events.append(candle_event(inst, tf, t, row["open"], row["high"], row["low"],
                                       row["close"], row["volume"], closed, source))
    for t in sorted(seen)[:-10]:  # keep memory bounded
        seen.pop(t, None)
    return events


async def _poll(inst: Instrument, tf: str, stop: asyncio.Event,
                interval: float | None = None) -> AsyncIterator[dict]:
    base_interval = float(interval or get_config()["data"]["poll_interval_seconds"])
    source = "binance-rest" if inst.provider == "binance" else "yahoo-poll"
    seen: dict[int, tuple] = {}
    delay = base_interval
    last_market_state: str | None = None
    first = True

    while not stop.is_set():
        mstate = data.market_status(inst)["state"]
        if mstate != last_market_state and not first:
            yield status_event(inst, tf, "market_open" if mstate != "closed" else "market_closed",
                               "" if mstate != "closed" else "No new candles until the session reopens")
        last_market_state = mstate

        if mstate == "closed" and not first:
            await _sleep(CLOSED_MARKET_POLL, stop)
            continue
        try:
            df = await asyncio.to_thread(data.fetch_recent, inst, tf, 3)
            if first:
                yield status_event(inst, tf, "connected", f"polling every {base_interval:.0f}s "
                                   f"({inst.data_note})")
                if mstate == "closed":
                    yield status_event(inst, tf, "market_closed",
                                       "No new candles until the session reopens")
            for ev in diff_bars(df, seen, inst, tf, source):
                yield ev
            delay = base_interval
        except data.SymbolError:
            raise
        except Exception as exc:  # rate limits, network errors
            delay = min(300.0, delay * 2)
            yield status_event(inst, tf, "degraded", f"{exc}; next poll in {delay:.0f}s")
        first = False
        await _sleep(delay, stop)


# -----------------------------------------------------------------------------
# Public entry point
# -----------------------------------------------------------------------------

async def stream(inst: Instrument | str, tf: str, stop: asyncio.Event | None = None,
                 poll_interval: float | None = None) -> AsyncIterator[dict]:
    """Yield live candle/status events until ``stop`` is set (or the consumer stops iterating)."""
    inst = data.resolve_symbol(inst) if isinstance(inst, str) else inst
    tf = data.normalize_timeframe(tf)
    stop = stop or asyncio.Event()
    if inst.provider == "binance":
        gen = _binance_ws(inst, tf, stop)
    else:
        gen = _poll(inst, tf, stop, interval=poll_interval)
    async for event in gen:
        yield event


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

async def _cli(symbol: str, tf: str, updates: int, timeout: float) -> int:
    inst = data.resolve_symbol(symbol)
    stop = asyncio.Event()
    count = 0

    async def consume():
        nonlocal count
        async for ev in stream(inst, tf, stop):
            print(json.dumps(ev), flush=True)
            if ev["type"] == "candle":
                count += 1
                if count >= updates:
                    stop.set()
                    return

    try:
        await asyncio.wait_for(consume(), timeout=timeout)
    except TimeoutError:
        stop.set()
        print(json.dumps({"type": "cli", "message": f"timeout after {timeout:.0f}s with "
                          f"{count} candle updates"}))
    return 0 if count else 1


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m smc.feeds", description="Stream live candles.")
    ap.add_argument("symbol")
    ap.add_argument("--tf", default="1m")
    ap.add_argument("--updates", type=int, default=5, help="stop after N candle updates")
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        return asyncio.run(_cli(args.symbol, args.tf, args.updates, args.timeout))
    except (data.DataError, ValueError) as exc:
        print(json.dumps({"type": "error", "message": str(exc)}))
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(_main())
