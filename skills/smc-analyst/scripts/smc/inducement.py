"""Inducement: the bait liquidity that sits just in front of a POI.

For a bullish setup with a demand POI below price, the inducement is the **most recent
internal swing low above the POI** that formed after the POI was created. Early buyers
put their stops under it. Smart money usually runs those stops (price trades below the
inducement) before tapping the real POI. A bearish setup is the mirror image: the latest
internal swing high below a supply POI.

A setup counts as valid only once its inducement has been **swept**.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .common import BULL
from .swings import Swing
from .zones import Zone


@dataclass
class Inducement:
    price: float
    index: int
    time: pd.Timestamp
    direction: int                    # setup bias the inducement belongs to
    swept: bool = False
    swept_index: int | None = None
    swept_time: pd.Timestamp | None = None


def find_inducement(df: pd.DataFrame, internal_swings: list[Swing], poi: Zone, bias: int,
                    after_time: pd.Timestamp | None = None) -> Inducement | None:
    """Locate the inducement in front of ``poi`` on ``df`` and check whether it was swept."""
    if df.empty:
        return None
    after_time = after_time if after_time is not None else poi.created_time
    if bias == BULL:
        cands = [s for s in internal_swings if not s.is_high and s.price > poi.top and s.time >= after_time]
    else:
        cands = [s for s in internal_swings if s.is_high and s.price < poi.bottom and s.time >= after_time]
    if not cands:
        return None
    s = max(cands, key=lambda x: x.index)
    ind = Inducement(s.price, s.index, s.time, bias)
    start = s.confirmed_index + 1
    if start < len(df):
        series = df["low"].to_numpy(float)[start:] if bias == BULL else df["high"].to_numpy(float)[start:]
        hits = np.nonzero(series < s.price)[0] if bias == BULL else np.nonzero(series > s.price)[0]
        if hits.size:
            j = start + int(hits[0])
            ind.swept, ind.swept_index, ind.swept_time = True, j, df.index[j]
    return ind
