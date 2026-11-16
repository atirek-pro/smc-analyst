"""Top-down multi-timeframe cascade, copying a discretionary SMC trader's routine.

    Stage 1  Weekly (Monthly optional)  HTF bias, HTF POIs, draw on liquidity
    Stage 2  Daily                      confirm the bias, pick the target POI in discount/premium
    Stage 3  4H                         refine the POI, mark inducement
    Stage 4  1H / 15m                   wait for the POI tap, the sweep and the LTF CHoCH
    Stage 5  5m / 1m                    entry zone (OB/FVG of the confirming leg), invalidation, targets

**Gate rule:** the cascade stops at the first stage that fails or is still waiting, and
reports the missing condition in ``waiting_for``. It never forces a setup.

This module only *identifies* the setup: zones, levels and confirmation. Turning it into
entry/SL/TP prices, R:R and sizing is Phase 4 (``signal.py``).

CLI (run from the scripts/ directory):
    python -m smc.mtf BTCUSDT
    python -m smc.mtf EURUSD --json
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import pandas as pd

from . import data
from .common import BEAR, BULL, closed_bars, dir_name, fmt_price, fmt_time, index_at_or_after, jsonable
from .config import get_config
from .indicators import atr as atr_series
from .inducement import Inducement, find_inducement
from .liquidity import LiquidityLevel, LiquidityResult, analyze_liquidity, period_levels
from .pd_array import DealingRange, dealing_range
from .sessions import killzone_at, killzones_relevant
from .structure import StructureBreak, StructureResult, analyze_structure
from .swings import Swing, find_swings
from .zones import Zone, ZoneResult, analyze_zones

log = logging.getLogger("smc.mtf")
TF_ORDER = ["1M", "1W", "1D", "4H", "1H", "15m", "5m", "1m"]
ZONE_PRIORITY = {"order_block": 0, "fvg": 1, "demand": 2, "supply": 2}


# =============================================================================
# Per-timeframe analysis
# =============================================================================

@dataclass
class TimeframeAnalysis:
    tf: str
    df: pd.DataFrame = field(metadata={"json": False}, repr=False)
    price: float                      # latest price (includes the forming bar)
    atr: float | None                 # latest ATR on closed bars
    swings: list[Swing]
    internal_swings: list[Swing]
    structure: StructureResult
    internal: StructureResult
    liquidity: LiquidityResult
    zones: ZoneResult
    dealing_range: DealingRange | None
    bars: int

    def to_dict(self) -> dict:
        return jsonable(self)


def analyze_timeframe(df_raw: pd.DataFrame, tf: str, cfg: dict | None = None,
                      extra_levels: list[LiquidityLevel] | None = None) -> TimeframeAnalysis:
    """Run every detector on one timeframe (closed bars only)."""
    cfg = cfg or get_config()
    tf = data.normalize_timeframe(tf)
    df = closed_bars(df_raw, tf)
    price = float(df_raw["close"].iloc[-1]) if len(df_raw) else math.nan
    atr_s = atr_series(df, int(cfg["atr"]["period"]))
    last_atr = None if atr_s.empty or pd.isna(atr_s.iloc[-1]) else float(atr_s.iloc[-1])
    lookback = int(cfg["swings"]["lookback"].get(tf, 5))
    sw = find_swings(df, lookback, level="swing")
    isw = find_swings(df, int(cfg["swings"]["internal_lookback"]), level="internal")
    conf = cfg["structure"]["break_confirmation"]
    st = analyze_structure(df, sw, "swing", conf)
    ist = analyze_structure(df, isw, "internal", conf)
    zones = analyze_zones(df, atr_s, st.breaks + ist.breaks, cfg["zones"], tf)
    liq = analyze_liquidity(df, sw, isw, atr_s, float(cfg["liquidity"]["equal_level_tolerance_atr"]),
                            bool(cfg["liquidity"]["sweep_requires_close_back_inside"]), extra_levels)
    pdc = cfg["pd_array"]
    dr = dealing_range(df_raw, sw, float(pdc["equilibrium"]), tuple(pdc["ote"]),
                       float(pdc.get("equilibrium_band", 0.05)))
    return TimeframeAnalysis(tf, df, price, last_atr, sw, isw, st, ist, liq, zones, dr, len(df))


# =============================================================================
# Cascade result types
# =============================================================================

@dataclass
class StageResult:
    stage: int
    name: str
    timeframes: list[str]
    status: str                        # passed | failed | waiting | skipped
    rationale: list[str] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)


@dataclass
class CascadeResult:
    symbol: str
    asset_class: str
    price: float
    generated_at: str
    status: str = "no_setup"           # no_bias | no_setup | waiting | in_poi | ready
    bias: int = 0
    bias_source: str | None = None
    waiting_for: str | None = None
    stages: list[StageResult] = field(default_factory=list)
    htf_pois: list[Zone] = field(default_factory=list)
    target_poi: Zone | None = None
    refined_poi: Zone | None = None
    inducement: Inducement | None = None
    tap_time: pd.Timestamp | None = None
    sweep: LiquidityLevel | None = None
    confirmation: StructureBreak | None = None
    entry_zone: Zone | None = None
    invalidation_level: float | None = None
    targets: list[dict] = field(default_factory=list)
    draw_on_liquidity: LiquidityLevel | None = None
    alignment: dict = field(default_factory=dict)
    confluence: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    _json_extra = ("bias_name",)

    @property
    def bias_name(self) -> str:
        return dir_name(self.bias)

    def to_dict(self) -> dict:
        return jsonable(self)


# =============================================================================
# Helpers
# =============================================================================

def _side_ok(z: Zone, price: float, bias: int) -> bool:
    """Zone is on the entry side of price (below for longs, above for shorts) or contains it."""
    return z.bottom < price if bias == BULL else z.top > price


def _distance(z: Zone, price: float, bias: int) -> float:
    return max(0.0, price - z.top) if bias == BULL else max(0.0, z.bottom - price)


def _candidates(a: "TimeframeAnalysis", bias: int, price: float) -> list[Zone]:
    return [z for z in a.zones.select(direction=bias) if _side_ok(z, price, bias)]


def _where(dr: DealingRange, price: float) -> str:
    """'discount (32%)', or 'discount, below the range' when price has left the dealing range."""
    pos = dr.position(price)
    if pos < 0:
        return f"{dr.zone_of(price)}, below the range"
    if pos > 1:
        return f"{dr.zone_of(price)}, above the range"
    return f"{dr.zone_of(price)} ({pos:.0%})"


def _first(analyses: dict, *tfs: str) -> "TimeframeAnalysis | None":
    for tf in tfs:
        if tf in analyses and analyses[tf].bars:
            return analyses[tf]
    return None


class _Ctx:
    """Mutable state handed from stage to stage."""

    def __init__(self, analyses: dict[str, TimeframeAnalysis], res: CascadeResult, inst, cfg):
        self.A, self.res, self.inst, self.cfg = analyses, res, inst, cfg
        self.tick = getattr(inst, "tick_size", None)
        self.price = res.price
        self.daily_dol: LiquidityLevel | None = None
        self.tap_index: int | None = None
        self.ltf: TimeframeAnalysis | None = None
        self.points: dict[str, tuple[int, int, str]] = {}

    def p(self, x: float | None) -> str:
        return fmt_price(x, self.tick)

    def zone(self, z: Zone) -> str:
        return f"{z.label()} {self.p(z.bottom)}-{self.p(z.top)} ({z.state})"

    def score(self, key: str, got: int, maximum: int, why: str) -> None:
        self.points[key] = (got, maximum, why)


# =============================================================================
# Stages
# =============================================================================

def _stage1(c: _Ctx) -> StageResult:
    st = StageResult(1, "HTF bias", ["1M", "1W"] if "1M" in c.A else ["1W"], "failed")
    w, d = c.A.get("1W"), c.A.get("1D")
    bias, src = 0, None
    if w and w.structure.trend:
        bias, src = w.structure.trend, "1W"
    elif d and d.structure.trend:
        bias, src = d.structure.trend, "1D"
        st.rationale.append("Weekly structure is not established yet (too few confirmed swings), "
                            "so the daily trend is used as the higher-timeframe bias.")
    if not bias:
        st.rationale.append("No higher-timeframe bias: neither the weekly nor the daily chart has a "
                            "confirmed break of structure.")
        c.res.waiting_for = "a confirmed weekly or daily break of structure"
        c.res.status = "no_bias"
        c.score("htf_bias", 0, 15, "no HTF bias")
        return st

    htf = c.A[src]
    c.res.bias, c.res.bias_source = bias, src
    c.score("htf_bias", 15 if src == "1W" else 8, 15, f"bias from {src}")
    lb = htf.structure.last_break()
    st.rationale.append(f"{src} swing structure is {dir_name(bias)}: last {lb.kind} through "
                        f"{c.p(lb.level)} on {fmt_time(lb.break_time)}.")
    labels = [s.label for s in htf.swings[-4:] if s.label]
    if labels:
        st.rationale.append(f"Recent {src} pivots: {', '.join(labels)}.")
    if "1M" in c.A and c.A["1M"].structure.trend:
        mt = c.A["1M"].structure.trend
        st.rationale.append(f"Monthly structure is {dir_name(mt)}"
                            + (" (agrees)." if mt == bias else " (disagrees: treat the weekly move as a "
                                                              "larger pullback)."))
    dr = htf.dealing_range
    if dr:
        st.rationale.append(f"{src} dealing range {c.p(dr.low)}-{c.p(dr.high)}, EQ {c.p(dr.eq)}; price "
                            f"{c.p(c.price)} is in {_where(dr, c.price)}.")
    pois = sorted(_candidates(htf, bias, c.price), key=lambda z: _distance(z, c.price, bias))[:3]
    c.res.htf_pois = pois
    st.rationale.append("HTF POIs: " + ("; ".join(c.zone(z) for z in pois) if pois
                                        else f"no unmitigated {dir_name(bias)} {src} POI on the entry side."))
    dol = htf.liquidity.nearest_resting(c.price, bias)
    c.res.draw_on_liquidity = dol
    if dol:
        st.rationale.append(f"Draw on liquidity: {dol.label()} at {c.p(dol.price)} "
                            f"({'buy' if bias == BULL else 'sell'}-side).")
    elif dr:
        st.rationale.append(f"No resting {'buy' if bias == BULL else 'sell'}-side pool beyond price; the "
                            f"draw is the range extreme {c.p(dr.high if bias == BULL else dr.low)}.")
    st.status = "passed"
    st.outputs = {"bias": dir_name(bias), "source": src, "pois": pois, "draw_on_liquidity": dol,
                  "dealing_range": dr}
    return st


def _stage2(c: _Ctx) -> StageResult:
    st = StageResult(2, "Daily confirmation and target POI", ["1D"], "failed")
    bias, d = c.res.bias, c.A.get("1D")
    bn = dir_name(bias)
    if d is None:
        st.rationale.append("Daily data unavailable.")
        c.res.waiting_for = "daily data"
        return st
    lb = d.structure.last_break()
    if c.res.bias_source == "1D":
        st.rationale.append("The daily chart is the bias source, so it agrees by definition.")
        c.score("daily_aligned", 15, 15, "daily is bias source")
    elif d.structure.trend == -bias:
        st.rationale.append(f"Daily structure is {dir_name(d.structure.trend)} (last {lb.kind} through "
                            f"{c.p(lb.level)} on {fmt_time(lb.break_time)}), against the weekly {bn} bias. "
                            f"A daily CHoCH signals a possible reversal.")
        c.res.waiting_for = (f"the daily to realign with the weekly {bn} bias (a daily {bn} CHoCH/BOS)")
        c.score("daily_aligned", 0, 15, "daily against weekly")
        return st
    elif d.structure.trend == 0:
        st.rationale.append("Daily structure is undetermined; proceeding on the weekly bias.")
        c.res.warnings.append("daily structure undetermined")
        c.score("daily_aligned", 5, 15, "daily undetermined")
    else:
        st.rationale.append(f"Daily structure agrees: {lb.kind} {bn} through {c.p(lb.level)} on "
                            f"{fmt_time(lb.break_time)}.")
        c.score("daily_aligned", 15, 15, "daily aligned")

    dr = d.dealing_range
    want = "discount" if bias == BULL else "premium"
    if dr:
        st.rationale.append(f"Daily dealing range {c.p(dr.low)}-{c.p(dr.high)}, EQ {c.p(dr.eq)}; price is in "
                            f"{_where(dr, c.price)}.")
    cands = _candidates(d, bias, c.price)
    fav = [z for z in cands if dr is None or dr.zone_of(z.mid) == want]
    if not fav:
        st.rationale.append(f"No unmitigated daily {bn} POI in {want}"
                            + (f" ({len(cands)} found outside {want})." if cands else "."))
        c.res.waiting_for = f"a daily {bn} POI in {want} (usually left by the next daily {bn} BOS)"
        c.score("poi_location", 0, 10, f"no POI in {want}")
        return st
    c.score("poi_location", 10 if dr else 5, 10, f"POI in {want}")
    tol = 0.25 * (d.atr or 0.0)
    overlap = lambda z: any(z.overlaps(h, tol) for h in c.res.htf_pois)  # noqa: E731
    # A trader works the *current* dealing range first: POIs inside it beat distant ones,
    # then HTF confluence, then zone type (OB > FVG > supply/demand), then proximity.
    in_range = lambda z: dr is None or dr.low <= z.mid <= dr.high  # noqa: E731
    best = min(fav, key=lambda z: (not in_range(z), not overlap(z), ZONE_PRIORITY[z.kind],
                                   _distance(z, c.price, bias)))
    if not in_range(best):
        st.rationale.append("No POI inside the current daily range; using the nearest one beyond it.")
    c.res.target_poi = best
    if overlap(best):
        st.rationale.append(f"Target POI: {c.zone(best)}, inside/next to an HTF POI (confluence).")
        c.score("htf_overlap", 10, 10, "daily POI within HTF POI")
    else:
        st.rationale.append(f"Target POI: {c.zone(best)} (no HTF POI overlap).")
        c.score("htf_overlap", 0, 10, "no HTF overlap")
    c.daily_dol = d.liquidity.nearest_resting(c.price, bias)
    if c.daily_dol:
        st.rationale.append(f"Daily draw on liquidity: {c.daily_dol.label()} at {c.p(c.daily_dol.price)}.")
    st.status = "passed"
    st.outputs = {"target_poi": best, "daily_dol": c.daily_dol, "price_zone": dr.zone_of(c.price) if dr else None}
    return st


def _stage3(c: _Ctx) -> StageResult:
    st = StageResult(3, "4H refinement and inducement", ["4H"], "failed")
    bias, poi, h = c.res.bias, c.res.target_poi, c.A.get("4H")
    bn = dir_name(bias)
    if h is None:
        c.res.refined_poi = poi
        c.res.warnings.append("4H data unavailable; daily POI used unrefined")
        st.status, st.rationale = "skipped", ["4H data unavailable; using the daily POI unrefined."]
        c.score("refined", 0, 10, "no 4H data")
        c.score("inducement", 0, 10, "no 4H data")
        return st
    after = h.df[h.df.index >= poi.created_time]
    broken = after[after["close"] < poi.bottom] if bias == BULL else after[after["close"] > poi.top]
    if not broken.empty:
        st.rationale.append(f"The daily POI failed: 4H closed {'below' if bias == BULL else 'above'} "
                            f"{c.p(poi.distal)} on {fmt_time(broken.index[0])}.")
        c.res.waiting_for = f"a new daily {bn} POI (the previous one was invalidated)"
        return st

    inside = [z for z in h.zones.select(direction=bias) if z.overlaps(poi)]
    if inside:
        refined = min(inside, key=lambda z: (ZONE_PRIORITY[z.kind], -z.overlap_amount(poi), -z.created_index))
        st.rationale.append(f"Refined POI: {c.zone(refined)} inside the daily POI.")
        c.score("refined", 10, 10, "4H zone inside daily POI")
    else:
        refined = poi
        st.rationale.append("No 4H OB/FVG inside the daily POI; keeping the daily POI.")
        c.score("refined", 0, 10, "no 4H refinement")
    c.res.refined_poi = refined

    lb = h.structure.last_break()
    if lb and lb.direction == -bias and lb.kind == "BOS" and (
            (bias == BULL and lb.level < refined.bottom) or (bias == BEAR and lb.level > refined.top)):
        st.rationale.append(f"4H is breaking structure {dir_name(-bias)} beyond the POI ({lb.describe()}).")
        c.res.waiting_for = f"4H {bn} structure to hold the POI"
        return st
    if h.structure.trend == -bias:
        st.rationale.append(f"4H swing structure is {dir_name(-bias)}: the expected pullback into the POI.")
    elif h.structure.trend == bias:
        st.rationale.append(f"4H swing structure is {bn}, aligned with the bias.")
    st.rationale.append(f"4H internal structure is {h.internal.trend_name}.")

    ind = find_inducement(h.df, h.internal_swings, refined, bias)
    c.res.inducement = ind
    if ind is None:
        st.rationale.append("No inducement pivot in front of the POI yet.")
        c.score("inducement", 0, 10, "no inducement found")
    elif ind.swept:
        st.rationale.append(f"Inducement at {c.p(ind.price)} was swept on {fmt_time(ind.swept_time)}.")
        c.score("inducement", 10, 10, "inducement swept")
    else:
        st.rationale.append(f"Inducement at {c.p(ind.price)} is still resting (expected to be taken first).")
        c.score("inducement", 0, 10, "inducement not swept")
    st.status = "passed"
    st.outputs = {"refined_poi": refined, "inducement": ind, "trend_4h": h.structure.trend_name}
    return st


def _stage4(c: _Ctx) -> StageResult:
    ctf = c.cfg["structure"].get("confirmation_timeframe", "15m")
    L = _first(c.A, ctf, "1H")
    st = StageResult(4, "POI tap and LTF confirmation", [L.tf] if L else [ctf], "failed")
    bias, poi = c.res.bias, c.res.refined_poi
    bn = dir_name(bias)
    if L is None:
        st.rationale.append("No 1H/15m data available.")
        c.res.waiting_for = "lower-timeframe data"
        return st
    c.ltf = L
    df = L.df
    start = index_at_or_after(df, poi.created_time)
    lows, highs, closes = df["low"].to_numpy(), df["high"].to_numpy(), df["close"].to_numpy()
    tap = None
    for j in range(start, len(df)):
        if (bias == BULL and lows[j] <= poi.top) or (bias == BEAR and highs[j] >= poi.bottom):
            tap = j
            break
    if tap is None:
        dist = _distance(poi, c.price, bias)
        pct = dist / c.price * 100 if c.price else 0
        st.status = "waiting"
        st.rationale.append(f"Price {c.p(c.price)} has not reached the POI {c.p(poi.bottom)}-{c.p(poi.top)} "
                            f"on {L.tf} yet ({c.p(dist)} / {pct:.2f}% away).")
        ind = c.res.inducement
        if ind and not ind.swept:
            st.rationale.append(f"Expect the inducement at {c.p(ind.price)} to be swept on the way.")
        c.res.status = "waiting"
        c.res.waiting_for = f"price to reach the {poi.label()} at {c.p(poi.bottom)}-{c.p(poi.top)}"
        c.score("sweep", 0, 10, "POI not reached")
        c.score("ltf_choch", 0, 10, "POI not reached")
        return st
    c.tap_index = tap
    c.res.tap_time = df.index[tap]
    st.rationale.append(f"Price tapped the POI on {L.tf} at {fmt_time(df.index[tap])}.")
    fail = next((j for j in range(tap, len(df))
                 if (bias == BULL and closes[j] < poi.bottom) or (bias == BEAR and closes[j] > poi.top)), None)
    if fail is not None:
        st.rationale.append(f"POI failed: {L.tf} closed {'below' if bias == BULL else 'above'} "
                            f"{c.p(poi.distal)} on {fmt_time(df.index[fail])}.")
        c.res.waiting_for = f"a new {bn} POI (the refined POI was invalidated)"
        c.score("sweep", 0, 10, "POI invalidated")
        c.score("ltf_choch", 0, 10, "POI invalidated")
        return st

    breaks = sorted(L.structure.breaks_after(tap, bias) + L.internal.breaks_after(tap, bias),
                    key=lambda b: (b.break_index, b.kind != "CHoCH"))
    # The *latest* CHoCH defines the current setup (an earlier attempt may have been invalidated
    # and re-formed while the POI held). Without a CHoCH (the LTF already trended with the bias),
    # the first BOS after the tap is the confirmation.
    choch = [b for b in breaks if b.kind == "CHoCH"]
    confirm = choch[-1] if choch else (breaks[0] if breaks else None)

    # Liquidity sweep AT the POI, between the tap and the confirmation: an opposite-side pool
    # (sell-side for a long) lying in or just beyond the zone, whose wick was taken and closed back.
    opp = "sell" if bias == BULL else "buy"
    tol = L.atr or 0.0
    window_end = confirm.break_index if confirm else len(df) - 1
    sweeps = [lv for lv in L.liquidity.levels if lv.side == opp and lv.status == "swept"
              and lv.status_index is not None and tap - 1 <= lv.status_index <= window_end
              and poi.bottom - tol <= lv.price <= poi.top + tol]
    sweep = min(sweeps, key=lambda lv: lv.status_index) if sweeps else None
    ind = c.res.inducement
    if sweep:
        c.res.sweep = sweep
        st.rationale.append(f"Liquidity sweep at the POI: {sweep.label()} {c.p(sweep.price)} on "
                            f"{fmt_time(sweep.status_time)}.")
        c.score("sweep", 10, 10, "sweep at POI")
    elif ind and ind.swept:
        st.rationale.append("No separate sweep at the POI, but the 4H inducement was taken.")
        c.score("sweep", 5, 10, "inducement only")
    else:
        st.rationale.append("No liquidity sweep at the POI.")
        c.score("sweep", 0, 10, "no sweep")

    if confirm is None:
        ref = (L.internal.active_high or L.structure.active_high) if bias == BULL else \
              (L.internal.active_low or L.structure.active_low)
        lvl = f"a close {'above' if bias == BULL else 'below'} {c.p(ref.price)}" if ref else \
            "a new swing to form and break"
        st.status = "waiting"
        st.rationale.append(f"Inside the POI; waiting for a {L.tf} {bn} CHoCH ({lvl}).")
        c.res.status = "in_poi"
        c.res.waiting_for = f"a {L.tf} {bn} CHoCH: {lvl}"
        c.score("ltf_choch", 0, 10, "no LTF CHoCH yet")
        return st
    c.res.confirmation = confirm
    st.rationale.append(f"Confirmation: {confirm.describe()}.")
    c.score("ltf_choch", 10 if confirm.kind == "CHoCH" else 5, 10, f"{L.tf} {confirm.kind}")
    st.status = "passed"
    st.outputs = {"tap_time": c.res.tap_time, "sweep": sweep, "confirmation": confirm}
    return st


def _pick_entry_zone(a: TimeframeAnalysis, bias: int, t0: pd.Timestamp, t1: pd.Timestamp) -> Zone | None:
    zs = [z for z in a.zones.select(direction=bias) if z.kind in ("order_block", "fvg")
          and t0 <= z.created_time <= t1]
    return min(zs, key=lambda z: (ZONE_PRIORITY[z.kind], -z.created_index)) if zs else None


def _stage5(c: _Ctx) -> StageResult:
    etf = c.cfg["structure"].get("entry_timeframe", "5m")
    L, conf, bias = c.ltf, c.res.confirmation, c.res.bias
    E = _first(c.A, etf) or L
    st = StageResult(5, "Entry refinement", [E.tf] + (["1m"] if "1m" in c.A and E.tf != "1m" else []),
                     "failed")
    slack = pd.Timedelta(seconds=3 * data.TF_SECONDS[L.tf])
    t0, t1 = c.res.tap_time, conf.break_time + slack
    zone = _pick_entry_zone(E, bias, t0, t1)
    if zone is None and E is not L:
        zone = _pick_entry_zone(L, bias, t0, t1)
    if zone is None:
        zone = c.res.refined_poi
        st.rationale.append("No OB/FVG from the confirming leg on the LTF; the entry zone falls back to the "
                            "refined POI.")
        c.score("entry_zone", 0, 5, "no LTF entry zone")
    else:
        st.rationale.append(f"Entry zone: {c.zone(zone)}, created by the confirming leg.")
        c.score("entry_zone", 5, 5, "LTF OB/FVG")
    one = c.A.get("1m")
    if one is not None and zone.timeframe != "1m":
        fine = [z for z in one.zones.select(direction=bias) if z.kind in ("order_block", "fvg")
                and z.overlap_amount(zone) > 0 and t0 <= z.created_time <= t1]
        if fine:
            zone = min(fine, key=lambda z: z.top - z.bottom)
            st.rationale.append(f"Refined on 1m: {c.zone(zone)}.")
    c.res.entry_zone = zone

    # Invalidation = the extreme of the leg that produced the confirming CHoCH (its origin):
    # the low that swept liquidity before a bullish CHoCH, or the high before a bearish one.
    df = L.df
    o = conf.origin_index
    inval = float(df["low"].iloc[o]) if bias == BULL else float(df["high"].iloc[o])
    c.res.invalidation_level = inval
    st.rationale.append(f"Invalidation (extreme of the leg that made the CHoCH, {fmt_time(df.index[o])}): "
                        f"{c.p(inval)}.")

    # Targets as they stood at confirmation: the nearest pool beyond the confirmation close that
    # was known then and not yet hit (internal), then the daily and HTF draws (external).
    ref = float(df["close"].iloc[conf.break_index])
    side = "buy" if bias == BULL else "sell"
    known = [lv for lv in L.liquidity.levels if lv.side == side and lv.active_from <= conf.break_time
             and (lv.price > ref if bias == BULL else lv.price < ref)
             and (lv.resting or (lv.status_index is not None and lv.status_index > conf.break_index))]
    internal = min(known, key=lambda lv: abs(lv.price - ref), default=None)
    targets: list[dict] = []
    seen: set[float] = set()
    for role, lv in [("internal", internal), ("external", c.daily_dol), ("external", c.res.draw_on_liquidity)]:
        if lv is not None and lv.price not in seen:
            seen.add(lv.price)
            targets.append({"role": role, "level": lv})
    c.res.targets = targets
    if targets:
        st.rationale.append("Targets: " + "; ".join(f"{t['role']} {t['level'].label()} {c.p(t['level'].price)}"
                                                    for t in targets) + ".")

    # Has the setup already played out since confirmation?
    after = df.iloc[conf.break_index + 1:]
    closes = after["close"].to_numpy()
    inv_hit = next((i for i, x in enumerate(closes) if (x < inval if bias == BULL else x > inval)), None)
    tgt_hit = internal.status_index - conf.break_index - 1 if internal and internal.status_index is not None else None
    if inv_hit is not None and (tgt_hit is None or inv_hit <= tgt_hit):
        st.rationale.append(f"Setup invalidated after confirmation: {L.tf} closed beyond {c.p(inval)} on "
                            f"{fmt_time(after.index[inv_hit])}.")
        c.res.waiting_for = f"a fresh {dir_name(bias)} setup (the last one was invalidated)"
        return st
    if tgt_hit is not None:
        st.rationale.append(f"Setup already played out: the first target {c.p(internal.price)} was reached on "
                            f"{fmt_time(internal.status_time)}.")
        c.res.waiting_for = f"a fresh {dir_name(bias)} POI (the last setup already reached its first target)"
        return st
    retest = after[(after["low"] <= zone.top) if bias == BULL else (after["high"] >= zone.bottom)]
    st.rationale.append(f"Entry zone retested on {fmt_time(retest.index[0])}." if not retest.empty
                        else "Entry zone not retested yet since confirmation.")
    inst_cls = getattr(c.inst, "asset_class", "")
    if killzones_relevant(inst_cls):
        kz = killzone_at(conf.break_time, c.cfg["sessions"])
        st.rationale.append(f"Confirmation {'inside the ' + kz + ' killzone' if kz else 'outside the killzones'}.")
        c.score("timing", 5 if kz else 0, 5, f"killzone: {kz}")
    else:
        hourly = c.A.get("1H")
        ok = bool(hourly and hourly.internal.trend == bias)
        c.score("timing", 5 if ok else 0, 5, "1H internal aligned" if ok else "1H internal not aligned")
    st.status = "passed"
    st.outputs = {"entry_zone": zone, "invalidation_level": inval, "targets": targets}
    c.res.status = "ready"
    c.res.waiting_for = None
    return st


# =============================================================================
# Orchestration
# =============================================================================

CONFLUENCE_FACTORS = [
    ("htf_bias", 15), ("daily_aligned", 15), ("poi_location", 10), ("htf_overlap", 10),
    ("refined", 10), ("inducement", 10), ("sweep", 10), ("ltf_choch", 10), ("entry_zone", 5), ("timing", 5),
]


def alignment_matrix(analyses: dict[str, TimeframeAnalysis], bias: int, price: float) -> dict:
    out = {}
    for tf in TF_ORDER:
        a = analyses.get(tf)
        if a is None:
            continue
        lb = a.structure.last_break()
        out[tf] = {
            "swing_trend": a.structure.trend_name,
            "internal_trend": a.internal.trend_name,
            "last_break": f"{lb.kind} {lb.direction_name}" if lb else None,
            "pd_zone": a.dealing_range.zone_of(price) if a.dealing_range else None,
            "aligned": bool(bias) and a.structure.trend == bias,
        }
    return out


def run_cascade(analyses: dict[str, TimeframeAnalysis], inst=None, cfg: dict | None = None) -> CascadeResult:
    """Run the 5-stage top-down cascade over per-timeframe analyses."""
    cfg = cfg or get_config()
    lowest = next((analyses[tf] for tf in reversed(TF_ORDER) if tf in analyses and analyses[tf].bars), None)
    price = lowest.price if lowest else math.nan
    res = CascadeResult(symbol=getattr(inst, "symbol", "?"), asset_class=getattr(inst, "asset_class", "?"),
                        price=price, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    c = _Ctx(analyses, res, inst, cfg)
    for stage_fn in (_stage1, _stage2, _stage3, _stage4, _stage5):
        st = stage_fn(c)
        res.stages.append(st)
        if st.status in ("failed", "waiting"):
            if res.status not in ("no_bias", "waiting", "in_poi"):
                res.status = "no_setup"
            break
    res.alignment = alignment_matrix(analyses, res.bias, price)
    breakdown = []
    for key, mx in CONFLUENCE_FACTORS:
        got, _, why = c.points.get(key, (0, mx, "not reached"))
        breakdown.append({"factor": key, "points": got, "max": mx, "note": why})
    res.confluence = {"score": sum(b["points"] for b in breakdown), "breakdown": breakdown}
    return res


def analyze_symbol(symbol: "str | data.Instrument", cfg: dict | None = None, limit: int | None = None
                   ) -> tuple[CascadeResult, dict[str, TimeframeAnalysis]]:
    """Fetch the ladder, analyse every timeframe and run the cascade.

    ``symbol`` may be a string or an already-resolved :class:`data.Instrument` (e.g. one
    forced to an asset class after the user clarified an ambiguous input).
    """
    cfg = cfg or get_config()
    inst = symbol if isinstance(symbol, data.Instrument) else data.resolve_symbol(symbol)
    tfs = list(cfg["timeframes"]["ladder"])
    if cfg["timeframes"].get("include_monthly") and "1M" not in tfs:
        tfs.insert(0, "1M")
    frames: dict[str, pd.DataFrame] = {}
    warnings: list[str] = []
    for tf in tfs:
        try:
            frames[tf] = data.get_ohlcv(inst, tf, limit)
        except data.SymbolError:
            raise
        except data.DataError as exc:
            warnings.append(f"{tf} unavailable: {exc}")
    if not frames:
        raise data.DataError(f"No data for {symbol}: " + "; ".join(warnings))
    first = next(iter(frames.values()))
    info = first.attrs.get("instrument", {})
    inst = replace(inst, tick_size=info.get("tick_size", inst.tick_size), provider=info.get("provider", inst.provider),
                   provider_symbol=info.get("provider_symbol", inst.provider_symbol))
    daily = closed_bars(frames["1D"], "1D") if "1D" in frames else None
    weekly = closed_bars(frames["1W"], "1W") if "1W" in frames else None
    extra = period_levels(daily, weekly)
    analyses = {tf: analyze_timeframe(df, tf, cfg, extra) for tf, df in frames.items()}
    res = run_cascade(analyses, inst, cfg)
    res.warnings.extend(warnings)
    return res, analyses


# =============================================================================
# CLI
# =============================================================================

def format_report(res: CascadeResult) -> str:
    lines = [f"{res.symbol} [{res.asset_class}] price {res.price} | bias: {res.bias_name}"
             f"{' (' + res.bias_source + ')' if res.bias_source else ''} | status: {res.status.upper()}"
             f" | confluence {res.confluence.get('score', 0)}/100", ""]
    for st in res.stages:
        lines.append(f"Stage {st.stage} - {st.name} [{', '.join(st.timeframes)}]: {st.status.upper()}")
        lines.extend(f"   - {r}" for r in st.rationale)
    if res.waiting_for:
        lines += ["", f"No setup yet - waiting for {res.waiting_for}."]
    lines += ["", "Alignment: " + " | ".join(f"{tf} {v['swing_trend'][:4]}/{v['internal_trend'][:4]}"
                                             for tf, v in res.alignment.items())]
    if res.warnings:
        lines.append("Warnings: " + "; ".join(res.warnings))
    lines.append("Educational analysis only - not financial advice.")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m smc.mtf", description="Top-down SMC cascade.")
    ap.add_argument("symbol")
    ap.add_argument("--json", action="store_true", help="print the cascade result as JSON")
    ap.add_argument("--limit", type=int, default=None, help="bars per timeframe (default: config)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        res, _ = analyze_symbol(args.symbol, limit=args.limit)
    except (data.DataError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}))
        return 1
    print(json.dumps(res.to_dict()) if args.json else format_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
