"""The JSON contract of ``analyze.py`` (what Claude and the tests rely on), with a tiny validator.

No third-party dependency: a spec is a Python type, a tuple of types, a nested dict
(required keys), ``ListOf(spec)``, ``Nullable(spec)`` or ``OneOf(*values)``.

    errors = validate(obj, ANALYSIS)   # [] when valid, else readable "path: problem" strings
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NUM = (int, float)


@dataclass(frozen=True)
class ListOf:
    item: Any


@dataclass(frozen=True)
class Nullable:
    spec: Any


@dataclass(frozen=True)
class OneOf:
    values: tuple

    def __init__(self, *values):
        object.__setattr__(self, "values", values)


STAGE = {"stage": int, "name": str, "timeframes": ListOf(str),
         "status": OneOf("passed", "failed", "waiting", "skipped"), "rationale": ListOf(str), "outputs": dict}
ZONE = {"kind": OneOf("order_block", "fvg", "demand", "supply"), "direction": int, "top": NUM, "bottom": NUM,
        "timeframe": str, "state": OneOf("fresh", "mitigated", "invalidated"), "time": str, "created_time": str}
LEVEL = {"price": NUM, "side": OneOf("buy", "sell"), "kind": str, "status": OneOf("resting", "swept", "taken")}
TARGET = {"name": OneOf("TP1", "TP2", "TP3"), "price": NUM, "rr": NUM, "source": str}

CASCADE = {
    "symbol": str, "asset_class": str, "price": NUM, "generated_at": str,
    "status": OneOf("no_bias", "no_setup", "waiting", "in_poi", "ready"),
    "bias": OneOf(-1, 0, 1), "bias_name": OneOf("bullish", "bearish", "neutral"), "bias_source": Nullable(str),
    "waiting_for": Nullable(str), "stages": ListOf(STAGE), "htf_pois": ListOf(ZONE),
    "target_poi": Nullable(ZONE), "refined_poi": Nullable(ZONE), "entry_zone": Nullable(ZONE),
    "invalidation_level": Nullable(NUM), "targets": ListOf({"role": OneOf("internal", "external"), "level": LEVEL}),
    "alignment": dict, "confluence": {"score": int, "breakdown": ListOf({"factor": str, "points": int, "max": int})},
    "warnings": ListOf(str),
}
PLAN = {
    "symbol": str, "status": OneOf("valid", "no_valid_setup", "no_setup"), "direction": OneOf(-1, 0, 1),
    "side": Nullable(OneOf("long", "short")), "price": NUM, "cascade_status": str, "confluence": int,
    "reason": str, "waiting_for": Nullable(str), "entry": Nullable(NUM), "stop_loss": Nullable(NUM),
    "entry_type": Nullable(OneOf("limit", "market")), "targets": ListOf(TARGET), "primary_target": Nullable(str),
    "min_rr": NUM, "position": Nullable(dict), "opposite": Nullable(dict), "hedge": Nullable(dict),
    "notes": ListOf(str),
}
ANALYSIS = {
    "status": OneOf("ok"), "mode": OneOf("analysis"), "version": str, "generated_at": str, "symbol": str,
    "instrument": {"symbol": str, "asset_class": str, "provider": str, "provider_symbol": str, "tick_size": NUM,
                   "price_precision": int, "realtime": bool, "data_note": str},
    "market": {"state": OneOf("open", "closed", "unknown"), "reason": str, "next_open": Nullable(str)},
    "timeframes": ListOf(str),
    "summary": {"headline": str, "bias": str, "setup_status": str, "confluence": int, "plan_status": str,
                "waiting_for": Nullable(str), "side": Nullable(str)},
    "cascade": CASCADE, "plan": PLAN, "tracked_signal": Nullable(dict), "warnings": ListOf(str),
    "dashboard_args": ListOf(str), "report_markdown": str,
}
RESOLVE = {
    "status": OneOf("ok"), "mode": OneOf("resolve"), "query": str, "instrument": dict, "market": dict,
    "ambiguous": bool, "alternatives": ListOf({"symbol": str, "asset_class": str, "select_with": str}),
}
ERROR = {"status": OneOf("error"), "error": str, "message": str}


def validate(obj: Any, spec: Any, path: str = "$") -> list[str]:
    """Return a list of contract violations (empty when ``obj`` satisfies ``spec``)."""
    if isinstance(spec, Nullable):
        return [] if obj is None else validate(obj, spec.spec, path)
    if obj is None:
        return [f"{path}: is null"]
    if isinstance(spec, OneOf):
        return [] if obj in spec.values else [f"{path}: {obj!r} not one of {spec.values}"]
    if isinstance(spec, ListOf):
        if not isinstance(obj, list):
            return [f"{path}: expected list, got {type(obj).__name__}"]
        return [e for i, item in enumerate(obj) for e in validate(item, spec.item, f"{path}[{i}]")]
    if isinstance(spec, dict):
        if not isinstance(obj, dict):
            return [f"{path}: expected object, got {type(obj).__name__}"]
        errs = [f"{path}.{k}: missing" for k in spec if k not in obj]
        return errs + [e for k, sub in spec.items() if k in obj for e in validate(obj[k], sub, f"{path}.{k}")]
    types = spec if isinstance(spec, tuple) else (spec,)
    if bool in types and isinstance(obj, bool):
        return []
    if isinstance(obj, bool) and bool not in types:  # bool is an int subclass: don't accept it as a number
        return [f"{path}: expected {'/'.join(t.__name__ for t in types)}, got bool"]
    return [] if isinstance(obj, types) else [f"{path}: expected {'/'.join(t.__name__ for t in types)}, "
                                               f"got {type(obj).__name__}"]
