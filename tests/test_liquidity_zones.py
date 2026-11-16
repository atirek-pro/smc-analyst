"""Phase 3: liquidity, zones, dealing range, inducement, sessions."""

from __future__ import annotations

import pandas as pd
import pytest

from smc import liquidity as liq
from smc import sessions
from smc.inducement import find_inducement
from smc.pd_array import dealing_range
from smc.structure import StructureBreak
from smc.swings import Swing
from smc.zones import Zone, ZoneResult, analyze_zones, find_fvgs, find_order_blocks, find_supply_demand, update_states

from smc_builders import T0, candles


def sw(kind, i, price, df, confirm=1, level="internal"):
    return Swing(kind, i, df.index[i], price, i + confirm, df.index[min(i + confirm, len(df) - 1)], level)


def flat(n=20, price=100.0):
    return candles([(price, price + 1, price - 1, price)] * n)


def const_atr(df, v=1.0):
    return pd.Series(v, index=df.index)


# --- liquidity -----------------------------------------------------------------------

def test_equal_highs_within_tolerance():
    df = flat(10)
    piv = [sw("high", 1, 120.0, df), sw("high", 3, 120.05, df), sw("high", 5, 121.0, df)]
    eq = liq.equal_levels(piv, const_atr(df), 0.1)
    assert len(eq) == 1
    lv = eq[0]
    assert (lv.kind, lv.side, lv.price, lv.touches, lv.index) == ("equal_highs", "buy", 120.05, 2, 3)


def test_equal_lows_chain_of_three():
    df = flat(10)
    # each pivot is within 0.1 x ATR of the previous one (50.00 -> 50.08 -> 49.99)
    piv = [sw("low", 1, 50.0, df), sw("low", 3, 50.08, df), sw("low", 5, 49.99, df)]
    lv = liq.equal_levels(piv, const_atr(df), 0.1)[0]
    assert (lv.kind, lv.price, lv.touches) == ("equal_lows", 49.99, 3)
    # a gap larger than the tolerance breaks the chain
    piv2 = [sw("low", 1, 50.0, df), sw("low", 3, 50.08, df), sw("low", 5, 49.95, df)]
    assert liq.equal_levels(piv2, const_atr(df), 0.1)[0].touches == 2


def level(price, side, active=T0):
    return liq.LiquidityLevel(price, side, "swing_high" if side == "buy" else "swing_low", active, active)


@pytest.mark.parametrize("bar,require,status", [
    ((100, 101, 99, 99.5), True, "swept"),     # wick above, close back below
    ((100, 102, 99, 101.5), True, "taken"),    # closed beyond
    ((100, 102, 99, 101.5), False, "swept"),   # any trade-through counts
])
def test_buy_side_sweep_vs_taken(bar, require, status):
    df = candles([(99, 99.5, 98, 99), bar])
    lv = level(100.5, "buy")
    liq.evaluate_levels([lv], df, require)
    assert lv.status == status and lv.status_index == 1


def test_sell_side_sweep_and_resting():
    df = candles([(100, 101, 99, 100), (100, 100.5, 94, 98)])
    swept, resting = level(95, "sell"), level(90, "sell")
    liq.evaluate_levels([swept, resting], df)
    assert swept.status == "swept" and resting.status == "resting"


def test_level_not_judged_before_it_is_active():
    df = candles([(100, 110, 99, 100), (100, 101, 99, 100)])
    lv = level(105, "buy", active=df.index[1])
    liq.evaluate_levels([lv], df)
    assert lv.resting


def test_period_levels_from_closed_bars():
    daily = candles([(1, 5, 0, 2), (2, 7, 1, 3)], freq="1D")
    weekly = candles([(1, 9, -1, 2)], freq="7D")
    lv = {x.kind: x for x in liq.period_levels(daily, weekly)}
    assert (lv["PDH"].price, lv["PDL"].price, lv["PWH"].price, lv["PWL"].price) == (7, 1, 9, -1)
    assert lv["PDH"].active_from == daily.index[-1] + pd.Timedelta(days=1)


def test_nearest_resting_and_beyond():
    res = liq.LiquidityResult([level(110, "buy"), level(105, "buy"), level(95, "sell"), level(90, "sell")])
    assert res.nearest_resting(100, 1).price == 105
    assert res.nearest_resting(100, -1).price == 95
    assert [x.price for x in res.resting_beyond(100, 1)] == [105, 110]
    assert [x.price for x in res.resting_beyond(100, -1)] == [95, 90]


# --- FVG -------------------------------------------------------------------------------

def test_bullish_and_bearish_fvg():
    df = candles([(10, 11, 9, 10.5), (10.5, 15, 10.5, 14.5), (14.5, 16, 12, 15.5),     # bull gap 11..12
                  (15.5, 16, 15, 15), (15, 15, 9, 9.5), (9.5, 10, 8, 8.5)])           # bear gap 10..15
    fvgs = find_fvgs(df, const_atr(df, 1.0), 0.1, "1H")
    bull = [z for z in fvgs if z.direction == 1][0]
    bear = [z for z in fvgs if z.direction == -1][0]
    assert (bull.bottom, bull.top, bull.index, bull.created_index) == (11, 12, 1, 2)
    assert (bear.bottom, bear.top) == (10, 15)


