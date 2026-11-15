"""Shared pytest setup: make the engine importable and isolate config/cache per test."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "smc-analyst" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own empty cache dir and a fresh config cache."""
    monkeypatch.setenv("SMC_ANALYST_CACHE", str(tmp_path / "cache"))
    try:
        from smc import config
        config.get_config.cache_clear()
    except ImportError:  # engine deps not installed: scaffold tests still run
        pass
    yield
