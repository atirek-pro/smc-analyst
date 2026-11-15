"""Configuration loading: ``config.default.json`` deep-merged with an optional ``config.json``.

Users never edit the defaults. They create ``config.json`` next to it containing
only the keys they want to change, for example::

    {"trade": {"min_rr": 3.0}, "data": {"poll_interval_seconds": 15}}
"""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = SKILL_DIR / "config.default.json"
USER_CONFIG_PATH = SKILL_DIR / "config.json"


class ConfigError(ValueError):
    """Raised when a config file is unreadable or has the wrong shape."""


def deep_merge(base: dict, override: dict) -> dict:
    """Return a new dict: ``override`` merged into ``base`` recursively.

    Nested dicts are merged; any other value in ``override`` replaces the base value.
    Keys starting with ``_`` (e.g. ``_meta``) in the override are ignored.
    """
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must contain a JSON object at the top level")
    return data


def load_config(user_path: Path | str | None = None) -> dict[str, Any]:
    """Load the effective configuration (fresh copy each call).

    ``user_path`` defaults to ``<skill-dir>/config.json``. A missing user file is fine.
    """
    cfg = _read_json(DEFAULT_CONFIG_PATH)
    path = Path(user_path) if user_path else USER_CONFIG_PATH
    if path.exists():
        cfg = deep_merge(cfg, _read_json(path))
    return cfg


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Cached effective config for the running process (treat as read-only)."""
    return load_config()


def cache_dir(cfg: dict | None = None) -> Path:
    """Directory for on-disk market-data cache.

    ``SMC_ANALYST_CACHE`` overrides it. Relative paths resolve against the skill dir.
    """
    override = os.environ.get("SMC_ANALYST_CACHE")
    if override:
        return Path(override).expanduser().resolve()
    cfg = cfg or get_config()
    p = Path(cfg["data"]["cache_dir"]).expanduser()
    return p if p.is_absolute() else SKILL_DIR / p
