"""Swing points (fractal pivots).

A bar *i* is a **swing high** when its high is strictly above the ``left`` highs
before it and at least as high as the ``right`` highs after it. A **swing low**
mirrors this with lows. When several bars share the extreme, the first one is the
pivot. A pivot is only *known* once its right-hand bars have closed: its
``confirmed_index`` is ``i + right``. Later modules must never use a pivot before
that bar, which keeps the engine free of look-ahead.

Two levels are produced for each timeframe:
* ``swing`` (external) structure: lookback from ``config.swings.lookback[tf]``
* ``internal`` structure: the shorter ``config.swings.internal_lookback``
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Swing:
    kind: str                 # "high" | "low"
    index: int                # bar position of the pivot
    time: pd.Timestamp
    price: float
    confirmed_index: int      # first bar at whose close the pivot is known
    confirmed_time: pd.Timestamp
    level: str = "swing"      # "swing" (external) | "internal"
    label: str | None = None  # HH / LH for highs, HL / LL for lows

    @property
    def is_high(self) -> bool:
        return self.kind == "high"


def find_swings(df: pd.DataFrame, left: int, right: int | None = None,
                level: str = "swing") -> list[Swing]:
    """Return confirmed swing highs and lows ordered by bar index."""
    right = left if right is None else right
    if left < 1 or right < 1:
        raise ValueError("left/right lookback must be >= 1")
    n = len(df)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    idx = df.index
    out: list[Swing] = []
    for i in range(left, n - right):
        h = highs[i]
        if h > highs[i - left:i].max() and h >= highs[i + 1:i + right + 1].max():
            out.append(Swing("high", i, idx[i], float(h), i + right, idx[i + right], level))
        lo = lows[i]
        if lo < lows[i - left:i].min() and lo <= lows[i + 1:i + right + 1].min():
            out.append(Swing("low", i, idx[i], float(lo), i + right, idx[i + right], level))
    out.sort(key=lambda s: (s.index, 0 if s.is_high else 1))
    label_swings(out)
    return out


def label_swings(swings: list[Swing]) -> None:
    """Label each pivot relative to the previous pivot of the same kind (in place)."""
    prev: dict[str, float] = {}
    for s in swings:
        p = prev.get(s.kind)
        if p is not None:
            if s.is_high:
                s.label = "HH" if s.price > p else "LH"
            else:
                s.label = "HL" if s.price > p else "LL"
        prev[s.kind] = s.price


def alternate(swings: list[Swing]) -> list[Swing]:
    """Collapse consecutive same-kind pivots, keeping the most extreme one.

    The result alternates high / low / high …, which is what a dealing range or a
    zig-zag leg needs.
    """
    out: list[Swing] = []
    for s in sorted(swings, key=lambda x: x.index):
        if out and out[-1].kind == s.kind:
            better = s.price > out[-1].price if s.is_high else s.price < out[-1].price
            if better:
                out[-1] = s
        else:
            out.append(s)
    return out


def swings_known_at(swings: list[Swing], bar: int) -> list[Swing]:
    """Pivots that are confirmed at or before ``bar`` (no look-ahead)."""
    return [s for s in swings if s.confirmed_index <= bar]


def last_swing(swings: list[Swing], kind: str) -> Swing | None:
    for s in reversed(swings):
        if s.kind == kind:
            return s
    return None


def extreme_between(df: pd.DataFrame, start: int, end: int, kind: str) -> int:
    """Index of the lowest low (``kind="low"``) or highest high between start..end inclusive."""
    start, end = max(0, start), min(len(df) - 1, end)
    if kind == "low":
        return start + int(np.argmin(df["low"].to_numpy()[start:end + 1]))
    return start + int(np.argmax(df["high"].to_numpy()[start:end + 1]))
