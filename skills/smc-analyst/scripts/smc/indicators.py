"""Small, dependency-light indicator helpers used across the engine.

All functions take/return pandas objects aligned to the input index and never
look ahead: the value at bar *i* only uses bars ``<= i``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# --- volatility ----------------------------------------------------------------

def true_range(df: pd.DataFrame) -> pd.Series:
    """True range: max(high-low, |high-prev_close|, |low-prev_close|).

    The first bar has no previous close, so its TR is simply high - low.
    """
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    )
    return ranges.max(axis=1, skipna=True).rename("tr")


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range using Wilder's smoothing (RMA), the same method TradingView uses.

    The first ``period - 1`` values are NaN. The value at index ``period - 1`` is the
    simple mean of the first ``period`` true ranges, and each later value is
    ``(prev * (period - 1) + tr) / period``.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    tr = true_range(df).to_numpy(dtype=float)
    out = np.full(len(tr), np.nan)
    if len(tr) >= period:
        out[period - 1] = tr[:period].mean()
        for i in range(period, len(tr)):
            out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return pd.Series(out, index=df.index, name=f"atr{period}")


# --- candle anatomy ------------------------------------------------------------

def body_size(df: pd.DataFrame) -> pd.Series:
    return (df["close"] - df["open"]).abs().rename("body")


def candle_direction(df: pd.DataFrame) -> pd.Series:
    """+1 bullish, -1 bearish, 0 doji (close == open)."""
    return np.sign(df["close"] - df["open"]).astype(int).rename("direction")


def is_displacement(df: pd.DataFrame, atr_series: pd.Series, mult: float = 1.2) -> pd.Series:
    """True where the candle body is at least ``mult`` × ATR (an impulsive, institutional candle).

    It uses the *previous* bar's ATR so that a huge candle doesn't inflate its own threshold.
    Bars without a defined ATR return False.
    """
    threshold = atr_series.shift(1) * mult
    return (body_size(df) >= threshold).fillna(False).astype(bool).rename("displacement")


# --- returns / correlation / beta (for the hedge module) ----------------------------

def returns(close: pd.Series, log: bool = False) -> pd.Series:
    """Simple (default) or log returns. The first value is dropped."""
    r = np.log(close / close.shift(1)) if log else close.pct_change()
    return r.dropna().rename("returns")


def to_daily(series: pd.Series) -> pd.Series:
    """Collapse a timestamp-indexed series to one value per calendar date (the last one).

    This lets 24/7 crypto line up with exchange-hours instruments by date.
    """
    idx = pd.DatetimeIndex(series.index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    s = pd.Series(series.to_numpy(), index=idx.normalize())
    return s.groupby(level=0).last()


def align_returns(close_a: pd.Series, close_b: pd.Series) -> pd.DataFrame:
    """Daily returns of two close series on their common dates (inner join)."""
    a, b = to_daily(close_a), to_daily(close_b)
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    joined.columns = ["a", "b"]
    return joined.pct_change().dropna()


def rolling_correlation(close_a: pd.Series, close_b: pd.Series, window: int = 30) -> pd.Series:
    r = align_returns(close_a, close_b)
    return r["a"].rolling(window).corr(r["b"]).rename("corr")


def rolling_beta(close_a: pd.Series, close_b: pd.Series, window: int = 30) -> pd.Series:
    """Beta of A relative to B: cov(a, b) / var(b) over a rolling window."""
    r = align_returns(close_a, close_b)
    cov = r["a"].rolling(window).cov(r["b"])
    var = r["b"].rolling(window).var()
    return (cov / var).rename("beta")


def correlation_and_beta(close_a: pd.Series, close_b: pd.Series, window: int = 30) -> dict:
    """Latest correlation and beta of A vs B over the last ``window`` common daily returns.

    Returns ``{"correlation": float|None, "beta": float|None, "observations": int}``.
    The values are None when fewer than ``window`` aligned returns exist.
    """
    r = align_returns(close_a, close_b).tail(window)
    n = len(r)
    if n < window or r["b"].var() == 0:
        return {"correlation": None, "beta": None, "observations": n}
    corr = float(r["a"].corr(r["b"]))
    beta = float(r["a"].cov(r["b"]) / r["b"].var())
    return {"correlation": corr, "beta": beta, "observations": n}
