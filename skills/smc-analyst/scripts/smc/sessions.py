"""ICT killzones: the session windows where institutional order flow is concentrated.

Defaults (``config.sessions``, New York time, so daylight saving is automatic):
    asia      20:00–00:00
    london    02:00–05:00
    new_york  07:00–10:00

Killzones matter most for forex, indices and commodities. For crypto (24/7) they are
informational. For single stocks they don't apply (the regular session is what counts).
"""

from __future__ import annotations

from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

RELEVANT_CLASSES = {"forex", "index", "commodity"}


def _windows(cfg_sessions: dict) -> list[tuple[str, dtime, dtime]]:
    return [(name, dtime.fromisoformat(a), dtime.fromisoformat(b))
            for name, (a, b) in cfg_sessions.get("killzones", {}).items()]


def _in_window(t: dtime, start: dtime, end: dtime) -> bool:
    if start < end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight (end 00:00 means "until midnight")


def killzone_at(ts: pd.Timestamp, cfg_sessions: dict) -> str | None:
    """Name of the killzone containing ``ts`` (tz-aware), or None."""
    tz = ZoneInfo(cfg_sessions.get("timezone", "America/New_York"))
    t = ts.tz_convert(tz).time() if ts.tzinfo else ts.tz_localize("UTC").tz_convert(tz).time()
    for name, a, b in _windows(cfg_sessions):
        if _in_window(t, a, b):
            return name
    return None


def label_killzones(index: pd.DatetimeIndex, cfg_sessions: dict) -> pd.Series:
    """Killzone name (or None) for every bar open time."""
    return pd.Series([killzone_at(t, cfg_sessions) for t in index], index=index, name="killzone")


def killzones_relevant(asset_class: str) -> bool:
    return asset_class in RELEVANT_CLASSES


def session_ranges(df: pd.DataFrame, cfg_sessions: dict) -> dict[str, dict]:
    """High/low of the most recent completed occurrence of each killzone on ``df``.

    Typical use: the Asian range high/low as liquidity for the London/New York sessions.
    The frame should be intraday (≤ 1H).
    """
    if df.empty:
        return {}
    labels = label_killzones(df.index, cfg_sessions)
    out: dict[str, dict] = {}
    for name in labels.dropna().unique():
        mask = (labels == name).to_numpy()
        # split into contiguous runs, take the last run that is followed by a non-session bar
        runs, start = [], None
        for i, m in enumerate(mask):
            if m and start is None:
                start = i
            if not m and start is not None:
                runs.append((start, i - 1))
                start = None
        if not runs:
            continue
        a, b = runs[-1]
        seg = df.iloc[a:b + 1]
        out[name] = {"high": float(seg["high"].max()), "low": float(seg["low"].min()),
                     "start": seg.index[0], "end": seg.index[-1]}
    return out
