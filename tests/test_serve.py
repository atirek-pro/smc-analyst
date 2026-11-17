"""Phase 5: dashboard server - payloads, live candle merging, API, WebSocket, process helpers."""

from __future__ import annotations

import asyncio
import json
import socket
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import serve
from smc.config import load_config
from smc.data import TF_SECONDS, resolve_symbol
from smc.mtf import analyze_timeframe
from smc.tracker import Journal

from smc_builders import zigzag
from test_tracker import plan as valid_plan

CFG = load_config()
FREQ = {"1W": "7D", "1D": "1D", "4H": "4h", "1H": "1h", "15m": "15min", "5m": "5min", "1m": "1min"}
PTS = [100, 120, 110, 135, 124, 150, 138, 165, 150, 170, 158]
LEGS = [2, 6, 2, 6, 2, 6, 2, 6, 2, 6]


def frame(tf: str, closed: bool = True) -> pd.DataFrame:
    end = (pd.Timestamp("2026-11-13 12:00", tz="UTC").floor(FREQ[tf]) if tf != "1W"
           else pd.Timestamp("2026-11-09", tz="UTC"))
    n = 1 + sum(LEGS)
    df = zigzag(PTS, legs=LEGS, start=end - pd.Timedelta(seconds=TF_SECONDS[tf]) * (n - 1), freq=FREQ[tf], wick=0.3)
    df.attrs.update({"instrument": resolve_symbol("BTCUSDT").to_dict(), "last_bar_closed": closed})
    return df


def fake_fetch(inst, tf, limit, use_cache=True):
    return frame(tf)


def make_stream(events_by_tf, gate=None):
    """Fake live feed. With ``gate`` (a dict holding the state), events wait until a viewer of
    that timeframe is connected, like real market events that arrive while someone watches."""
    async def stream(inst, tf, stop):
        if gate is not None:
            while not any(v == tf for v in gate["state"].clients.values()):
                if stop.is_set():
                    return
                await asyncio.sleep(0.01)
        for ev in events_by_tf.get(tf, []):
            await asyncio.sleep(0.01)
            yield ev
        await stop.wait()
    return stream


def make_state(tmp_path, **kw) -> serve.DashboardState:
    kw.setdefault("fetch", fake_fetch)
    kw.setdefault("stream", make_stream({}))
    return serve.DashboardState("BTCUSDT", CFG, journal=Journal(tmp_path / "j.json", CFG), **kw)


