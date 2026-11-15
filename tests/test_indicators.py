"""Phase 2: indicator helpers."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from smc import indicators as ind


def frame(rows, start="2026-01-01", freq="1h"):
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def test_true_range_uses_previous_close():
    df = frame([[10, 11, 9, 10], [10, 15, 12, 14], [14, 14, 8, 9]])
    tr = ind.true_range(df).tolist()
    assert tr == [2, 5, 6]  # bar2: |15-10|=5 > 3; bar3: |8-14|=6


def test_atr_wilder_seed_and_recursion():
    df = frame([[10, 11, 9, 10], [10, 15, 12, 14], [14, 14, 8, 9], [9, 10, 8, 9]])
    atr = ind.atr(df, period=3)
    assert math.isnan(atr.iloc[0]) and math.isnan(atr.iloc[1])
    assert atr.iloc[2] == pytest.approx((2 + 5 + 6) / 3)          # SMA seed
    assert atr.iloc[3] == pytest.approx((13 / 3 * 2 + 2) / 3)      # Wilder step, TR=2


def test_atr_constant_range():
    df = frame([[10, 12, 10, 11]] * 30)
    assert ind.atr(df, 14).iloc[-1] == pytest.approx(2.0)


def test_atr_short_series_all_nan():
    assert ind.atr(frame([[1, 2, 0, 1]] * 3), 14).isna().all()


def test_displacement_uses_previous_atr():
    rows = [[10, 11, 9, 10.5]] * 20 + [[10, 16, 10, 15.5]]  # big bullish candle last
    df = frame(rows)
    atr = ind.atr(df, 14)
    disp = ind.is_displacement(df, atr, mult=1.2)
    assert disp.iloc[-1] and not disp.iloc[:-1].any()


def test_candle_direction():
    df = frame([[1, 2, 0, 2], [2, 2, 0, 1], [1, 1, 1, 1]])
    assert ind.candle_direction(df).tolist() == [1, -1, 0]


def test_correlation_and_beta_perfect_relationship():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2026-01-01", periods=60, freq="1D", tz="UTC")
    b = pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, 60)), index=idx)
    rb = b.pct_change()
    a = pd.Series(50 * np.cumprod(1 + (2 * rb).fillna(0)), index=idx)
    res = ind.correlation_and_beta(a, b, window=30)
    assert res["correlation"] == pytest.approx(1.0, abs=1e-9)
    assert res["beta"] == pytest.approx(2.0, abs=1e-9)
    assert res["observations"] == 30


def test_correlation_insufficient_data_returns_none():
    idx = pd.date_range("2026-01-01", periods=10, freq="1D", tz="UTC")
    s = pd.Series(range(1, 11), index=idx, dtype=float)
    assert ind.correlation_and_beta(s, s, window=30)["correlation"] is None


def test_align_returns_crypto_vs_weekday_market():
    crypto_idx = pd.date_range("2026-03-02", periods=14, freq="1D", tz="UTC")       # 7 days/week
    stock_idx = pd.bdate_range("2026-03-02", periods=10, tz="America/New_York")      # weekdays
    a = pd.Series(np.arange(14, dtype=float) + 100, index=crypto_idx)
    b = pd.Series(np.arange(10, dtype=float) + 50, index=stock_idx)
    r = ind.align_returns(a, b)
    assert len(r) == 9  # 10 common weekdays -> 9 returns
    assert r.index.day_of_week.max() <= 4
