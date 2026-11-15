#!/usr/bin/env python3
"""SMC Analyst live dashboard server (FastAPI + WebSocket).

Serves the chart UI from ``assets/dashboard`` and a small API for one symbol:

    GET  /                      dashboard page
    GET  /api/meta              symbol, instrument, timeframes, market status, load status
    GET  /api/candles?tf=1H     OHLCV candles (UTC epoch seconds)
    GET  /api/analysis?tf=1H    drawable overlays for that timeframe + cascade + trade plan
    GET  /api/journal           tracked signals + stats
    POST /api/track             start tracking the current valid plan
    POST /api/refresh           re-fetch every timeframe and recompute
    POST /api/shutdown          stop the server (used by ``--stop``)
    WS   /ws/stream?tf=1H       live candles for the viewed timeframe, status and
                                "analysis_updated" / "journal" notifications

Live data flows from ``smc.feeds.stream``, one upstream feed per timeframe, run only while
needed (the viewed timeframes, the confirmation timeframe that drives the cascade, and the
tracker timeframe). When a candle **closes**, that timeframe's analysis is recomputed (and cached
per timeframe), stale higher timeframes are refreshed, the cascade and trade plan are rebuilt,
and clients are told to reload overlays.

CLI:
    python serve.py --symbol BTCUSDT [--account 10000 --risk-pct 1] [--track] [--port N]
                    [--no-browser] [--replace] [--json]
    python serve.py --status
    python serve.py --stop
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal as _signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import pandas as pd  # noqa: E402
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from smc import __version__, data, feeds  # noqa: E402
from smc.common import closed_bars, jsonable  # noqa: E402
from smc.config import SKILL_DIR, get_config  # noqa: E402
from smc.liquidity import period_levels  # noqa: E402
from smc.mtf import TF_ORDER, TimeframeAnalysis, analyze_timeframe, run_cascade  # noqa: E402
from smc.signal import build_trade_plan  # noqa: E402
from smc.tracker import Journal  # noqa: E402

log = logging.getLogger("smc.serve")
DASHBOARD_DIR = SKILL_DIR / "assets" / "dashboard"
PID_FILE = SKILL_DIR / ".server.pid"
EPOCH = pd.Timestamp("1970-01-01", tz="UTC")
MAX_BREAKS, MAX_LEVELS, MAX_SWINGS = 40, 60, 80
SHORT_LIQ = {"swing_high": "BSL", "swing_low": "SSL", "equal_highs": "EQH", "equal_lows": "EQL"}
PROJECTED_TFS = ("1M", "1W", "1D", "4H")
HTF_PER_TF = 4


# =============================================================================
# Payload builders (pure functions: easy to test)
# =============================================================================

def _t(ts) -> int | None:
    return None if ts is None else int(pd.Timestamp(ts).timestamp())


def candles_payload(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    secs = ((df.index.tz_convert("UTC") - EPOCH) // pd.Timedelta(seconds=1)).astype("int64")
    return [{"time": int(t), "open": float(o), "high": float(h), "low": float(lo), "close": float(c),
             "volume": float(v)}
            for t, o, h, lo, c, v in zip(secs, df["open"], df["high"], df["low"], df["close"], df["volume"])]


def zone_payload(z, tf: str) -> dict:
    return {"kind": z.kind, "dir": z.direction, "top": z.top, "bottom": z.bottom, "from": _t(z.time),
            "created": _t(z.created_time), "to": _t(z.mitigated_time) if z.state == "mitigated" else None,
            "state": z.state, "label": z.label(), "tf": z.timeframe or tf, "source": z.source}


def overlays_payload(a: TimeframeAnalysis, analyses: dict[str, TimeframeAnalysis], cascade=None) -> dict:
    """Everything the chart draws for one timeframe."""
    tf = a.tf
    breaks = [{"kind": b.kind, "dir": b.direction, "level": b.level, "from": _t(b.swing_time),
               "to": _t(b.break_time), "structure": b.structure}
              for b in a.structure.breaks[-MAX_BREAKS:] + a.internal.breaks[-MAX_BREAKS:]]
    swings = [{"time": _t(s.time), "price": s.price, "kind": s.kind, "label": s.label, "level": s.level}
              for s in (a.swings + a.internal_swings)[-MAX_SWINGS:]]
    levels = sorted(a.liquidity.levels, key=lambda lv: lv.time)[-MAX_LEVELS:]
    liquidity = [{"price": lv.price, "side": lv.side, "kind": lv.kind, "label": SHORT_LIQ.get(lv.kind, lv.kind),
                  "from": _t(lv.time), "to": _t(lv.status_time), "status": lv.status} for lv in levels]
    sweeps = [{"time": _t(lv.status_time), "price": lv.price, "side": lv.side,
               "label": SHORT_LIQ.get(lv.kind, lv.kind)} for lv in levels if lv.status == "swept"]
    dr = a.dealing_range
    pd_band = None if dr is None else {
        "high": dr.high, "low": dr.low, "eq": dr.eq, "ote_top": dr.ote_top, "ote_bottom": dr.ote_bottom,
        "eq_band": dr.eq_band, "direction": dr.direction, "from": _t(min(dr.high_time, dr.low_time)),
        "price_zone": dr.zone_of(a.price) if a.price == a.price else None}

    # Higher-timeframe POIs projected onto this chart, as traders do by hand: only the monthly,
    # weekly, daily and 4H POIs above this timeframe, the nearest few per timeframe, and only
    # those near this chart's price range (so a 1m chart isn't buried under 100 boxes).
    htf = []
    if not a.df.empty and tf in TF_ORDER:
        lo, hi = float(a.df["low"].min()), float(a.df["high"].max())
        pad = 2 * (hi - lo)
        price = a.price if a.price == a.price else float(a.df["close"].iloc[-1])
        for htf_tf in [t for t in TF_ORDER[: TF_ORDER.index(tf)] if t in PROJECTED_TFS]:
            h = analyses.get(htf_tf)
            if h is None:
                continue
            near = [z for z in h.zones.display() if z.bottom <= hi + pad and z.top >= lo - pad]
            near.sort(key=lambda z: 0.0 if z.bottom <= price <= z.top else min(abs(price - z.top), abs(price - z.bottom)))
            htf += [zone_payload(z, htf_tf) for z in near[:HTF_PER_TF]]

    ind = getattr(cascade, "inducement", None)
    return {
        "tf": tf, "bars": a.bars, "price": a.price, "atr": a.atr,
        "trend": a.structure.trend_name, "internal_trend": a.internal.trend_name,
        "breaks": breaks, "swings": swings,
        "zones": [zone_payload(z, tf) for z in a.zones.display()],
        "liquidity": liquidity, "sweeps": sweeps, "dealing_range": pd_band, "htf_zones": htf,
        "inducement": None if ind is None else {"price": ind.price, "time": _t(ind.time), "swept": ind.swept,
                                                  "swept_time": _t(ind.swept_time)},
    }


# =============================================================================
# Dashboard state + live feed hub
# =============================================================================

def _default_fetch(inst, tf: str, limit: int, use_cache: bool = True) -> pd.DataFrame:
    return data.get_ohlcv(inst, tf, limit, use_cache=use_cache)


class DashboardState:
    """All data for one symbol, kept current by live feeds."""

    def __init__(self, symbol: str, cfg: dict | None = None, account: float | None = None,
                 risk_pct: float | None = None, auto_track: bool = False,
                 fetch: Callable[..., pd.DataFrame] | None = None, stream: Callable | None = None,
                 hedge_fetch: Callable | None = None, journal: Journal | None = None, live: bool = True):
        self.cfg = cfg or get_config()
        self.inst = data.resolve_symbol(symbol)  # raises SymbolError early for bad input
        self.symbol = self.inst.symbol
        tfs = list(self.cfg["timeframes"]["ladder"])
        if self.cfg["timeframes"].get("include_monthly") and "1M" not in tfs:
            tfs.insert(0, "1M")
        self.tfs = tfs
        self.limit = int(self.cfg["timeframes"]["history_bars"])
        self.account, self.risk_pct, self.auto_track = account, risk_pct, auto_track
        self.fetch = fetch or _default_fetch
        self.stream = stream or feeds.stream
        self.hedge_fetch = hedge_fetch
        self.journal = journal or Journal(cfg=self.cfg)
        self.live = live
        self.confirm_tf = self.cfg["structure"].get("confirmation_timeframe", "15m")
        self.tracker_tf = self.cfg["tracker"].get("timeframe", "5m")

        self.status, self.error, self.loaded_at = "loading", None, None
        self.warnings: list[str] = []
        self.frames: dict[str, pd.DataFrame] = {}
        self.analyses: dict[str, TimeframeAnalysis] = {}
        self.extra: list = []
        self.cascade = None
        self.plan = None
        self.hedge = None
        self.version = 0
        self.last_closed: dict[str, int] = {}
        self.feed_state: dict[str, dict] = {}
        self.clients: dict[Any, str] = {}
        self.feed_tasks: dict[str, asyncio.Task] = {}
        self.feed_stops: dict[str, asyncio.Event] = {}
        self._lock: asyncio.Lock | None = None
        self._bg: set[asyncio.Task] = set()

    # ---- loading / recompute (sync, run in a worker thread) -------------------------------

    def _fetch(self, tf: str, use_cache: bool = True) -> pd.DataFrame:
        return self.fetch(self.inst, tf, self.limit, use_cache=use_cache)

    def _update_instrument(self, df: pd.DataFrame) -> None:
        info = df.attrs.get("instrument") or {}
        if info:
            self.inst = replace(self.inst, tick_size=info.get("tick_size", self.inst.tick_size),
                                provider=info.get("provider", self.inst.provider),
                                provider_symbol=info.get("provider_symbol", self.inst.provider_symbol),
                                realtime=info.get("realtime", self.inst.realtime),
                                data_note=info.get("data_note", self.inst.data_note))

    def _period_levels(self) -> list:
        daily = closed_bars(self.frames["1D"], "1D") if "1D" in self.frames else None
        weekly = closed_bars(self.frames["1W"], "1W") if "1W" in self.frames else None
        return period_levels(daily, weekly)

    def load_sync(self, use_cache: bool = True) -> None:
        warnings = []
        frames = {}
        for tf in self.tfs:
            try:
                frames[tf] = self._fetch(tf, use_cache)
            except data.SymbolError:
                raise
            except data.DataError as exc:
                warnings.append(f"{tf} unavailable: {exc}")
        if not frames:
            raise data.DataError("no timeframe could be loaded: " + "; ".join(warnings))
        self.frames, self.warnings = frames, warnings
        self._update_instrument(next(iter(frames.values())))
        self.extra = self._period_levels()
        self.analyses = {tf: analyze_timeframe(df, tf, self.cfg, self.extra) for tf, df in frames.items()}
        self.rebuild_sync(with_hedge=True)
        self.loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def rebuild_sync(self, with_hedge: bool = False) -> None:
        self.cascade = run_cascade(self.analyses, self.inst, self.cfg)
        self.cascade.warnings.extend(self.warnings)
        plan = build_trade_plan(self.cascade, self.analyses, self.inst, self.cfg, account=self.account,
                                risk_pct=self.risk_pct, with_hedge=with_hedge, hedge_fetch=self.hedge_fetch)
        if with_hedge:
            self.hedge = plan.hedge
        elif plan.is_valid and self.hedge is not None and self.plan is not None \
                and self.plan.direction == plan.direction:
            plan.hedge = self.hedge  # correlations change slowly; reuse until the next full refresh
        self.plan = plan
        self.version += 1
        if self.auto_track and plan.is_valid:
            self.journal.add_plan(plan, self.inst)

    def stale_tfs(self, now: datetime | None = None) -> list[str]:
        """Non-streamed timeframes whose newest bar has closed (a newer bar should exist)."""
        out = []
        for tf, df in self.frames.items():
            if tf in self.feed_tasks or df.empty:
                continue
            if data.is_bar_closed(df.index[-1], tf, now):
                out.append(tf)
        return out

    def reanalyze_sync(self, tfs: list[str], refetch: list[str]) -> list[str]:
        """Re-analyse ``tfs``; re-fetch ``refetch`` first. Returns the timeframes actually updated."""
        done = []
        for tf in refetch:
            try:
                self.frames[tf] = self._fetch(tf, use_cache=True)
            except data.DataError as exc:
                log.warning("refresh %s failed: %s", tf, exc)
        if any(tf in ("1D", "1W") for tf in refetch):
            self.extra = self._period_levels()
            tfs = list(self.frames)  # PDH/PDL/PWH/PWL changed: every timeframe's liquidity changes
        for tf in dict.fromkeys(tfs + refetch):
            if tf in self.frames:
                self.analyses[tf] = analyze_timeframe(self.frames[tf], tf, self.cfg, self.extra)
                done.append(tf)
        self.rebuild_sync(with_hedge=False)
        return done

    def apply_candle(self, tf: str, ev: dict) -> bool:
        """Merge a live candle into the in-memory frame. Returns True on a *new* bar close."""
        df = self.frames.get(tf)
        if df is None:
            return False
        ts = pd.Timestamp(int(ev["time"]), unit="s", tz="UTC")
        row = [float(ev["open"]), float(ev["high"]), float(ev["low"]), float(ev["close"]), float(ev["volume"])]
        attrs = dict(df.attrs)
        if len(df) and ts < df.index[-1] and ts not in df.index:
            return False  # out-of-order update for a bar we no longer hold
        if ts in df.index:
            df = df.copy()  # never mutate a frame an analysis object may still reference
            df.loc[ts, ["open", "high", "low", "close", "volume"]] = row
        else:
            new = pd.DataFrame([row], columns=["open", "high", "low", "close", "volume"],
                               index=pd.DatetimeIndex([ts], name="time"))
            df = pd.concat([df, new]).iloc[-(self.limit + 50):]
        df.attrs.update(attrs)
        df.attrs["last_bar_closed"] = bool(ev.get("closed"))
        self.frames[tf] = df
        if ev.get("closed") and int(ev["time"]) > self.last_closed.get(tf, 0):
            self.last_closed[tf] = int(ev["time"])
            return True
        return False

    # ---- async orchestration --------------------------------------------------------------

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def start(self) -> None:
        try:
            async with self.lock:
                await asyncio.to_thread(self.load_sync)
                await asyncio.to_thread(self.journal.refresh)
            self.status = "ready"
        except Exception as exc:  # noqa: BLE001 - surface any load failure to the UI
            log.exception("load failed")
            self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
            await self.broadcast({"type": "error", "message": self.error})
            return
        if self.live:
            self.sync_feeds()
        await self.broadcast({"type": "analysis_updated", "tfs": list(self.analyses), "version": self.version})
        await self.broadcast({"type": "journal"})

    async def refresh(self) -> None:
        async with self.lock:
            self.status = "loading"
            await self.broadcast({"type": "status", "state": "loading", "message": "refreshing all timeframes"})
            try:
                await asyncio.to_thread(self.load_sync, False)
                self.status = "ready"
            except Exception as exc:  # noqa: BLE001
                self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
        await self.broadcast({"type": "analysis_updated", "tfs": list(self.analyses), "version": self.version})

    async def on_close(self, tf: str) -> None:
        async with self.lock:
            refetch = self.stale_tfs()
            done = await asyncio.to_thread(self.reanalyze_sync, [tf], refetch)
        await self.broadcast({"type": "analysis_updated", "tfs": done, "version": self.version,
                              "status": self.cascade.status if self.cascade else None})

    async def refetch_tf(self, tf: str) -> None:
        """Backfill after a feed reconnect (candles may have been missed)."""
        async with self.lock:
            done = await asyncio.to_thread(self.reanalyze_sync, [], [tf])
        await self.broadcast({"type": "analysis_updated", "tfs": done, "version": self.version, "reload": True})

    async def handle_event(self, tf: str, ev: dict) -> None:
        if ev.get("type") == "candle":
            new_close = self.apply_candle(tf, ev)
            await self.broadcast(ev, tf=tf)
            if tf == self.tracker_tf:
                changed = await asyncio.to_thread(self.journal.update_with_candle, self.symbol, tf, ev)
                if changed:
                    await self.broadcast({"type": "journal"})
            if new_close:
                self._spawn(self.on_close(tf))
        elif ev.get("type") == "status":
            self.feed_state[tf] = ev
            await self.broadcast(ev)
            if ev.get("state") == "connected" and "backfill" in (ev.get("message") or ""):
                self._spawn(self.refetch_tf(tf))

    # ---- feeds ------------------------------------------------------------------------------

    def wanted_feeds(self) -> set[str]:
        wanted = {tf for tf in self.clients.values() if tf in self.frames}
        if self.confirm_tf in self.frames:
            wanted.add(self.confirm_tf)  # drives the cascade
        has_open = any(s.is_open and s.symbol == self.symbol for s in self.journal.signals)
        if (has_open or self.auto_track) and self.tracker_tf in self.frames:
            wanted.add(self.tracker_tf)
        return wanted

    def sync_feeds(self) -> None:
        if not self.live or self.status != "ready":
            return
        wanted = self.wanted_feeds()
        for tf in wanted - set(self.feed_tasks):
            stop = asyncio.Event()
            self.feed_stops[tf] = stop
            self.feed_tasks[tf] = asyncio.create_task(self._run_feed(tf, stop))
        for tf in set(self.feed_tasks) - wanted:
            self.feed_stops.pop(tf).set()
            self.feed_tasks.pop(tf).cancel()

    async def _run_feed(self, tf: str, stop: asyncio.Event) -> None:
        try:
            async for ev in self.stream(self.inst, tf, stop):
                if stop.is_set():
                    break
                await self.handle_event(tf, ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("feed %s stopped: %s", tf, exc)
            await self.broadcast({"type": "status", "tf": tf, "state": "degraded", "message": str(exc)})

    async def shutdown(self) -> None:
        for stop in self.feed_stops.values():
            stop.set()
        for task in list(self.feed_tasks.values()) + list(self._bg):
            task.cancel()
        self.feed_tasks.clear()

    # ---- clients ----------------------------------------------------------------------------

    async def broadcast(self, msg: dict, tf: str | None = None) -> None:
        dead = []
        for ws, wtf in list(self.clients.items()):
            if tf is not None and wtf != tf:
                continue
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 - client went away
                dead.append(ws)
        for ws in dead:
            self.clients.pop(ws, None)

    # ---- API views ----------------------------------------------------------------------------

    def meta(self) -> dict:
        return {
            "app": "smc-analyst", "version": __version__, "pid": os.getpid(), "status": self.status,
            "error": self.error, "loaded_at": self.loaded_at, "symbol": self.symbol,
            "instrument": self.inst.to_dict(), "market": data.market_status(self.inst),
            "timeframes": [tf for tf in TF_ORDER if tf in self.frames] or self.tfs,
            "default_tf": self.confirm_tf if self.confirm_tf in self.frames else (self.tfs[-1] if self.tfs else None),
            "feeds": {tf: self.feed_state.get(tf, {}).get("state", "starting") for tf in self.feed_tasks},
            "warnings": self.warnings, "version_counter": self.version,
        }

    def require(self, tf: str | None = None) -> str | None:
        if self.status != "ready":
            raise HTTPException(503, detail=self.error or "analysis is still loading")
        if tf is None:
            return None
        try:
            tf = data.normalize_timeframe(tf)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        if tf not in self.frames:
            raise HTTPException(404, detail=f"timeframe {tf} not loaded")
        return tf

    def journal_view(self) -> dict:
        return {"signals": [s.to_dict() for s in reversed(self.journal.signals)], "stats": self.journal.stats(),
                "symbol": self.symbol}


# =============================================================================
# FastAPI app
# =============================================================================

def create_app(state: DashboardState) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        loader = asyncio.create_task(state.start())
        yield
        loader.cancel()
        await state.shutdown()

    app = FastAPI(title="SMC Analyst Dashboard", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dashboard = state
    app.state.server = None
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(DASHBOARD_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/meta")
    async def meta():
        return state.meta()

    @app.get("/api/candles")
    async def candles(tf: str = Query(...)):
        tf = state.require(tf)
        df = state.frames[tf]
        return {"tf": tf, "candles": candles_payload(df), "last_bar_closed": df.attrs.get("last_bar_closed"),
                "data_note": state.inst.data_note, "tick_size": state.inst.tick_size,
                "precision": state.inst.price_precision}

    @app.get("/api/analysis")
    async def analysis(tf: str = Query(...)):
        tf = state.require(tf)
        a = state.analyses.get(tf)
        if a is None:
            raise HTTPException(404, detail=f"no analysis for {tf}")
        return {"version": state.version, "symbol": state.symbol, "overlays": overlays_payload(a, state.analyses, state.cascade),
                "cascade": state.cascade.to_dict() if state.cascade else None,
                "plan": state.plan.to_dict() if state.plan else None,
                "market": data.market_status(state.inst)}

    @app.get("/api/journal")
    async def journal():
        return state.journal_view()

    @app.post("/api/track")
    async def track():
        state.require()
        if state.plan is None or not state.plan.is_valid:
            raise HTTPException(409, detail="There is no valid trade plan to track right now.")
        sig = state.journal.add_plan(state.plan, state.inst)
        state.sync_feeds()
        await state.broadcast({"type": "journal"})
        return {"tracked": sig.to_dict()}

    @app.post("/api/refresh")
    async def refresh():
        state._spawn(state.refresh())
        return {"status": "refreshing"}

    @app.post("/api/shutdown")
    async def shutdown():
        server = app.state.server
        if server is not None:
            server.should_exit = True
        return {"status": "stopping"}

    @app.websocket("/ws/stream")
    async def ws_stream(ws: WebSocket, tf: str = Query("15m")):
        await ws.accept()
        try:
            tf = data.normalize_timeframe(tf)
        except ValueError:
            tf = state.confirm_tf
        state.clients[ws] = tf
        state.sync_feeds()
        await ws.send_json({"type": "hello", "tf": tf, "status": state.status, "version": state.version,
                            "feeds": state.meta()["feeds"]})
        try:
            while True:
                msg = await ws.receive_json()
                if isinstance(msg, dict) and msg.get("subscribe"):
                    try:
                        state.clients[ws] = data.normalize_timeframe(msg["subscribe"])
                    except ValueError:
                        continue
                    state.sync_feeds()
                    await ws.send_json({"type": "subscribed", "tf": state.clients[ws]})
                elif isinstance(msg, dict) and msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            state.clients.pop(ws, None)
            state.sync_feeds()

    return app


# =============================================================================
# Process management: PID file, free port, stop / status
# =============================================================================

def pick_port(host: str, port_range: tuple[int, int]) -> int:
    lo, hi = port_range
    for port in range(lo, hi + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {lo}-{hi}")


def read_pid_file(path: Path = PID_FILE) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _http_json(url: str, method: str = "GET", timeout: float = 3.0) -> dict | None:
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def running_server(path: Path = PID_FILE) -> dict | None:
    """The recorded server if it is actually alive (its /api/meta answers with the same pid)."""
    info = read_pid_file(path)
    if not info:
        return None
    meta = _http_json(f"{info['url']}/api/meta", timeout=2.0)
    if meta and meta.get("pid") == info.get("pid") and meta.get("app") == "smc-analyst":
        return {**info, "meta": meta}
    return None


def stop_server(path: Path = PID_FILE) -> dict:
    info = read_pid_file(path)
    if not info:
        return {"status": "not_running"}
    live = running_server(path)
    if live:
        _http_json(f"{info['url']}/api/shutdown", method="POST")
        for _ in range(30):
            if running_server(path) is None:
                break
            time.sleep(0.2)
        if running_server(path) is not None:
            with contextlib.suppress(OSError):
                os.kill(int(info["pid"]), _signal.SIGTERM)
    with contextlib.suppress(OSError):
        path.unlink()
    return {"status": "stopped" if live else "stale_pid_file_removed", "pid": info.get("pid"), "url": info.get("url")}


def _open_browser_when_ready(url: str) -> None:
    def worker():
        for _ in range(100):
            if _http_json(f"{url}/api/meta", timeout=1.0):
                webbrowser.open(url)
                return
            time.sleep(0.2)
    threading.Thread(target=worker, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SMC Analyst live dashboard.")
    ap.add_argument("--symbol", help="symbol to chart, e.g. BTCUSDT, EURUSD, AAPL")
    ap.add_argument("--account", type=float, default=None, help="account size for position sizing")
    ap.add_argument("--risk-pct", type=float, default=None, help="percent risked per trade")
    ap.add_argument("--track", action="store_true", help="automatically track valid plans in the journal")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--replace", action="store_true", help="stop a running dashboard first")
    ap.add_argument("--stop", action="store_true", help="stop the running dashboard")
    ap.add_argument("--status", action="store_true", help="show whether a dashboard is running")
    ap.add_argument("--json", action="store_true", help="print machine-readable status lines")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    def out(payload: dict, text: str) -> None:
        print(json.dumps(payload) if args.json else text, flush=True)

    if args.stop:
        res = stop_server()
        out(res, {"stopped": f"Dashboard stopped (pid {res.get('pid')}).",
                  "not_running": "No dashboard is running.",
                  "stale_pid_file_removed": "No live dashboard; removed a stale PID file."}[res["status"]])
        return 0
    live = running_server()
    if args.status:
        out({"status": "running", **{k: live[k] for k in ("url", "pid", "symbol")}} if live else {"status": "not_running"},
            f"Dashboard running at {live['url']} for {live['symbol']} (pid {live['pid']})." if live
            else "No dashboard is running.")
        return 0
    if not args.symbol:
        ap.error("--symbol is required to start the dashboard")

    try:
        wanted = data.resolve_symbol(args.symbol).symbol
    except data.SymbolError as exc:
        out({"status": "error", "message": str(exc)}, f"Error: {exc}")
        return 1
    if live and not args.replace:
        if live["symbol"] == wanted:
            out({"status": "already_running", "url": live["url"], "pid": live["pid"], "symbol": wanted},
                f"Dashboard already running for {wanted} at {live['url']} (pid {live['pid']}).")
            if not args.no_browser:
                webbrowser.open(live["url"])
            return 0
        out({"status": "busy", "url": live["url"], "symbol": live["symbol"]},
            f"A dashboard for {live['symbol']} is already running at {live['url']}. "
            f"Use --replace to switch to {wanted}, or --stop first.")
        return 2
    if live and args.replace:
        stop_server()

    cfg = get_config()
    host = args.host or cfg["server"]["host"]
    port = args.port or pick_port(host, tuple(cfg["server"]["port_range"]))
    url = f"http://{host}:{port}"
    state = DashboardState(wanted, cfg, account=args.account, risk_pct=args.risk_pct, auto_track=args.track)
    app = create_app(state)

    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    app.state.server = server
    PID_FILE.write_text(json.dumps({"pid": os.getpid(), "url": url, "host": host, "port": port, "symbol": wanted,
                                    "started": datetime.now(timezone.utc).isoformat(timespec="seconds")}),
                        encoding="utf-8")
    out({"status": "started", "url": url, "pid": os.getpid(), "symbol": wanted},
        f"SMC dashboard for {wanted} running at {url} (pid {os.getpid()}). Stop with: python serve.py --stop")
    if not args.no_browser and cfg["server"].get("open_browser", True):
        _open_browser_when_ready(url)
    try:
        server.run()
    finally:
        info = read_pid_file()
        if info and info.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                PID_FILE.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