def test_fvg_min_size_filter():
    df = candles([(10, 11, 9, 10.5), (10.5, 15, 10.5, 14.5), (14.5, 16, 11.05, 15.5)])
    assert find_fvgs(df, const_atr(df, 1.0), 0.1) == []        # gap 0.05 < 0.1 x ATR
    assert len(find_fvgs(df, const_atr(df, 0.1), 0.1)) == 1    # gap 0.05 >= 0.01


def test_zone_states_fresh_mitigated_invalidated():
    df = candles([(10, 11, 9, 10.5), (10.5, 15, 10.5, 14.5), (14.5, 16, 12, 15.5),
                  (15.5, 16, 13, 14), (14, 14.5, 11.5, 12), (12, 12, 10, 10.5)])
    z = find_fvgs(df.iloc[:3], const_atr(df.iloc[:3]), 0.1)[0]
    update_states([z], df.iloc[:4])
    assert z.state == "fresh"
    update_states([z], df.iloc[:5])
    assert z.state == "mitigated" and z.mitigated_index == 4
    update_states([z], df)
    assert z.state == "invalidated" and z.invalidated_index == 5


# --- order blocks ---------------------------------------------------------------------------

def ob_frame():
    # 0-4 drift, 5 = last bearish candle (the OB), 6-7 = displacement up through the 105 high
    return candles([(100, 101, 99, 100.5), (100.5, 105, 100, 104), (104, 104.5, 101, 102),
                    (102, 103, 100, 101), (101, 102, 99.5, 100.5), (100.5, 101, 98, 98.5),
                    (98.5, 104, 98.5, 103.8), (103.8, 108, 103.5, 107.5)])


def bull_break(df, origin=5, at=7, kind="BOS"):
    return StructureBreak(kind, 1, 105.0, 1, df.index[1], at, df.index[at], origin, df.index[origin], "swing")


def test_order_block_is_last_opposite_candle_before_displacement():
    df = ob_frame()
    obs = find_order_blocks(df, const_atr(df, 2.0), [bull_break(df)], [], {"displacement_body_atr": 1.2}, "1H")
    assert len(obs) == 1
    ob = obs[0]
    assert (ob.index, ob.top, ob.bottom, ob.direction, ob.created_index) == (5, 101, 98, 1, 7)
    assert ob.source == "BOS bullish (swing)"


def test_order_block_requires_displacement_or_fvg():
    df = ob_frame()
    cfg = {"displacement_body_atr": 5.0, "ob_requires_displacement_or_fvg": True}
    assert find_order_blocks(df, const_atr(df, 2.0), [bull_break(df)], [], cfg) == []
    cfg["ob_requires_displacement_or_fvg"] = False
    assert len(find_order_blocks(df, const_atr(df, 2.0), [bull_break(df)], [], cfg)) == 1


def test_order_block_swing_beats_internal_on_same_candle():
    df = ob_frame()
    internal = StructureBreak("BOS", 1, 104.0, 1, df.index[1], 6, df.index[6], 5, df.index[5], "internal")
    obs = find_order_blocks(df, const_atr(df, 2.0), [internal, bull_break(df)], [], {"displacement_body_atr": 1.2})
    assert len(obs) == 1 and obs[0].source.endswith("(swing)")


# --- supply / demand ------------------------------------------------------------------------------

def test_demand_base_and_departure():
    df = candles([(100, 101, 99, 100)] * 15 + [(100, 100.4, 99.2, 100.2), (100.2, 100.6, 99.8, 100.1),
                                               (100.1, 106, 100, 105.5)])
    zones = find_supply_demand(df, const_atr(df, 2.0), {"displacement_body_atr": 1.2, "base_max_body_atr": 0.5,
                                                        "base_max_candles": 3})
    demand = [z for z in zones if z.kind == "demand"]
    assert demand, "expected a demand zone"
    z = demand[-1]
    assert z.created_index == 17 and z.top == pytest.approx(100.2) and z.bottom == 99


def test_analyze_zones_drops_invalidated_and_display_caps():
    zs = [Zone("fvg", 1, 10 + i, 9 + i, i, T0, i, T0) for i in range(15)]
    res = ZoneResult(zs, display_cap=10)
    assert len(res.display()) == 10 and res.display()[0].top == 15
    df = candles([(100, 101, 99, 100)] * 30)
    out = analyze_zones(df, const_atr(df), [], {"max_zones_per_timeframe": 10})
    assert all(z.active for z in out.zones)


def test_zone_helpers():
    a = Zone("order_block", 1, 10, 8, 0, T0, 0, T0, "4H")
    b = Zone("fvg", 1, 9, 7, 0, T0, 0, T0, "4H")
    assert a.overlaps(b) and a.overlap_amount(b) == 1 and a.mid == 9
    assert (a.proximal, a.distal) == (10, 8)
    s = Zone("supply", -1, 20, 18, 0, T0, 0, T0, "1D")
    assert (s.proximal, s.distal) == (18, 20) and s.label() == "1D supply zone"