def wait_ready(client, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        meta = client.get("/api/meta").json()
        if meta["status"] in ("ready", "error"):
            return meta
        time.sleep(0.05)
    raise AssertionError("dashboard never became ready")


# --- payload builders ------------------------------------------------------------------------

def test_candles_payload_epoch_seconds():
    df = frame("1H")
    out = serve.candles_payload(df)
    assert len(out) == len(df) and out[0]["time"] == int(df.index[0].timestamp())
    assert set(out[0]) == {"time", "open", "high", "low", "close", "volume"}
    assert serve.candles_payload(df.iloc[:0]) == []


def test_overlays_payload_shape_and_htf_projection():
    analyses = {tf: analyze_timeframe(frame(tf), tf, CFG) for tf in ("1D", "4H", "1H", "15m")}
    o = serve.overlays_payload(analyses["15m"], analyses)
    for key in ("breaks", "swings", "zones", "liquidity", "sweeps", "dealing_range", "htf_zones", "inducement"):
        assert key in o
    assert all(isinstance(b["from"], int) and isinstance(b["to"], int) for b in o["breaks"])
    assert {z["tf"] for z in o["htf_zones"]} <= set(serve.PROJECTED_TFS)       # 1H is never projected
    per_tf = pd.Series([z["tf"] for z in o["htf_zones"]]).value_counts()
    assert (per_tf <= serve.HTF_PER_TF).all()
    json.dumps(o)


def test_zone_payload_ends_at_mitigation():
    a = analyze_timeframe(frame("4H"), "4H", CFG)
    for z in a.zones.zones:
        p = serve.zone_payload(z, "4H")
        assert (p["to"] is None) == (z.state != "mitigated")


# --- live candle merging -------------------------------------------------------------------------

def test_apply_candle_updates_appends_and_detects_close(tmp_path):
    st = make_state(tmp_path)
    st.load_sync()
    df = st.frames["15m"]
    last = int(df.index[-1].timestamp())
    upd = {"type": "candle", "time": last, "open": 1, "high": 999, "low": 0.5, "close": 2, "volume": 3, "closed": False}
    assert st.apply_candle("15m", upd) is False and st.frames["15m"]["high"].iloc[-1] == 999
    assert st.frames["15m"].attrs["last_bar_closed"] is False
    assert st.apply_candle("15m", {**upd, "closed": True}) is True            # first close of that bar
    assert st.apply_candle("15m", {**upd, "closed": True}) is False           # duplicate close ignored
    nxt = {**upd, "time": last + 900, "closed": False}
    st.apply_candle("15m", nxt)
    assert int(st.frames["15m"].index[-1].timestamp()) == last + 900 and len(st.frames["15m"]) == len(df) + 1
    assert st.apply_candle("15m", {**upd, "time": last - 10 * 900 - 7}) is False  # stale out-of-order


def test_apply_candle_does_not_mutate_analysis_frame(tmp_path):
    st = make_state(tmp_path)
    st.load_sync()
    before = st.analyses["15m"].df.copy()
    last = int(st.frames["15m"].index[-1].timestamp())
    st.apply_candle("15m", {"time": last, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1, "closed": True})
    pd.testing.assert_frame_equal(st.analyses["15m"].df, before)


def test_stale_timeframes_and_reanalyze(tmp_path):
    st = make_state(tmp_path)
    st.load_sync()
    stale = st.stale_tfs(now=pd.Timestamp("2026-11-20", tz="UTC").to_pydatetime())
    assert set(stale) == set(st.frames)
    v = st.version
    done = st.reanalyze_sync(["15m"], ["1D"])
    assert set(done) == set(st.frames) and st.version == v + 1   # 1D refresh -> period levels -> all re-analysed


# --- HTTP API ---------------------------------------------------------------------------------------

def test_api_endpoints(tmp_path):
    st = make_state(tmp_path, live=False)
    with TestClient(serve.create_app(st)) as client:
        meta = wait_ready(client)
        assert meta["status"] == "ready" and meta["symbol"] == "BTCUSDT" and meta["app"] == "smc-analyst"
        assert meta["timeframes"] == ["1W", "1D", "4H", "1H", "15m", "5m", "1m"] and meta["default_tf"] == "15m"
        c = client.get("/api/candles", params={"tf": "4h"}).json()
        assert c["tf"] == "4H" and len(c["candles"]) == 1 + sum(LEGS) and c["precision"] == 2
        a = client.get("/api/analysis", params={"tf": "1H"}).json()
        assert a["overlays"]["tf"] == "1H" and a["cascade"]["stages"]
        assert a["plan"]["status"] in ("valid", "no_valid_setup", "no_setup")
        assert client.get("/api/candles", params={"tf": "7h"}).status_code == 400
        assert client.get("/api/journal").json()["stats"]["total"] == 0
        assert client.get("/").status_code == 200 and "lightweight-charts" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        nav = client.get("/static/navigation.js")
        assert nav.status_code == 200 and nav.headers["cache-control"] == "no-cache"
        assert "/static/navigation.js" in client.get("/").text
        assert client.post("/api/shutdown").json() == {"status": "stopping"}


def test_api_returns_503_while_loading(tmp_path):
    def slow_fetch(inst, tf, limit, use_cache=True):
        time.sleep(0.5)
        return frame(tf)
    st = make_state(tmp_path, fetch=slow_fetch, live=False)
    with TestClient(serve.create_app(st)) as client:
        assert client.get("/api/candles", params={"tf": "1H"}).status_code == 503
        wait_ready(client)


def test_load_error_is_reported(tmp_path):
    from smc.data import DataError

    def broken(inst, tf, limit, use_cache=True):
        raise DataError("exchange down")
    st = make_state(tmp_path, fetch=broken, live=False)
    with TestClient(serve.create_app(st)) as client:
        meta = wait_ready(client)
        assert meta["status"] == "error" and "exchange down" in meta["error"]


def test_track_endpoint(tmp_path):
    st = make_state(tmp_path, live=False)
    with TestClient(serve.create_app(st)) as client:
        wait_ready(client)
        st.plan = None
        assert client.post("/api/track").status_code == 409
        st.plan = valid_plan()
        r = client.post("/api/track").json()
        assert r["tracked"]["status"] == "pending"
        assert client.get("/api/journal").json()["stats"]["open"] == 1


# --- WebSocket ----------------------------------------------------------------------------------------

def test_websocket_hello_candles_subscribe_and_recompute(tmp_path):
    last = int(frame("15m").index[-1].timestamp())
    events = {"15m": [{"type": "status", "state": "connected", "message": "test feed", "tf": "15m"},
                      {"type": "candle", "tf": "15m", "time": last + 900, "open": 170, "high": 171, "low": 169,
                       "close": 170.5, "volume": 1, "closed": True, "realtime": True}]}
    gate: dict = {}
    st = make_state(tmp_path, stream=make_stream(events, gate))
    gate["state"] = st
    with TestClient(serve.create_app(st)) as client:
        wait_ready(client)
        with client.websocket_connect("/ws/stream?tf=15m") as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["tf"] == "15m"
            seen = []
            for _ in range(10):
                msg = ws.receive_json()
                seen.append(msg["type"])
                if msg["type"] == "analysis_updated":
                    break
            assert "candle" in seen and "analysis_updated" in seen     # closed candle -> recompute
            ws.send_json({"subscribe": "1H"})
            while (m := ws.receive_json())["type"] != "subscribed":
                pass
            assert m["tf"] == "1H" and "1H" in st.feed_tasks
            ws.send_json({"type": "ping"})
            while (m := ws.receive_json())["type"] != "pong":
                pass
        time.sleep(0.1)
        assert "1H" not in st.feed_tasks and "15m" in st.feed_tasks   # viewer gone: only the pinned feed stays


# --- process helpers ---------------------------------------------------------------------------------------

def test_pick_port_skips_busy_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        assert serve.pick_port("127.0.0.1", (busy, busy + 20)) != busy


def test_pick_port_raises_when_range_full():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        with pytest.raises(RuntimeError):
            serve.pick_port("127.0.0.1", (busy, busy))


def test_stop_and_running_with_stale_pid_file(tmp_path):
    pid_file = tmp_path / "server.pid"
    assert serve.stop_server(pid_file) == {"status": "not_running"}
    pid_file.write_text(json.dumps({"pid": 123456, "url": "http://127.0.0.1:9", "symbol": "BTCUSDT"}))
    assert serve.running_server(pid_file) is None
    assert serve.stop_server(pid_file)["status"] == "stale_pid_file_removed" and not pid_file.exists()


def test_detached_command_drops_parent_only_flags():
    cmd = serve.detached_command(["--symbol", "BTCUSDT", "--detach", "--replace", "--json", "--account", "10000"])
    assert cmd[1].endswith("serve.py") and cmd[0]
    assert cmd[2:] == ["--symbol", "BTCUSDT", "--json", "--account", "10000"]


def test_start_detached_reports_failure_with_log_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "LOG_FILE", tmp_path / "server.log")
    monkeypatch.setattr(serve, "running_server", lambda *a: None)
    # The child exits immediately (bad symbol), so the parent must give up quickly and say why.
    res = serve.start_detached(["--symbol", "!!!", "--json", "--no-browser"], "X", timeout=20)
    assert res["status"] == "error" and "exit code 1" in res["message"] and "error" in res["log_tail"]


def test_cli_rejects_bad_symbol(capsys):
    assert serve.main(["--symbol", "!!!", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"
