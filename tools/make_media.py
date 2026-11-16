#!/usr/bin/env python3
"""Regenerate the README screenshots and hero GIF from the real, live dashboard.

Dev-only tool (needs Pillow from requirements-dev.txt and a Chromium-based browser: Edge or
Chrome). It starts the dashboard per symbol, captures each view with a headless browser,
crops concept shots to the chart, compresses them, and assembles the hero GIF.

    python tools/make_media.py                       # everything -> docs/images/
    python tools/make_media.py --only hero,position  # a subset
    SMC_BROWSER="C:/path/to/chrome.exe" python tools/make_media.py

Run it with the skill's venv Python (or any Python with the runtime + Pillow installed).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SERVE = ROOT / "skills" / "smc-analyst" / "scripts" / "serve.py"
OUT = ROOT / "docs" / "images"
SIZE = (1600, 1000)
CHART_BOX = (0, 66, 1218, 966)  # the chart area inside a 1600x1000 dashboard screenshot

# name: (symbol, query, crop_to_chart, caption)
SHOTS = {
    "dashboard": ("BTCUSDT", "tf=15m", False, "Full dashboard: chart, status badges and the side panel"),
    "structure": ("BTCUSDT", "tf=1H&layers=structure,swings", True, "Swing structure: BOS / CHoCH and HH/HL labels"),
    "order-blocks": ("BTCUSDT", "tf=4H&layers=ob,sd", True, "Order blocks and supply / demand"),
    "fvg": ("BTCUSDT", "tf=1H&layers=fvg", True, "Fair value gaps"),
    "liquidity": ("BTCUSDT", "tf=1H&layers=liquidity,sweeps", True, "Liquidity pools, sweeps and inducement"),
    "premium-discount": ("BTCUSDT", "tf=4H&layers=pd", True, "Premium / discount, EQ and OTE"),
    "htf-projection": ("BTCUSDT", "tf=15m&layers=htf", True, "Weekly / daily / 4H POIs projected onto 15m"),
    "position": ("EURUSD", "tf=5m&layers=position,ghost,ob", True, "Trade plan as a position tool + ghost scenario"),
    "light-theme": ("EURUSD", "tf=1H&theme=light", False, "Light theme"),
}
HERO = ("BTCUSDT", ["1W", "1D", "4H", "1H", "15m", "5m"])


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


def serve(*args: str) -> dict:
    out = subprocess.run([sys.executable, str(SERVE), *args, "--json"], capture_output=True, text=True, timeout=180)
    return json.loads(out.stdout.strip().splitlines()[-1])


def start_dashboard(symbol: str) -> str:
    res = serve("--symbol", symbol, "--detach", "--replace", "--no-browser")
    if res.get("status") not in ("started", "already_running"):
        sys.exit(f"dashboard for {symbol} failed: {res}")
    url = res["url"]
    for _ in range(120):  # wait until the analysis is loaded, not just the HTTP server
        try:
            import urllib.request
            meta = json.loads(urllib.request.urlopen(f"{url}/api/meta", timeout=3).read())
            if meta.get("status") == "ready":
                return url
        except OSError:
            pass
        time.sleep(1)
    sys.exit(f"dashboard for {symbol} never became ready")


def screenshot(browser: str, url: str, path: Path, profile: str) -> None:
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                    f"--window-size={SIZE[0]},{SIZE[1]}", "--virtual-time-budget=20000",
                    f"--screenshot={path}", url], capture_output=True, timeout=120, check=False)
    if not path.exists():
        sys.exit(f"browser produced no screenshot for {url}")


def compress(src: Path, dst: Path, crop: bool, width: int) -> None:
    img = Image.open(src).convert("RGB")
    if crop:
        img = img.crop(CHART_BOX)
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).save(dst, optimize=True)


def make_hero(frames: list[Path], dst: Path, width: int = 1000) -> None:
    imgs = []
    for f in frames:
        im = Image.open(f).convert("RGB")
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        imgs.append(im.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
    imgs[0].save(dst, save_all=True, append_images=imgs[1:], duration=1400, loop=0, optimize=True, disposal=2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", default="", help="comma-separated subset of: hero," + ",".join(SHOTS))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    browser = find_browser()
    tmp = Path(tempfile.mkdtemp(prefix="smc-media-"))
    jobs = {n: s for n, s in SHOTS.items() if not only or n in only}
    want_hero = not only or "hero" in only
    symbols = list(dict.fromkeys([s[0] for s in jobs.values()] + ([HERO[0]] if want_hero else [])))
    try:
        for symbol in symbols:
            url = start_dashboard(symbol)
            print(f"[{symbol}] dashboard ready at {url}")
            for name, (sym, query, crop, _) in jobs.items():
                if sym != symbol:
                    continue
                raw = tmp / f"{name}.png"
                screenshot(browser, f"{url}/?{query}", raw, str(tmp / f"profile-{name}"))
                compress(raw, out / f"{name}.png", crop, 1200 if crop else 1400)
                print(f"  {name}.png  ({(out / f'{name}.png').stat().st_size // 1024} KB)")
            if want_hero and symbol == HERO[0]:
                frames = []
                for tf in HERO[1]:
                    raw = tmp / f"hero-{tf}.png"
                    screenshot(browser, f"{url}/?tf={tf}", raw, str(tmp / f"profile-hero-{tf}"))
                    frames.append(raw)
                make_hero(frames, out / "hero.gif")
                print(f"  hero.gif  ({(out / 'hero.gif').stat().st_size // 1024} KB, {len(frames)} frames)")
    finally:
        serve("--stop")
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
