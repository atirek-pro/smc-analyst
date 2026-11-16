"""Points of interest (POIs): order blocks, fair value gaps, supply and demand.

Order block (OB)
    For every BOS/CHoCH, look at the impulsive leg that caused it (from the break's
    ``origin_index`` to the break bar). The OB is the **last opposite-colour candle**
    at or just before the origin: the last bearish candle before a bullish leg, or the
    last bullish candle before a bearish leg. The search goes back at most
    ``ob_search_back_bars``; if none is found, the origin candle itself is used. The
    zone is that candle's full range (high to low). If
    ``ob_requires_displacement_or_fvg`` is set, the leg must contain a displacement
    candle (body ≥ k × ATR) or a same-direction FVG, otherwise no OB is recorded.

Fair value gap (FVG)
    A 3-candle imbalance. It is bullish when ``low[i] > high[i-2]``, giving the zone
    ``high[i-2]`` to ``low[i]``, and bearish when ``high[i] < low[i-2]``. Gaps smaller
    than ``fvg_min_size_atr`` × ATR are ignored.

Supply / demand (base and departure)
    One to ``base_max_candles`` small-bodied candles (body ≤ ``base_max_body_atr`` × ATR)
    followed by a displacement candle. Demand: proximal = highest base body,
    distal = lowest base wick. Supply is the mirror image.

Zone state (checked on closed bars after the zone is created)
    fresh → mitigated (price trades back into the zone) → invalidated (a bar *closes*
    beyond the distal edge). Invalidated zones are dropped from the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .common import BEAR, BULL, dir_name
from .structure import StructureBreak

KINDS = ("order_block", "fvg", "demand", "supply")


@dataclass
class Zone:
    kind: str                      # order_block | fvg | demand | supply
    direction: int                 # +1 bullish / demand, -1 bearish / supply
    top: float
    bottom: float
    index: int                     # anchor candle (OB candle, FVG middle candle, last base candle)
    time: pd.Timestamp
    created_index: int             # bar at whose close the zone is known
    created_time: pd.Timestamp
    timeframe: str = ""
    source: str = ""               # e.g. "BOS bullish (swing)"
    state: str = "fresh"           # fresh | mitigated | invalidated
    mitigated_index: int | None = None
    mitigated_time: pd.Timestamp | None = None
    invalidated_index: int | None = None
    invalidated_time: pd.Timestamp | None = None

    _json_extra = ("direction_name", "mid")

    @property
    def direction_name(self) -> str:
        return dir_name(self.direction)

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def active(self) -> bool:
        return self.state != "invalidated"

    @property
    def proximal(self) -> float:
        """Edge price meets first when returning to the zone."""
        return self.top if self.direction == BULL else self.bottom

    @property
    def distal(self) -> float:
        """Edge that invalidates the zone when closed beyond."""
        return self.bottom if self.direction == BULL else self.top

    def overlaps(self, other: Zone, tolerance: float = 0.0) -> bool:
        return self.bottom <= other.top + tolerance and other.bottom <= self.top + tolerance

    def overlap_amount(self, other: Zone) -> float:
        return max(0.0, min(self.top, other.top) - max(self.bottom, other.bottom))

    def label(self) -> str:
        names = {"order_block": "OB", "fvg": "FVG", "demand": "demand zone", "supply": "supply zone"}
        prefix = "" if self.kind in ("demand", "supply") else f"{self.direction_name} "
        return f"{self.timeframe} {prefix}{names[self.kind]}".strip()


@dataclass
class ZoneResult:
    zones: list[Zone] = field(default_factory=list)   # every active zone (used by the cascade)
    display_cap: int = 10

    def display(self) -> list[Zone]:
        """Most recent ``display_cap`` active zones per kind and direction (for charts/reports)."""
        kept: list[Zone] = []
        for kind in KINDS:
            for d in (BULL, BEAR):
                group = [z for z in self.zones if z.kind == kind and z.direction == d]
                kept.extend(group[-self.display_cap:])
        return sorted(kept, key=lambda z: z.created_index)

    def select(self, kind: str | None = None, direction: int | None = None,
               active_only: bool = True) -> list[Zone]:
        return [z for z in self.zones
                if (kind is None or z.kind == kind) and (direction is None or z.direction == direction)
                and (z.active or not active_only)]


# -----------------------------------------------------------------------------
# Detection
# -----------------------------------------------------------------------------

def find_fvgs(df: pd.DataFrame, atr: pd.Series, min_size_atr: float = 0.1, tf: str = "") -> list[Zone]:
    highs, lows = df["high"].to_numpy(float), df["low"].to_numpy(float)
    atr_v = atr.to_numpy(float)
    idx = df.index
    out = []
    for i in range(2, len(df)):
        a = atr_v[i - 1]
        min_size = 0.0 if np.isnan(a) else a * min_size_atr
        if lows[i] > highs[i - 2] and lows[i] - highs[i - 2] >= min_size:
            out.append(Zone("fvg", BULL, float(lows[i]), float(highs[i - 2]), i - 1, idx[i - 1],
                            i, idx[i], tf, "3-candle imbalance"))
        elif highs[i] < lows[i - 2] and lows[i - 2] - highs[i] >= min_size:
            out.append(Zone("fvg", BEAR, float(lows[i - 2]), float(highs[i]), i - 1, idx[i - 1],
                            i, idx[i], tf, "3-candle imbalance"))
    return out


def _has_displacement(df: pd.DataFrame, atr: pd.Series, start: int, end: int, direction: int,
                      mult: float) -> bool:
    o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
    prev_atr = atr.shift(1).to_numpy(float)
    for j in range(max(start, 1), end + 1):
        body = (c[j] - o[j]) * direction
        if not np.isnan(prev_atr[j]) and body >= mult * prev_atr[j]:
            return True
    return False


def find_order_blocks(df: pd.DataFrame, atr: pd.Series, breaks: list[StructureBreak],
                      fvgs: list[Zone], cfg_zones: dict, tf: str = "") -> list[Zone]:
    o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
    h, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    idx = df.index
    back = int(cfg_zones.get("ob_search_back_bars", 3))
    need_disp = bool(cfg_zones.get("ob_requires_displacement_or_fvg", True))
    mult = float(cfg_zones.get("displacement_body_atr", 1.2))
    seen: dict[tuple[int, int], Zone] = {}
    for b in breaks:
        d = b.direction
        k_found = None
        for k in range(b.origin_index, max(-1, b.origin_index - back - 1), -1):
            if (c[k] - o[k]) * d < 0:  # opposite colour to the break
                k_found = k
                break
        k = b.origin_index if k_found is None else k_found
        if need_disp:
            disp = _has_displacement(df, atr, k + 1, b.break_index, d, mult)
            gap = any(f.direction == d and k < f.created_index <= b.break_index for f in fvgs)
            if not (disp or gap):
                continue
        key = (k, d)
        if key in seen and seen[key].source.endswith("(swing)"):
            continue  # a swing-structure OB on the same candle wins over the internal one
        seen[key] = Zone("order_block", d, float(h[k]), float(lo[k]), k, idx[k], b.break_index,
                         b.break_time, tf, f"{b.kind} {b.direction_name} ({b.structure})")
    return list(seen.values())


def find_supply_demand(df: pd.DataFrame, atr: pd.Series, cfg_zones: dict, tf: str = "") -> list[Zone]:
    o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
    h, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    prev_atr = atr.shift(1).to_numpy(float)
    atr_v = atr.to_numpy(float)
    idx = df.index
    mult = float(cfg_zones.get("displacement_body_atr", 1.2))
    base_body = float(cfg_zones.get("base_max_body_atr", 0.5))
    max_base = int(cfg_zones.get("base_max_candles", 3))
    out = []
    for i in range(1, len(df)):
        if np.isnan(prev_atr[i]):
            continue
        body = c[i] - o[i]
        d = BULL if body > 0 else BEAR
        if abs(body) < mult * prev_atr[i]:
            continue
        base = []
        j = i - 1
        while j >= 0 and len(base) < max_base and not np.isnan(atr_v[j]) \
                and abs(c[j] - o[j]) <= base_body * atr_v[j]:
            base.append(j)
            j -= 1
        if not base:
            continue
        body_hi = max(max(o[k], c[k]) for k in base)
        body_lo = min(min(o[k], c[k]) for k in base)
        if d == BULL:
            top, bottom, kind = body_hi, min(lo[k] for k in base), "demand"
        else:
            top, bottom, kind = max(h[k] for k in base), body_lo, "supply"
        if top <= bottom:
            continue
        last = max(base)
        out.append(Zone(kind, d, float(top), float(bottom), last, idx[last], i, idx[i], tf,
                        f"{len(base)}-candle base + displacement"))
    return out


def update_states(zones: list[Zone], df: pd.DataFrame) -> list[Zone]:
    """Walk the bars after each zone's creation: fresh → mitigated → invalidated (in place)."""
    h, lo, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    for z in zones:
        z.state, z.mitigated_index, z.mitigated_time = "fresh", None, None
        z.invalidated_index = z.invalidated_time = None
        for j in range(z.created_index + 1, len(df)):
            if z.direction == BULL:
                touched, broken = lo[j] <= z.top, c[j] < z.bottom
            else:
                touched, broken = h[j] >= z.bottom, c[j] > z.top
            if touched and z.mitigated_index is None:
                z.state, z.mitigated_index, z.mitigated_time = "mitigated", j, df.index[j]
            if broken:
                z.state, z.invalidated_index, z.invalidated_time = "invalidated", j, df.index[j]
                break
    return zones


def analyze_zones(df: pd.DataFrame, atr: pd.Series, breaks: list[StructureBreak],
                  cfg_zones: dict, tf: str = "") -> ZoneResult:
    """Detect all POIs, update their states and drop invalidated ones.

    All active zones are kept for the cascade; ``ZoneResult.display()`` applies the
    ``max_zones_per_timeframe`` cap for charts and reports.
    """
    fvgs = find_fvgs(df, atr, float(cfg_zones.get("fvg_min_size_atr", 0.1)), tf)
    obs = find_order_blocks(df, atr, breaks, fvgs, cfg_zones, tf)
    sd = find_supply_demand(df, atr, cfg_zones, tf)
    # Degenerate zones (top == bottom, e.g. a 1m doji whose high equals its low) can't be traded.
    zones = [z for z in update_states(obs + fvgs + sd, df) if z.active and z.top > z.bottom]
    zones.sort(key=lambda z: z.created_index)
    return ZoneResult(zones, int(cfg_zones.get("max_zones_per_timeframe", 10)))
