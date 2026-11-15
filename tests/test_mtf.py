"""Phase 3: per-timeframe analysis and the top-down cascade (gate rule, statuses, scoring)."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from smc_builders import T0, candles, zigzag

from smc.config import load_config
from smc.data import resolve_symbol
from smc.liquidity import LiquidityLevel, LiquidityResult
from smc.mtf import CONFLUENCE_FACTORS, TimeframeAnalysis, analyze_timeframe, run_cascade
from smc.pd_array import DealingRange
from smc.structure import StructureBreak, StructureResult
from smc.swings import Swing
from smc.zones import Zone, ZoneResult

CFG = load_config()
BTC = resolve_symbol("BTCUSDT")


# --- analyze_timeframe on a synthetic impulsive uptrend -----------------------------------------------

def test_analyze_timeframe_detects_structure_zones_and_serialises():
    # impulsive up legs (2 bars) and slow pullbacks (6 bars) -> displacement -> order blocks
    pts = [100, 120, 110, 135, 124, 150, 138, 165, 150]
    legs = [2, 6, 2, 6, 2, 6, 2, 6]
    df = zigzag(pts, legs=legs, freq="4h", wick=0.3)
    a = analyze_timeframe(df, "4H", CFG)
    assert a.structure.trend == 1
    assert [b.kind for b in a.structure.breaks][:1] == ["BOS"]
    assert any(z.kind == "order_block" and z.direction == 1 for z in a.zones.zones)
    assert a.dealing_range is not None and a.atr is not None
    json.dumps(a.to_dict())  # must be JSON-serialisable (df excluded)
    assert "df" not in a.to_dict()


def test_analyze_timeframe_drops_forming_bar():
    df = zigzag([100, 120, 105, 130], bars_per_leg=5)
    df.attrs["last_bar_closed"] = False
    a = analyze_timeframe(df, "1H", CFG)
    assert a.bars == len(df) - 1 and a.price == df["close"].iloc[-1]


# --- synthetic TimeframeAnalysis builders ------------------------------------------------------------

def frame(n=20, price=105.0, freq="15min", start=T0):
    return candles([(price, price + 0.5, price - 0.5, price)] * n, start=start, freq=freq)


def brk(direction, kind="BOS", level=100.0, at=5, origin=3, df=None, structure="swing"):
    idx = df.index if df is not None else pd.date_range(T0, periods=max(at, origin) + 1, freq="1h")
    return StructureBreak(kind, direction, level, max(0, origin - 1), idx[max(0, origin - 1)], at, idx[at],
                          origin, idx[origin], structure)


def ta(tf, df=None, trend=0, breaks=(), zones=(), levels=(), dr=None, internal_trend=0, internal_breaks=(),
       internal_swings=(), price=None, atr=1.0):
    df = df if df is not None else frame()
    return TimeframeAnalysis(
        tf=tf, df=df, price=price if price is not None else float(df["close"].iloc[-1]), atr=atr,
        swings=[], internal_swings=list(internal_swings),
        structure=StructureResult("swing", trend, list(breaks)),
        internal=StructureResult("internal", internal_trend, list(internal_breaks)),
        liquidity=LiquidityResult(list(levels)), zones=ZoneResult(list(zones)), dealing_range=dr, bars=len(df))


def zone(kind, d, top, bottom, tf, created=T0, idx=0):
    return Zone(kind, d, top, bottom, idx, created, idx, created, tf, "test")


def lvl(price, side, kind="swing_high", active=T0, status="resting", si=None, st=None):
    return LiquidityLevel(price, side, kind, active, active, None, 1, status, si, st)


DR_D = DealingRange(130, 90, T0, T0, 1, 110, 105.2, 98.4, 2.0)   # daily range 90-130, EQ 110


def bullish_htf(daily_zones=None, daily_trend=1):
    w = ta("1W", trend=1, breaks=[brk(1)], levels=[lvl(130, "buy", "PWH")], price=105)
    d = ta("1D", trend=daily_trend, breaks=[brk(daily_trend or 1)], dr=DR_D, atr=2.0, price=105,
           zones=daily_zones if daily_zones is not None else [zone("order_block", 1, 100, 95, "1D")],
           levels=[lvl(120, "buy", "PDH")])
    h4 = ta("4H", df=frame(30, 104, "4h"), trend=1, breaks=[brk(1)],
            zones=[zone("order_block", 1, 99, 96, "4H")])
    return {"1W": w, "1D": d, "4H": h4}


def ltf_frame(tap=True, confirm=True, after=()):
    """15m bars: drift at 104, dip into the 96-99 POI at bar 5, low 97.5 at bar 6, rally to 105."""
    rows = [(104, 104.5, 103.5, 104)] * 5
    rows += [(104, 104, 98.5, 99), (99, 99.5, 97.5, 98.5)] if tap else [(104, 104.5, 103, 103.5)] * 2
    rows += [(98.5, 101, 98.5, 100.8), (100.8, 102, 100.5, 101.8), (101.8, 103, 101.5, 102.8),
             (102.8, 104.5, 102.5, 104.2)]
    rows += list(after) or [(104.2, 105.2, 104, 105)] * 3
    return candles(rows, freq="15min")


def run_bullish(ltf_rows_after=(), with_confirm=True, tap=True, target_status=("resting", None)):
    A = bullish_htf()
    df = ltf_frame(tap=tap, after=ltf_rows_after)
    breaks = [brk(1, "CHoCH", 102.0, at=9, origin=6, df=df, structure="internal")] if with_confirm else []
    ob = zone("order_block", 1, 99.5, 97.5, "15m", created=df.index[9], idx=6)
    status, si = target_status
    target = lvl(108, "buy", active=df.index[2], status=status, si=si, st=df.index[si] if si is not None else None)
    sweep = lvl(98.0, "sell", "swing_low", active=df.index[1], status="swept", si=6, st=df.index[6])
    A["15m"] = ta("15m", df=df, trend=-1, internal_trend=1 if with_confirm else -1, internal_breaks=breaks,
                  zones=[ob], levels=[target, sweep], atr=1.0)
    return run_cascade(A, BTC, CFG)


# --- gate rule and statuses ----------------------------------------------------------------------------

def test_no_bias_when_no_structure():
    res = run_cascade({"1W": ta("1W"), "1D": ta("1D"), "15m": ta("15m")}, BTC, CFG)
    assert res.status == "no_bias" and res.bias == 0
    assert len(res.stages) == 1 and res.stages[0].status == "failed"
    assert "break of structure" in res.waiting_for


def test_daily_bias_used_when_weekly_undetermined():
    A = bullish_htf()
    A["1W"] = ta("1W")
    res = run_cascade(A, BTC, CFG)
    assert res.bias_source == "1D" and res.bias == 1


def test_daily_against_weekly_stops_at_stage_2():
    res = run_cascade(bullish_htf(daily_trend=-1), BTC, CFG)
    assert res.status == "no_setup" and len(res.stages) == 2
    assert res.stages[1].status == "failed" and "realign" in res.waiting_for


def test_no_poi_in_discount_stops_at_stage_2():
    premium_zone = zone("order_block", 1, 104, 102, "1D")      # mid 103 > EQ? no: EQ 110 -> discount
    above_eq = zone("demand", 1, 128, 125, "1D")               # in premium, but above price -> not a candidate
    res = run_cascade(bullish_htf(daily_zones=[above_eq]), BTC, CFG)
    assert res.status == "no_setup" and "POI in discount" in res.waiting_for
    res2 = run_cascade(bullish_htf(daily_zones=[premium_zone]), BTC, CFG)
    assert res2.target_poi is premium_zone or res2.target_poi.top == 104


def test_waiting_for_price_to_reach_poi():
    A = bullish_htf()
    A["15m"] = ta("15m", df=frame(20, 105), trend=1)
    res = run_cascade(A, BTC, CFG)
    assert res.status == "waiting" and res.stages[-1].stage == 4 and res.stages[-1].status == "waiting"
    assert res.refined_poi.timeframe == "4H" and "price to reach" in res.waiting_for


def test_in_poi_waiting_for_ltf_choch():
    res = run_bullish(with_confirm=False)
    assert res.status == "in_poi" and "CHoCH" in res.waiting_for
    assert res.tap_time is not None and res.confirmation is None


def test_ready_setup_has_entry_invalidation_targets():
    res = run_bullish()
    assert res.status == "ready", res.waiting_for
    assert res.confirmation.kind == "CHoCH" and res.sweep.price == 98.0
    assert res.entry_zone.timeframe == "15m" and res.entry_zone.kind == "order_block"
    assert res.invalidation_level == 97.5                       # low of the CHoCH leg's origin bar
    assert [t["role"] for t in res.targets][:1] == ["internal"] and res.targets[0]["level"].price == 108
    assert res.waiting_for is None and len(res.stages) == 5
    assert 0 <= res.confluence["score"] <= 100
    json.dumps(res.to_dict())


def test_setup_played_out_when_first_target_hit():
    res = run_bullish(target_status=("swept", 12))
    assert res.status == "no_setup" and res.stages[-1].status == "failed"
    assert "played out" in " ".join(res.stages[-1].rationale)


def test_setup_invalidated_after_confirmation():
    res = run_bullish(ltf_rows_after=[(104, 104, 97, 97.2), (97.2, 98, 96.5, 97)])  # closes < 97.5, > 96
    assert res.status == "no_setup"
    assert "invalidated after confirmation" in " ".join(res.stages[-1].rationale)


def test_poi_failure_on_ltf_close_beyond_distal():
    res = run_bullish(ltf_rows_after=[(104, 104, 95, 95.5)])  # close below POI bottom 96
    assert res.status == "no_setup" and "POI failed" in " ".join(res.stages[-1].rationale)


def test_daily_poi_invalidated_on_4h():
    A = bullish_htf()
    A["4H"] = ta("4H", df=frame(10, 94, "4h"), trend=1, breaks=[brk(1)])  # 4H closes below 95
    A["15m"] = ta("15m", df=frame(20, 105))
    res = run_cascade(A, BTC, CFG)
    assert res.stages[-1].stage == 3 and res.stages[-1].status == "failed"


def test_confluence_breakdown_sums_to_score():
    res = run_bullish()
    bd = res.confluence["breakdown"]
    assert [b["factor"] for b in bd] == [k for k, _ in CONFLUENCE_FACTORS]
    assert sum(b["max"] for b in bd) == 100
    assert res.confluence["score"] == sum(b["points"] for b in bd)


def test_alignment_matrix():
    res = run_bullish()
    assert res.alignment["1W"]["aligned"] and res.alignment["1D"]["swing_trend"] == "bullish"
    assert res.alignment["15m"]["internal_trend"] == "bullish"


def test_bearish_mirror():
    """Mirror the bullish 'waiting' scenario around 200 and expect a bearish bias."""
    w = ta("1W", trend=-1, breaks=[brk(-1)], price=95)
    dr = DealingRange(110, 70, T0, T0, -1, 90, 86.8, 94.8, 2.0)
    d = ta("1D", trend=-1, breaks=[brk(-1)], dr=dr, atr=2.0, price=95,
           zones=[zone("order_block", -1, 105, 100, "1D")])
    h4 = ta("4H", df=frame(30, 96, "4h"), trend=-1, breaks=[brk(-1)], zones=[zone("order_block", -1, 104, 101, "4H")])
    m15 = ta("15m", df=frame(20, 95), trend=-1)
    res = run_cascade({"1W": w, "1D": d, "4H": h4, "15m": m15}, BTC, CFG)
    assert res.bias == -1 and res.status == "waiting" and res.refined_poi.bottom == 101
