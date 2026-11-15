"""Liquidity: where resting stop orders sit, and when price takes them.

* **Buy-side liquidity (BSL)** rests above swing highs, equal highs, PDH and PWH.
* **Sell-side liquidity (SSL)** rests below swing lows, equal lows, PDL and PWL.
* **Equal highs/lows (EQH/EQL)**: consecutive internal pivots of the same kind whose
  prices differ by no more than ``equal_level_tolerance_atr`` × ATR. They are stronger
  pools than a single pivot.

Each level becomes active once its pivot is confirmed (or, for period levels, once that
day or week has closed). The first bar that trades through it decides its status:
* **swept**: the wick went through, but the bar closed back on the original side (a stop hunt)
* **taken**: the bar closed beyond the level (liquidity was run and price accepted there)

If ``sweep_requires_close_back_inside`` is false, any trade through counts as a sweep.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .common import BEAR, BULL
from .swings import Swing

BUY, SELL = "buy", "sell"


@dataclass
class LiquidityLevel:
    price: float
    side: str                      # "buy" (BSL, above highs) | "sell" (SSL, below lows)
    kind: str                      # swing_high | swing_low | equal_highs | equal_lows | PDH | PDL | PWH | PWL
    time: pd.Timestamp             # when the level formed (last contributing pivot / period open)
    active_from: pd.Timestamp      # first moment the level is known
    index: int | None = None       # bar index of the (last) pivot on this timeframe
    touches: int = 1
    status: str = "resting"        # resting | swept | taken
    status_index: int | None = None
    status_time: pd.Timestamp | None = None

    @property
    def resting(self) -> bool:
        return self.status == "resting"

    def label(self) -> str:
        names = {"swing_high": "swing high", "swing_low": "swing low", "equal_highs": "equal highs",
                 "equal_lows": "equal lows"}
        return names.get(self.kind, self.kind)


@dataclass
class LiquidityResult:
    levels: list[LiquidityLevel] = field(default_factory=list)

    def resting(self, side: str | None = None) -> list[LiquidityLevel]:
        return [lv for lv in self.levels if lv.resting and (side is None or lv.side == side)]

    def sweeps(self) -> list[LiquidityLevel]:
        return [lv for lv in self.levels if lv.status == "swept"]

    def nearest_resting(self, price: float, direction: int) -> LiquidityLevel | None:
        """Closest resting pool in ``direction`` from ``price`` (+1 above, -1 below)."""
        if direction == BULL:
            cands = [lv for lv in self.resting(BUY) if lv.price > price]
            return min(cands, key=lambda lv: lv.price, default=None)
        cands = [lv for lv in self.resting(SELL) if lv.price < price]
        return max(cands, key=lambda lv: lv.price, default=None)

    def resting_beyond(self, price: float, direction: int) -> list[LiquidityLevel]:
        """All resting pools beyond price in ``direction``, nearest first."""
        if direction == BULL:
            return sorted([lv for lv in self.resting(BUY) if lv.price > price], key=lambda lv: lv.price)
        return sorted([lv for lv in self.resting(SELL) if lv.price < price], key=lambda lv: -lv.price)


def swing_levels(swings: list[Swing]) -> list[LiquidityLevel]:
    return [LiquidityLevel(price=s.price, side=BUY if s.is_high else SELL,
                           kind="swing_high" if s.is_high else "swing_low", time=s.time,
                           active_from=s.confirmed_time, index=s.index)
            for s in swings]


def equal_levels(swings: list[Swing], atr: pd.Series, tolerance_atr: float) -> list[LiquidityLevel]:
    """Chains of consecutive same-kind pivots within ``tolerance_atr`` × ATR of each other."""
    atr_vals = atr.to_numpy(dtype=float)
    out: list[LiquidityLevel] = []
    for kind in ("high", "low"):
        piv = [s for s in swings if s.kind == kind]
        chain: list[Swing] = []
        for s in piv:
            tol = atr_vals[s.index] * tolerance_atr if s.index < len(atr_vals) else np.nan
            if chain and not np.isnan(tol) and abs(s.price - chain[-1].price) <= tol:
                chain.append(s)
                continue
            if len(chain) >= 2:
                out.append(_chain_level(chain))
            chain = [s]
        if len(chain) >= 2:
            out.append(_chain_level(chain))
    return out


def _chain_level(chain: list[Swing]) -> LiquidityLevel:
    high = chain[0].is_high
    price = max(s.price for s in chain) if high else min(s.price for s in chain)
    last = chain[-1]
    return LiquidityLevel(price=price, side=BUY if high else SELL,
                          kind="equal_highs" if high else "equal_lows", time=last.time,
                          active_from=last.confirmed_time, index=last.index, touches=len(chain))


def period_levels(daily: pd.DataFrame | None, weekly: pd.DataFrame | None) -> list[LiquidityLevel]:
    """PDH/PDL from the last *closed* daily bar and PWH/PWL from the last closed weekly bar.

    The frames must already contain closed bars only.
    """
    out: list[LiquidityLevel] = []
    for frame, hi_kind, lo_kind, span in ((daily, "PDH", "PDL", pd.Timedelta(days=1)),
                                          (weekly, "PWH", "PWL", pd.Timedelta(weeks=1))):
        if frame is None or frame.empty:
            continue
        t = frame.index[-1]
        bar = frame.iloc[-1]
        active = t + span
        out.append(LiquidityLevel(float(bar["high"]), BUY, hi_kind, t, active))
        out.append(LiquidityLevel(float(bar["low"]), SELL, lo_kind, t, active))
    return out


def evaluate_levels(levels: list[LiquidityLevel], df: pd.DataFrame,
                    require_close_back: bool = True) -> list[LiquidityLevel]:
    """Set each level's status from the first bar that trades through it (in place)."""
    if df.empty:
        return levels
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    for lv in levels:
        start = int(df.index.searchsorted(lv.active_from, side="left"))
        if lv.index is not None:  # never judge on the pivot's own confirmation bar
            start = max(start, lv.index + 1)
        lv.status, lv.status_index, lv.status_time = "resting", None, None
        if lv.side == BUY:
            hits = np.nonzero(highs[start:] > lv.price)[0]
            if hits.size:
                j = start + int(hits[0])
                back = closes[j] <= lv.price
                lv.status = "swept" if (back or not require_close_back) else "taken"
                lv.status_index, lv.status_time = j, df.index[j]
        else:
            hits = np.nonzero(lows[start:] < lv.price)[0]
            if hits.size:
                j = start + int(hits[0])
                back = closes[j] >= lv.price
                lv.status = "swept" if (back or not require_close_back) else "taken"
                lv.status_index, lv.status_time = j, df.index[j]
    return levels


