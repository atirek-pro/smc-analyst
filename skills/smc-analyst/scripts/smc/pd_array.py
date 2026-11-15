"""Premium / discount arrays: the dealing range of the latest swing leg.

The **dealing range** comes from the last two alternating external pivots. If the
later pivot is a high, the leg runs low → high (bullish leg).
* If price later trades above that high, the range high extends to the new extreme
  (the leg keeps going).
* If price later trades below the leg's low, the range has broken. It becomes a
  bearish leg from that high to the new low.

The bearish case is the mirror image.

* **EQ** (equilibrium) = low + ``equilibrium`` × range (default 50 %).
* Above EQ is **premium** (expensive: sell zone), below EQ is **discount** (cheap: buy zone).
  Within ± ``equilibrium_band`` × range of EQ, price is in **equilibrium**.
* **OTE** (optimal trade entry) is the 62–79 % retracement of the leg: measured down
  from the high for a bullish leg, up from the low for a bearish leg.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .common import BEAR, BULL, dir_name
from .swings import Swing, alternate


@dataclass
class DealingRange:
    high: float
    low: float
    high_time: pd.Timestamp
    low_time: pd.Timestamp
    direction: int                  # +1 leg up (low→high), -1 leg down (high→low)
    eq: float
    ote_top: float
    ote_bottom: float
    eq_band: float                  # half-width of the equilibrium band in price

    _json_extra = ("direction_name",)

    @property
    def direction_name(self) -> str:
        return dir_name(self.direction)

    @property
    def size(self) -> float:
        return self.high - self.low

    def position(self, price: float) -> float:
        """0 at the range low, 1 at the range high (can be outside 0..1)."""
        return 0.5 if self.size == 0 else (price - self.low) / self.size

    def zone_of(self, price: float) -> str:
        if abs(price - self.eq) <= self.eq_band:
            return "equilibrium"
        return "premium" if price > self.eq else "discount"

    def in_ote(self, price: float) -> bool:
        return self.ote_bottom <= price <= self.ote_top

    def favourable(self, price: float, bias: int) -> bool:
        """Discount for longs, premium for shorts (equilibrium counts as neither)."""
        z = self.zone_of(price)
        return z == ("discount" if bias == BULL else "premium")


def dealing_range(df: pd.DataFrame, swings: list[Swing], equilibrium: float = 0.5,
                  ote: tuple[float, float] = (0.62, 0.79), eq_band: float = 0.05) -> DealingRange | None:
    alt = alternate(swings)
    if len(alt) < 2 or df.empty:
        return None
    a, b = alt[-2], alt[-1]
    after = df.iloc[b.index + 1:]
    if b.is_high:
        hi, hi_t, lo, lo_t = b.price, b.time, a.price, a.time
        if not after.empty and after["high"].max() > hi:
            hi, hi_t = float(after["high"].max()), after["high"].idxmax()
        direction = BULL
        if not after.empty and after["low"].min() < lo:
            lo, lo_t, direction = float(after["low"].min()), after["low"].idxmin(), BEAR
    else:
        lo, lo_t, hi, hi_t = b.price, b.time, a.price, a.time
        if not after.empty and after["low"].min() < lo:
            lo, lo_t = float(after["low"].min()), after["low"].idxmin()
        direction = BEAR
        if not after.empty and after["high"].max() > hi:
            hi, hi_t, direction = float(after["high"].max()), after["high"].idxmax(), BULL
    rng = hi - lo
    eq = lo + equilibrium * rng
    lo_r, hi_r = sorted(ote)
    if direction == BULL:
        ote_top, ote_bottom = hi - lo_r * rng, hi - hi_r * rng
    else:
        ote_bottom, ote_top = lo + lo_r * rng, lo + hi_r * rng
    return DealingRange(hi, lo, hi_t, lo_t, direction, eq, ote_top, ote_bottom, eq_band * rng)
