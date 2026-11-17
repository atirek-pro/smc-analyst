#!/usr/bin/env python3
"""Browser check of the dashboard's chart navigation (pan / zoom / cursors / price lock).

Dev-only tool. It opens a running dashboard in a headless Chromium browser (Edge or Chrome),
sends real wheel, modifier, drag and hover input through the Chrome DevTools Protocol, and
reads the chart's visible time and price ranges back after each action.

    python skills/smc-analyst/scripts/serve.py --symbol BTCUSDT --port 8790 --detach --no-browser
    python tools/check_navigation.py http://127.0.0.1:8790
    SMC_BROWSER="C:/path/to/chrome.exe" python tools/check_navigation.py URL

Run it with the skill's venv Python (it needs `websockets`). Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

CDP_PORT = 9341

STATE = """(() => { const S = window.SMCDashboard; if (!S || !S.chart || !S.candles.length || !S.nav) return null;
  const chart = document.getElementById('chart'), rect = chart.getBoundingClientRect();
  const axisW = S.chart.priceScale('right').width(), axisH = S.chart.timeScale().height();
  const r = S.chart.timeScale().getVisibleLogicalRange();
  return { from: r.from, to: r.to, n: S.candles.length,
    top: S.series.coordinateToPrice(0), bot: S.series.coordinateToPrice(rect.height - axisH),
    auto: S.nav.isAutoScale(), autoBtn: document.getElementById('btn-auto').classList.contains('on'),
    left: rect.left, topPx: rect.top, w: rect.width, h: rect.height, axisW, axisH }; })()"""


def find_browser() -> str:
    env = os.environ.get("SMC_BROWSER")
    candidates = [env] if env else []
    candidates += [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ] + [shutil.which(n) or "" for n in ("google-chrome", "chromium", "chromium-browser", "msedge")]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("No Chromium-based browser found; set SMC_BROWSER to Chrome/Edge.")


class Page:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def cmd(self, method: str, **params):
        # Every DevTools call is bounded, so a stuck browser fails the run instead of hanging it.
        try:
            return await asyncio.wait_for(self._cmd(method, params), timeout=30)
        except TimeoutError:
            raise RuntimeError(f"browser did not answer {method} within 30 s") from None

    async def _cmd(self, method: str, params: dict):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    async def ev(self, expr: str):
        return (await self.cmd("Runtime.evaluate", expression=expr, returnByValue=True))["result"].get("value")

    async def wheel(self, x, y, dx=0.0, dy=0.0, mods=0, pause=0.08):
        await self.cmd("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy, modifiers=mods)
        await asyncio.sleep(pause)

    async def mouse(self, kind, x, y, buttons=0):
        extra = {"button": "left", "clickCount": 1} if kind in ("mousePressed", "mouseReleased") else {}
        await self.cmd("Input.dispatchMouseEvent", type=kind, x=x, y=y, buttons=buttons, **extra)

    async def drag(self, x0, y0, x1, y1, steps=20):
        await self.mouse("mouseMoved", x0, y0)
        await self.mouse("mousePressed", x0, y0, buttons=1)
        for i in range(1, steps + 1):
            await self.mouse("mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, buttons=1)
            await asyncio.sleep(0.02)
        await self.mouse("mouseReleased", x1, y1)
        await asyncio.sleep(0.4)


async def run(url: str) -> int:
    profile = tempfile.mkdtemp()
    browser = subprocess.Popen([find_browser(), "--headless=new", "--disable-gpu",
                                f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={profile}",
                                "--window-size=1500,900", url])
    results: list[bool] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(bool(ok))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))

    try:
        page_info = None
        for _ in range(150):             # up to ~45 s for a cold browser start
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2))
                page_info = next(t for t in tabs if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.3)
        if page_info is None:
            sys.exit("browser did not start")
        async with websockets.connect(page_info["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            p = Page(ws)
            for _ in range(180):
                if await p.ev(STATE):
                    break
                await asyncio.sleep(0.5)
            else:
                sys.exit("dashboard did not load (is the server running and ready?)")
            await asyncio.sleep(1.5)
            st = lambda: p.ev(STATE)

            async def reset():
                # Re-enable auto-scale and let the chart finish re-fitting (it applies on the next frame).
                await p.ev("SMCDashboard.nav.setAutoScale(true)")
                await asyncio.sleep(0.4)
            bars = lambda s: s["to"] - s["from"]
            pspan = lambda s: s["top"] - s["bot"]
            same = lambda a, b, tol=1e-6: abs(a - b) <= tol * max(1.0, abs(a))

            s0 = await st()
            L, T = s0["left"], s0["topPx"]
            paneW, paneH = s0["w"] - s0["axisW"], s0["h"] - s0["axisH"]
            px, py = L + paneW * 0.55, T + paneH * 0.5
            time_x, time_y = L + paneW * 0.5, T + paneH + s0["axisH"] / 2
            price_x, price_y = L + paneW + s0["axisW"] / 2, T + paneH * 0.4

            # --- hover only -------------------------------------------------------------
            a = await st()
            for i in range(50):
                await p.mouse("mouseMoved", L + (s0["w"] - 4) * i / 49, T + 10 + (s0["h"] - 14) * ((i * 7) % 50) / 49)
            await asyncio.sleep(0.4)
            b = await st()
            check("hover: no change to time or price", same(a["from"], b["from"]) and same(a["to"], b["to"])
                  and same(pspan(a), pspan(b)) and b["auto"])

            # --- sideways scroll / swipe: time only, price scale untouched ---------------
            a = await st()
            spans = []
            for _ in range(12):
                await p.wheel(px, py, dx=-50, dy=6)          # swipe with vertical noise
                spans.append(pspan(await st()))
            b = await st()
            check("swipe left/right: moves in time", b["from"] < a["from"] - 1,
                  f"from {a['from']:.1f} -> {b['from']:.1f}")
            check("swipe left/right: bar count unchanged (no time zoom)", same(bars(a), bars(b), 1e-4),
                  f"{bars(a):.2f} -> {bars(b):.2f}")
            check("swipe left/right: price scale never changes (no vertical zoom)",
                  all(same(x, pspan(a), 1e-6) for x in spans), f"{pspan(a):.2f} -> {min(spans):.2f}..{max(spans):.2f}")
            check("swipe left/right: price axis locked (auto off, toggle grey)", not b["auto"] and not b["autoBtn"])
            await p.wheel(px, py, dx=50 * 12, dy=0)
            await reset()

            # --- shift + wheel pans time, again without touching the price scale --------
            a = await st()
            await p.wheel(px, py, dy=-200, mods=8)
            b = await st()
            check("shift+wheel: moves in time, price scale unchanged",
                  b["from"] < a["from"] and same(bars(a), bars(b), 1e-4) and same(pspan(a), pspan(b)))
            await reset()

            # --- drag sideways (with a little wobble) -----------------------------------
            a = await st()
            await p.mouse("mouseMoved", px, py)
            await p.mouse("mousePressed", px, py, buttons=1)
            spans = []
            for i in range(1, 31):
                await p.mouse("mouseMoved", px + 15 * i, py + (i % 3) - 1, buttons=1)
                await asyncio.sleep(0.02)
                spans.append(pspan(await st()))
            await p.mouse("mouseReleased", px + 450, py)
            await asyncio.sleep(0.5)
            b = await st()
            check("drag sideways: moves in time", b["from"] < a["from"] - 1, f"from {a['from']:.1f} -> {b['from']:.1f}")
            check("drag sideways: price scale never changes",
                  all(same(x, pspan(a)) for x in spans) and same(pspan(b), pspan(a)),
                  f"{pspan(a):.2f} -> {min(spans):.2f}..{max(spans):.2f}")
            await reset()

            # --- plain vertical wheel pans price ----------------------------------------
            a = await st()
            await p.wheel(px, py, dy=100)
            b = await st()
            check("wheel up/down: moves in price, same price scale, same time",
                  b["top"] < a["top"] and same(pspan(a), pspan(b), 1e-6) and same(a["from"], b["from"]),
                  f"top {a['top']:.2f} -> {b['top']:.2f}, span {pspan(a):.2f} -> {pspan(b):.2f}, "
                  f"from {a['from']:.2f} -> {b['from']:.2f}")
            await reset()

            # --- vertical drag: grabbed price stays under the cursor --------------------
            await asyncio.sleep(0.2)
            grab = await p.ev(f"SMCDashboard.series.coordinateToPrice({py - T})")
            await p.drag(px, py, px, py + 100)
            under = await p.ev(f"SMCDashboard.series.coordinateToPrice({py + 100 - T})")
            a = await st()
            check("drag up/down: grabbed price stays under the cursor", abs(under - grab) <= pspan(a) * 0.005,
                  f"{grab:.2f} vs {under:.2f}")
            await reset()

            # --- time zoom: ctrl+wheel / pinch, anchored, price scale untouched ----------
            await asyncio.sleep(0.2)
            a = await st()
            ax = L + paneW * 0.25
            anchor_a = a["from"] + bars(a) * 0.25
            await p.wheel(ax, py, dy=-100, mods=2)
            b = await st()
            anchor_b = b["from"] + bars(b) * 0.25
            check("ctrl+wheel: zooms time in", bars(b) < bars(a) * 0.9, f"{bars(a):.1f} -> {bars(b):.1f} bars")
            check("ctrl+wheel: bar under the cursor stays put", abs(anchor_a - anchor_b) < 0.5)
            check("ctrl+wheel: price scale unchanged", same(pspan(a), pspan(b)))
            await reset()

            # --- time axis: wheel and drag zoom time only --------------------------------
            await asyncio.sleep(0.2)
            a = await st()
            await p.wheel(time_x, time_y, dy=100)
            b = await st()
            check("time-axis wheel: zooms time, price scale unchanged",
                  bars(b) > bars(a) * 1.1 and same(pspan(a), pspan(b)), f"{bars(a):.1f} -> {bars(b):.1f} bars")
            await reset()
            await asyncio.sleep(0.2)
            a = await st()
            await p.drag(time_x, time_y, time_x - 150, time_y)
            b = await st()
            check("time-axis drag: zooms time, price scale unchanged",
                  not same(bars(a), bars(b), 1e-3) and same(pspan(a), pspan(b)), f"{bars(a):.1f} -> {bars(b):.1f} bars")
            await reset()

            # --- price axis: wheel zooms price only ----------------------------------------
            await asyncio.sleep(0.2)
            a = await st()
            await p.wheel(price_x, price_y, dy=-100)
            b = await st()
            check("price-axis wheel: zooms price, time unchanged",
                  pspan(b) < pspan(a) * 0.9 and same(a["from"], b["from"]) and same(a["to"], b["to"]))

            # --- auto toggle restores the fit ---------------------------------------------
            await p.ev("document.getElementById('btn-auto').click()")
            await asyncio.sleep(0.3)
            b = await st()
            check("auto toggle: re-enables price auto-scale", b["auto"] and b["autoBtn"])

            # --- live tick while locked: slides the axis, never rescales -------------------
            # F (fit) puts the newest bar on screen.
            await p.ev("document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'f', bubbles: true}))")
            await asyncio.sleep(0.3)
            await p.wheel(px, py, dx=1, dy=0)                  # lock the price axis
            a = await st()
            target = a["top"] + pspan(a) * 0.5                 # a price far above the view
            await p.ev(f"SMCDashboard.nav.keepInView({target})")
            b = await st()
            check("live tick above the view: price axis slides to show it, same scale",
                  b["top"] >= target and same(pspan(a), pspan(b), 1e-6) and not b["auto"],
                  f"top {a['top']:.2f} -> {b['top']:.2f}, target {target:.2f}")
            await reset()

            # --- cursors per region -------------------------------------------------------
            cursor = "((x, y) => getComputedStyle(document.elementFromPoint(x, y)).cursor)"
            await p.mouse("mouseMoved", time_x, time_y)
            ct = await p.ev(f"{cursor}({time_x}, {time_y})")
            await p.mouse("mouseMoved", price_x, price_y)
            cp = await p.ev(f"{cursor}({price_x}, {price_y})")
            check("cursor: time axis shows <->", ct == "ew-resize", ct)
            check("cursor: price axis shows up/down", cp == "ns-resize", cp)
    finally:
        browser.kill()

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default="http://127.0.0.1:8765", help="dashboard URL")
    ap.add_argument("--tf", default="1H", help="timeframe to test on")
    args = ap.parse_args()
    url = args.url.rstrip("/") + f"/?tf={args.tf}"
    return asyncio.run(run(url))


if __name__ == "__main__":
    sys.exit(main())
