"""Deterministic synthetic market builders for the Phase 3 tests."""

from __future__ import annotations

import numpy as np
import pandas as pd

T0 = pd.Timestamp("2026-01-05 00:00", tz="UTC")


def candles(rows, start=T0, freq="1h") -> pd.DataFrame:
    """rows: iterable of (open, high, low, close[, volume])."""
    rows = [tuple(r) + (1.0,) * (5 - len(r)) for r in rows]
    idx = pd.date_range(start, periods=len(rows), freq=freq, name="time")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx).astype(float)


def zigzag(points, bars_per_leg=5, start=T0, freq="1h", wick=0.0, legs=None) -> pd.DataFrame:
    """Candles whose closes walk linearly between ``points``.

    ``legs`` optionally gives the bar count per leg (overrides ``bars_per_leg``), which
    lets a test make impulsive (few bars) and corrective (many bars) legs.
    """
    closes = [float(points[0])]
    for k in range(1, len(points)):
        n = legs[k - 1] if legs else bars_per_leg
        closes += list(np.linspace(points[k - 1], points[k], n + 1)[1:])
    rows = []
    prev = closes[0]
    for c in closes:
        o = prev
        rows.append((o, max(o, c) + wick, min(o, c) - wick, c))
        prev = c
    return candles(rows, start, freq)
