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
        # Hover first, as a real mouse does: headless Chromium can stall a wheel event that
        # arrives straight after a drag release with no movement in between.
        await self.mouse("mouseMoved", x, y)
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


class Ctx:
    """One fresh dashboard page plus the geometry and helpers every section uses."""

    def __init__(self, page: Page, s0: dict, check):
        self.p, self.check, self.s0 = page, check, s0
        self.L, self.T = s0["left"], s0["topPx"]
        self.paneW, self.paneH = s0["w"] - s0["axisW"], s0["h"] - s0["axisH"]
        self.px, self.py = self.L + self.paneW * 0.55, self.T + self.paneH * 0.5
        self.time_x, self.time_y = self.L + self.paneW * 0.5, self.T + self.paneH + s0["axisH"] / 2
        self.price_x, self.price_y = self.L + self.paneW + s0["axisW"] / 2, self.T + self.paneH * 0.4

    async def st(self):
        return await self.p.ev(STATE)

    async def fit(self):
        # One-shot price fit (load / F / A / "auto"); it locks again after the next paint.
        await self.p.ev("SMCDashboard.nav.fitPrice()")
        await asyncio.sleep(0.4)

    async def drag_track(self, x0, y0, path):
        """Press at (x0, y0), move through path [(x, y)], release; price span after every step."""
        await self.p.mouse("mouseMoved", x0, y0)
        await self.p.mouse("mousePressed", x0, y0, buttons=1)
        spans = []
        for x, y in path:
            await self.p.mouse("mouseMoved", x, y, buttons=1)
            spans.append(pspan(await self.st()))
        await self.p.mouse("mouseReleased", *path[-1])
        await asyncio.sleep(0.5)
        return spans


def bars(s):
    return s["to"] - s["from"]


def pspan(s):
    return s["top"] - s["bot"]


def same(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(a))


# Each section runs in its own fresh browser page: long single sessions of synthetic input can
# stall inside headless Chromium (the dashboard page itself stays responsive), so isolating the
# sections keeps the check reliable.

async def sec_load_and_hover(c: Ctx):
    c.check("load: price axis fitted and locked (no auto-scale left on)", not c.s0["auto"])
    a = await c.st()
    for i in range(50):
        await c.p.mouse("mouseMoved", c.L + (c.s0["w"] - 4) * i / 49,
                        c.T + 10 + (c.s0["h"] - 14) * ((i * 7) % 50) / 49)
    await asyncio.sleep(0.4)
    b = await c.st()
    c.check("hover: no change to time or price",
            same(a["from"], b["from"]) and same(a["to"], b["to"]) and same(pspan(a), pspan(b)))


async def sec_swipe(c: Ctx):
    a = await c.st()
    spans = []
    for _ in range(12):
        await c.p.wheel(c.px, c.py, dx=-50, dy=6)          # swipe with vertical noise
        spans.append(pspan(await c.st()))
    b = await c.st()
    c.check("swipe left/right: moves in time", b["from"] < a["from"] - 1,
            f"from {a['from']:.1f} -> {b['from']:.1f}")
    c.check("swipe left/right: bar count unchanged (no time zoom)", same(bars(a), bars(b), 1e-4),
            f"{bars(a):.2f} -> {bars(b):.2f}")
    c.check("swipe left/right: price scale never changes (no vertical zoom)",
            all(same(x, pspan(a)) for x in spans), f"{pspan(a):.6f} -> {min(spans):.6f}..{max(spans):.6f}")
    c.check("swipe left/right: price axis stays locked", not b["auto"])
    a = await c.st()
    await c.p.wheel(c.px, c.py, dy=-200, mods=8)
    b = await c.st()
    c.check("shift+wheel: moves in time, price scale unchanged",
            b["from"] < a["from"] and same(bars(a), bars(b), 1e-4) and same(pspan(a), pspan(b)))


