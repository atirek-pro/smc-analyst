"""Shared helpers for the SMC engine: directions, closed-bar slicing and JSON conversion."""

from __future__ import annotations

import math
from dataclasses import fields, is_dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import data

BULL = 1
BEAR = -1
NEUTRAL = 0


def dir_name(direction: int) -> str:
    return {BULL: "bullish", BEAR: "bearish"}.get(direction, "neutral")


def closed_bars(df: pd.DataFrame, tf: str, now=None) -> pd.DataFrame:
    """Drop the still-forming last bar. Structure is only ever judged on closed candles."""
    if df.empty:
        return df
    closed = df.attrs.get("last_bar_closed") if now is None else None
    if closed is None:
        closed = data.is_bar_closed(df.index[-1], tf, now)
    return df if closed else df.iloc[:-1]


def ts_at(df: pd.DataFrame, i: int | None) -> pd.Timestamp | None:
    return None if i is None or i < 0 or i >= len(df) else df.index[i]


def index_at_or_after(df: pd.DataFrame, ts: pd.Timestamp | None) -> int:
    """Position of the first bar whose open time is >= ``ts`` (len(df) if none)."""
    if ts is None:
        return 0
    return int(df.index.searchsorted(ts, side="left"))


def jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses, timestamps and numpy scalars into JSON-safe values.

    Dataclass fields declared with ``metadata={"json": False}`` are skipped.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        out = {}
        for f in fields(obj):
            if f.metadata.get("json", True):
                out[f.name] = jsonable(getattr(obj, f.name))
        for name in getattr(obj, "_json_extra", ()):
            out[name] = jsonable(getattr(obj, name))
        return out
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def fmt_price(price: float | None, tick: float | None = None) -> str:
    """Human-friendly price for rationale text."""
    if price is None:
        return "n/a"
    if tick:
        return f"{price:.{data.tick_decimals(tick)}f}"
    return f"{price:.8g}"


def fmt_time(ts: pd.Timestamp | None) -> str:
    return "n/a" if ts is None else ts.strftime("%Y-%m-%d %H:%M UTC")
