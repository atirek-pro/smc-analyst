"""Phase 3: swings and market structure (BOS / CHoCH)."""

from __future__ import annotations

import pytest
from smc_builders import candles, zigzag

from smc.structure import analyze_structure
from smc.swings import alternate, extreme_between, find_swings, swings_known_at

UP = [100, 120, 105, 130, 115, 140]


def test_swings_on_zigzag_with_labels_and_confirmation():
    df = zigzag(UP, bars_per_leg=5)
    sw = find_swings(df, 2)
    assert [(s.kind, s.price) for s in sw] == [("high", 120), ("low", 105), ("high", 130), ("low", 115)]
    assert [s.label for s in sw] == [None, None, "HH", "HL"]
    for s in sw:
        assert s.confirmed_index == s.index + 2
        assert s.confirmed_time == df.index[s.index + 2]


def test_no_pivot_inside_the_last_right_bars():
    df = zigzag([100, 120, 105, 130, 115, 140, 139], bars_per_leg=5)
    assert all(s.index < len(df) - 2 for s in find_swings(df, 2))


def test_equal_highs_first_bar_is_the_pivot():
    df = candles([(1, 2, 0.5, 1), (1, 3, 1, 2), (2, 5, 2, 3), (3, 5, 2, 3), (3, 4, 2, 2), (2, 3, 1, 1),
                  (1, 2, 0.5, 1)])
    highs = [s for s in find_swings(df, 2) if s.kind == "high"]
    assert [s.index for s in highs] == [2]


def test_lookback_validation():
    with pytest.raises(ValueError):
        find_swings(zigzag(UP), 0)


def test_alternate_keeps_most_extreme():
    df = candles([(5, 6, 4, 5), (5, 9, 5, 8), (8, 8, 6, 7), (7, 10, 7, 9), (9, 9, 3, 4), (4, 5, 2, 3),
                  (3, 4, 1, 2), (2, 7, 2, 6), (6, 6, 5, 5)])
    from smc.swings import Swing
    t = df.index
    sw = [Swing("high", 1, t[1], 9, 2, t[2]), Swing("high", 3, t[3], 10, 4, t[4]),
          Swing("low", 6, t[6], 1, 7, t[7])]
    assert [(s.kind, s.price) for s in alternate(sw)] == [("high", 10), ("low", 1)]


def test_swings_known_at_respects_confirmation():
    df = zigzag(UP, bars_per_leg=5)
    sw = find_swings(df, 2)
    first = sw[0]
    assert first not in swings_known_at(sw, first.confirmed_index - 1)
    assert first in swings_known_at(sw, first.confirmed_index)


def test_extreme_between():
    df = zigzag(UP, bars_per_leg=5)
    assert df["low"].iloc[extreme_between(df, 5, 12, "low")] == 105


# --- structure ---------------------------------------------------------------------

def test_uptrend_produces_bullish_bos_sequence():
    df = zigzag(UP, bars_per_leg=5)
    st = analyze_structure(df, find_swings(df, 2))
    assert [(b.kind, b.direction, b.level) for b in st.breaks] == [("BOS", 1, 120), ("BOS", 1, 130)]
    assert st.trend == 1
    first = st.breaks[0]
    assert df["close"].iloc[first.break_index] > 120 >= df["close"].iloc[first.break_index - 1]
    assert df["low"].iloc[first.origin_index] == 105  # the impulsive leg started at the HL


def test_break_of_last_higher_low_is_bearish_choch():
    df = zigzag(UP + [110], bars_per_leg=5)
    st = analyze_structure(df, find_swings(df, 2))
    last = st.breaks[-1]
    assert (last.kind, last.direction, last.level) == ("CHoCH", -1, 115)
    assert st.trend == -1
    assert df["high"].iloc[last.origin_index] == 140


def test_each_level_breaks_only_once():
    df = zigzag(UP, bars_per_leg=5)
    st = analyze_structure(df, find_swings(df, 2))
    assert len({b.swing_index for b in st.breaks}) == len(st.breaks)


def wick_only_frame():
    # swing high 10 at bar 2; bar 6 wicks to 12 but closes at 9 (below 10)
    return candles([(5, 6, 4, 5), (5, 8, 5, 7), (7, 10, 7, 9), (9, 9, 6, 7), (7, 8, 5, 6),
                    (6, 7, 5, 6), (6, 12, 6, 9), (9, 9, 7, 8)])


def test_wick_through_is_not_a_bos_with_body_close():
    df = wick_only_frame()
    st = analyze_structure(df, find_swings(df, 2), confirmation="body_close")
    assert not [b for b in st.breaks if b.direction == 1]


def test_wick_mode_counts_the_wick():
    df = wick_only_frame()
    st = analyze_structure(df, find_swings(df, 2), confirmation="wick")
    assert [b.break_index for b in st.breaks if b.direction == 1] == [6]


def test_invalid_confirmation_mode():
    with pytest.raises(ValueError):
        analyze_structure(wick_only_frame(), [], confirmation="nope")


def test_active_levels_and_helpers():
    df = zigzag(UP, bars_per_leg=5)
    st = analyze_structure(df, find_swings(df, 2))
    assert st.active_high is None          # 130 was broken, 140 not yet confirmed
    assert st.active_low.price == 115
    assert st.last_break().level == 130
    assert st.last_break(kind="CHoCH") is None
    assert len(st.breaks_after(st.breaks[0].break_index, 1)) == 1