# --- dealing range ----------------------------------------------------------------------------------

def test_dealing_range_bullish_leg_and_ote():
    df = candles([(100, 101, 99, 100)] * 3 + [(100, 200, 100, 199)] * 3 + [(150, 160, 140, 150)])
    piv = [sw("low", 1, 100.0, df, level="swing"), sw("high", 4, 200.0, df, level="swing")]
    dr = dealing_range(df.iloc[:6].assign(high=[101, 101, 101, 150, 200, 190], low=[99, 100, 99, 100, 150, 150]),
                       piv)
    assert (dr.low, dr.high, dr.direction, dr.eq) == (100, 200, 1, 150)
    assert dr.ote_top == pytest.approx(138) and dr.ote_bottom == pytest.approx(121)
    assert dr.zone_of(120) == "discount" and dr.zone_of(180) == "premium" and dr.zone_of(152) == "equilibrium"
    assert dr.in_ote(130) and not dr.in_ote(110)
    assert dr.favourable(120, 1) and not dr.favourable(120, -1)


def test_dealing_range_extends_and_flips():
    base = candles([(100, 101, 99, 100), (100, 101, 90, 95), (95, 120, 95, 118), (118, 150, 117, 149),
                    (149, 150, 130, 131)])
    piv = [sw("low", 1, 90.0, base, level="swing"), sw("high", 3, 150.0, base, level="swing")]
    ext = pd.concat([base, candles([(131, 160, 130, 158)], start=base.index[-1] + pd.Timedelta(hours=1))])
    assert dealing_range(ext, piv).high == 160
    brk = pd.concat([base, candles([(131, 132, 80, 85)], start=base.index[-1] + pd.Timedelta(hours=1))])
    dr = dealing_range(brk, piv)
    assert dr.direction == -1 and dr.low == 80 and dr.high == 150
    assert dr.ote_bottom == pytest.approx(80 + 0.62 * 70) and dr.ote_top == pytest.approx(80 + 0.79 * 70)


def test_dealing_range_needs_two_pivots():
    df = flat()
    assert dealing_range(df, [sw("low", 1, 99.0, df)]) is None


# --- inducement ---------------------------------------------------------------------------------------

def test_inducement_found_and_swept():
    df = candles([(110, 111, 109, 110)] * 4 + [(110, 111, 106, 107), (107, 112, 107, 111), (111, 112, 110, 111),
                                               (111, 111, 105, 106)])
    poi = Zone("order_block", 1, 104, 100, 0, df.index[0], 1, df.index[1], "4H")
    internal = [sw("low", 4, 106.0, df, confirm=2)]
    ind = find_inducement(df, internal, poi, 1)
    assert ind.price == 106 and ind.swept and ind.swept_index == 7


def test_inducement_absent_or_unswept():
    df = flat(10, 110)
    poi = Zone("order_block", 1, 104, 100, 0, df.index[0], 1, df.index[1])
    assert find_inducement(df, [], poi, 1) is None
    ind = find_inducement(df, [sw("low", 3, 108.0, df)], poi, 1)
    assert ind is not None and not ind.swept


# --- sessions ---------------------------------------------------------------------------------------------

CFG = {"timezone": "America/New_York",
       "killzones": {"asia": ["20:00", "00:00"], "london": ["02:00", "05:00"], "new_york": ["07:00", "10:00"]}}


@pytest.mark.parametrize("utc,expected", [
    ("2026-11-16 08:00", "london"),      # 03:00 EST
    ("2026-11-16 13:00", "new_york"),    # 08:00 EST
    ("2026-11-16 02:00", "asia"),        # 21:00 EST (previous day)
    ("2026-11-16 17:00", None),          # 12:00 EST
    ("2026-07-15 07:00", "london"),      # 03:00 EDT (daylight saving)
    ("2026-07-15 07:00", "london"),
])
def test_killzone_at(utc, expected):
    assert sessions.killzone_at(pd.Timestamp(utc, tz="UTC"), CFG) == expected


def test_session_ranges_asia():
    idx = pd.date_range("2026-11-16 00:00", periods=8, freq="1h", tz="UTC")  # 19:00..02:00 EST
    df = pd.DataFrame({"open": 1.0, "high": [1, 2, 3, 4, 5, 6, 7, 1.5], "low": [1, 0.5, 0.9, 0.8, 1, 1, 1, 1],
                       "close": 1.0, "volume": 1.0}, index=idx)
    df = pd.concat([df, pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
                                     index=[idx[-1] + pd.Timedelta(hours=1)])])
    rng = sessions.session_ranges(df, CFG)
    assert rng["asia"]["high"] == 5 and rng["asia"]["low"] == 0.5   # 01:00-05:00 UTC = 20:00-00:00 EST


def test_killzone_relevance():
    assert sessions.killzones_relevant("forex") and not sessions.killzones_relevant("stock")