def analyze_liquidity(df: pd.DataFrame, swings: list[Swing], internal_swings: list[Swing],
                      atr: pd.Series, tolerance_atr: float = 0.1, require_close_back: bool = True,
                      extra_levels: list[LiquidityLevel] | None = None) -> LiquidityResult:
    """External swing pools + equal highs/lows (from internal pivots) + period levels."""
    levels = swing_levels(swings) + equal_levels(internal_swings, atr, tolerance_atr)
    for lv in extra_levels or ():
        # copies, so the same PDH/PWH object is evaluated independently on each timeframe
        levels.append(LiquidityLevel(lv.price, lv.side, lv.kind, lv.time, lv.active_from))
    evaluate_levels(levels, df, require_close_back)
    return LiquidityResult(levels)


def side_for(direction: int) -> str:
    """Liquidity a move in ``direction`` targets: bullish moves draw on buy-side (above)."""
    return BUY if direction == BULL else SELL


def opposite_side(direction: int) -> str:
    return SELL if direction == BULL else BUY


__all__ = ["BUY", "SELL", "BEAR", "BULL", "LiquidityLevel", "LiquidityResult", "analyze_liquidity",
           "equal_levels", "evaluate_levels", "period_levels", "swing_levels", "side_for",
           "opposite_side"]