async def sec_drag(c: Ctx):
    a = await c.st()
    spans = await c.drag_track(c.px, c.py, [(c.px + 15 * i, c.py + (i % 3) - 1) for i in range(1, 31)])
    b = await c.st()
    c.check("drag sideways: moves in time", b["from"] < a["from"] - 1,
            f"from {a['from']:.1f} -> {b['from']:.1f}")
    c.check("drag sideways: price scale never changes", all(same(x, pspan(a)) for x in spans),
            f"{pspan(a):.6f} -> {min(spans):.6f}..{max(spans):.6f}")


async def sec_unseen_drag(c: Ctx):
    # The 1.0.2 failure on real mice: navigation.js's own pointer handlers miss the drag and only
    # Lightweight Charts handles it. The price scale must still not change.
    await c.p.ev("(n => { n.onPointerDown = () => {}; n.onPointerMove = () => {}; })(SMCDashboard.nav)")
    a = await c.st()
    path = [(c.px - 15 * i, c.py + 20 * ((i // 5) % 2) - 10) for i in range(1, 31)]
    spans = await c.drag_track(c.px, c.py, path)
    b = await c.st()
    c.check("drag unseen by our listeners: moves in time", abs(b["from"] - a["from"]) > 1)
    c.check("drag unseen by our listeners: price scale never changes", all(same(x, pspan(a)) for x in spans),
            f"{pspan(a):.6f} -> {min(spans):.6f}..{max(spans):.6f}")


async def sec_safety_net(c: Ctx):
    await c.p.ev("SMCDashboard.series.priceScale().setAutoScale(true)")   # behind navigation.js's back
    await asyncio.sleep(0.4)
    a = await c.st()
    spans = []
    for _ in range(8):
        await c.p.wheel(c.px, c.py, dx=-60, dy=0)
        spans.append(pspan(await c.st()))
    b = await c.st()
    c.check("safety net: stray auto-scale re-locked on first time move, scale unchanged",
            not b["auto"] and all(same(x, pspan(a)) for x in spans),
            f"{pspan(a):.6f} -> {min(spans):.6f}..{max(spans):.6f}")


async def sec_price_pan(c: Ctx):
    a = await c.st()
    await c.p.wheel(c.px, c.py, dy=100)
    b = await c.st()
    c.check("wheel up/down: moves in price, same price scale, same time",
            b["top"] < a["top"] and same(pspan(a), pspan(b)) and same(a["from"], b["from"]),
            f"top {a['top']:.6f} -> {b['top']:.6f}")
    await c.fit()
    grab = await c.p.ev(f"SMCDashboard.series.coordinateToPrice({c.py - c.T})")
    await c.p.drag(c.px, c.py, c.px, c.py + 100)
    under = await c.p.ev(f"SMCDashboard.series.coordinateToPrice({c.py + 100 - c.T})")
    a = await c.st()
    c.check("drag up/down: grabbed price stays under the cursor", abs(under - grab) <= pspan(a) * 0.005,
            f"{grab:.6f} vs {under:.6f}")


async def sec_time_zoom(c: Ctx):
    a = await c.st()
    ax = c.L + c.paneW * 0.25
    anchor_a = a["from"] + bars(a) * 0.25
    await c.p.wheel(ax, c.py, dy=-100, mods=2)
    b = await c.st()
    anchor_b = b["from"] + bars(b) * 0.25
    c.check("ctrl+wheel: zooms time in", bars(b) < bars(a) * 0.9, f"{bars(a):.1f} -> {bars(b):.1f} bars")
    c.check("ctrl+wheel: bar under the cursor stays put", abs(anchor_a - anchor_b) < 0.5)
    c.check("ctrl+wheel: price scale unchanged", same(pspan(a), pspan(b)))
    a = await c.st()
    await c.p.wheel(c.time_x, c.time_y, dy=100)
    b = await c.st()
    c.check("time-axis wheel: zooms time, price scale unchanged",
            bars(b) > bars(a) * 1.1 and same(pspan(a), pspan(b)), f"{bars(a):.1f} -> {bars(b):.1f} bars")


async def sec_time_axis_drag(c: Ctx):
    a = await c.st()
    await c.p.drag(c.time_x, c.time_y, c.time_x - 150, c.time_y)
    b = await c.st()
    c.check("time-axis drag: zooms time, price scale unchanged",
            not same(bars(a), bars(b), 1e-3) and same(pspan(a), pspan(b)),
            f"{bars(a):.1f} -> {bars(b):.1f} bars")


async def sec_price_axis_and_fit(c: Ctx):
    a = await c.st()
    await c.p.wheel(c.price_x, c.price_y, dy=-100)
    b = await c.st()
    c.check("price-axis wheel: zooms price, time unchanged",
            pspan(b) < pspan(a) * 0.9 and same(a["from"], b["from"]) and same(a["to"], b["to"]))
    await c.p.ev("SMCDashboard.series.priceScale().setAutoScale(true)")   # the library's own fit
    await asyncio.sleep(0.4)
    ref = pspan(await c.st())
    await c.p.wheel(c.price_x, c.price_y, dy=-150)                       # user zooms prices
    await c.p.ev("document.getElementById('btn-auto').click()")
    await asyncio.sleep(0.4)
    b = await c.st()
    c.check("auto button: fits prices to the candles on screen, then locks",
            same(pspan(b), ref) and not b["auto"], f"{pspan(b):.6f} vs fit {ref:.6f}")


async def sec_live_tick(c: Ctx):
    a = await c.st()
    target = a["top"] + pspan(a) * 0.5                                    # a price far above the view
    await c.p.ev(f"SMCDashboard.nav.keepInView({target})")
    b = await c.st()
    c.check("live tick above the view: price axis slides to show it, same scale",
            b["top"] >= target and same(pspan(a), pspan(b)) and not b["auto"],
            f"top {a['top']:.6f} -> {b['top']:.6f}, target {target:.6f}")


async def sec_cursors(c: Ctx):
    cursor = "((x, y) => getComputedStyle(document.elementFromPoint(x, y)).cursor)"
    await c.p.mouse("mouseMoved", c.time_x, c.time_y)
    ct = await c.p.ev(f"{cursor}({c.time_x}, {c.time_y})")
    await c.p.mouse("mouseMoved", c.price_x, c.price_y)
    cp = await c.p.ev(f"{cursor}({c.price_x}, {c.price_y})")
    c.check("cursor: time axis shows <->", ct == "ew-resize", ct)
    c.check("cursor: price axis shows up/down", cp == "ns-resize", cp)


SECTIONS = [sec_load_and_hover, sec_swipe, sec_drag, sec_unseen_drag, sec_safety_net, sec_price_pan,
            sec_time_zoom, sec_time_axis_drag, sec_price_axis_and_fit, sec_live_tick, sec_cursors]


async def run_section(url: str, section, check) -> None:
    browser = subprocess.Popen([find_browser(), "--headless=new", "--disable-gpu",
                                f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={tempfile.mkdtemp()}",
                                "--window-size=1500,900", url])
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
            raise RuntimeError("browser did not start")
        async with websockets.connect(page_info["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            p = Page(ws)
            for _ in range(180):
                if await p.ev(STATE):
                    break
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("dashboard did not load (is the server running and ready?)")
            await asyncio.sleep(1.5)
            await section(Ctx(p, await p.ev(STATE), check))
    finally:
        browser.kill()
        try:
            browser.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


async def run(url: str) -> int:
    results: list[bool] = []
    for section in SECTIONS:
        for attempt in (1, 2):
            pending: list[tuple[str, bool, str]] = []
            try:
                await run_section(url, section, lambda n, ok, d="", out=pending: out.append((n, bool(ok), d)))
                break
            except RuntimeError as e:
                if attempt == 2:
                    pending.append((f"{section.__name__}: could not run ({e})", False, ""))
                    break
                print(f"(retrying {section.__name__} in a fresh browser: {e})")
        for name, ok, detail in pending:
            results.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))

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
