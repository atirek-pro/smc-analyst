"""Market structure: Break of Structure (BOS) and Change of Character (CHoCH).

The engine walks the closed bars in order and keeps the most recent *confirmed* swing
high and swing low that have not been broken yet.

* **Bullish break**: a bar *closes* above the tracked swing high (``break_confirmation``
  ``"body_close"``, the default). With ``"wick"`` the high is enough.
* **Bearish break**: a bar closes below the tracked swing low.
* A break **in** the direction of the current trend, or the first break when the trend
  is still unknown, is a **BOS**. A break **against** the current trend is a **CHoCH**,
  and it flips the trend.
* Each swing level can be broken only once. The next break needs a newly confirmed pivot.

Every break also records its ``origin_index``: the extreme candle between the broken
pivot and the break bar. That is where the impulsive leg started, and order blocks are
searched from there.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from .common import BEAR, BULL, NEUTRAL, dir_name
from .swings import Swing, extreme_between


@dataclass
class StructureBreak:
    kind: str                 # "BOS" | "CHoCH"
    direction: int            # +1 bullish, -1 bearish
    level: float              # price of the broken swing
    swing_index: int
    swing_time: pd.Timestamp
    break_index: int
    break_time: pd.Timestamp
    origin_index: int         # start of the impulsive leg (lowest low / highest high)
    origin_time: pd.Timestamp
    structure: str = "swing"  # "swing" | "internal"

    _json_extra = ("direction_name",)

    @property
    def direction_name(self) -> str:
        return dir_name(self.direction)

    def describe(self) -> str:
        return (f"{self.direction_name} {self.kind} ({self.structure}) through "
                f"{self.level:g} on {self.break_time:%Y-%m-%d %H:%M}")


@dataclass
class StructureResult:
    structure: str
    trend: int = NEUTRAL
    breaks: list[StructureBreak] = field(default_factory=list)
    active_high: Swing | None = None   # latest unbroken swing high (next bullish break level)
    active_low: Swing | None = None    # latest unbroken swing low (next bearish break level)

    _json_extra = ("trend_name",)

    @property
    def trend_name(self) -> str:
        return dir_name(self.trend)

    def last_break(self, kind: str | None = None, direction: int | None = None) -> StructureBreak | None:
        for b in reversed(self.breaks):
            if (kind is None or b.kind == kind) and (direction is None or b.direction == direction):
                return b
        return None

    def breaks_after(self, index: int, direction: int | None = None) -> list[StructureBreak]:
        return [b for b in self.breaks if b.break_index > index
                and (direction is None or b.direction == direction)]


def analyze_structure(df: pd.DataFrame, swings: list[Swing], structure: str = "swing",
                      confirmation: str = "body_close") -> StructureResult:
    """Detect BOS / CHoCH events on ``df`` given its (confirmed) ``swings``."""
    if confirmation not in ("body_close", "wick"):
        raise ValueError("confirmation must be 'body_close' or 'wick'")
    res = StructureResult(structure=structure)
    if df.empty:
        return res
    closes = df["close"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    idx = df.index
    by_confirm: dict[int, list[Swing]] = defaultdict(list)
    for s in swings:
        by_confirm[s.confirmed_index].append(s)

    hi: Swing | None = None
    lo: Swing | None = None
    trend = NEUTRAL
    for j in range(len(df)):
        for s in by_confirm.get(j, ()):
            if s.is_high:
                hi = s
            else:
                lo = s

        up_price = closes[j] if confirmation == "body_close" else highs[j]
        if hi is not None and j > hi.index and up_price > hi.price:
            kind = "CHoCH" if trend == BEAR else "BOS"
            origin = extreme_between(df, hi.index, j, "low")
            res.breaks.append(StructureBreak(kind, BULL, hi.price, hi.index, hi.time, j, idx[j],
                                             origin, idx[origin], structure))
            trend, hi = BULL, None

        down_price = closes[j] if confirmation == "body_close" else lows[j]
        if lo is not None and j > lo.index and down_price < lo.price:
            kind = "CHoCH" if trend == BULL else "BOS"
            origin = extreme_between(df, lo.index, j, "high")
            res.breaks.append(StructureBreak(kind, BEAR, lo.price, lo.index, lo.time, j, idx[j],
                                             origin, idx[origin], structure))
            trend, lo = BEAR, None

    res.trend = trend
    res.active_high = hi
    res.active_low = lo
    return res
