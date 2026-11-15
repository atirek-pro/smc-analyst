"""Phase 2: config loader."""

from __future__ import annotations

import json

import pytest

from smc import config


def test_deep_merge_nested_and_ignores_meta():
    base = {"a": {"x": 1, "y": 2}, "b": 1, "_meta": {"v": 1}}
    out = config.deep_merge(base, {"a": {"y": 20, "z": 3}, "b": [1, 2], "_meta": {"v": 9}})
    assert out == {"a": {"x": 1, "y": 20, "z": 3}, "b": [1, 2], "_meta": {"v": 1}}
    assert base["a"]["y"] == 2, "base must not be mutated"


def test_load_defaults_without_user_file(tmp_path):
    cfg = config.load_config(tmp_path / "missing.json")
    assert cfg["trade"]["min_rr"] == 2.0
    assert cfg["timeframes"]["ladder"][0] == "1W"


def test_user_override_merges(tmp_path):
    user = tmp_path / "config.json"
    user.write_text(json.dumps({"trade": {"min_rr": 3.0}}), encoding="utf-8")
    cfg = config.load_config(user)
    assert cfg["trade"]["min_rr"] == 3.0
    assert cfg["trade"]["sl_atr_buffer"] == 0.25, "untouched keys keep defaults"


@pytest.mark.parametrize("content", ["{not json", "[1, 2]"])
def test_bad_user_file_raises(tmp_path, content):
    user = tmp_path / "config.json"
    user.write_text(content, encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load_config(user)


def test_cache_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SMC_ANALYST_CACHE", str(tmp_path / "c"))
    assert config.cache_dir() == (tmp_path / "c").resolve()


def test_cache_dir_relative_resolves_to_skill_dir(monkeypatch):
    monkeypatch.delenv("SMC_ANALYST_CACHE", raising=False)
    assert config.cache_dir({"data": {"cache_dir": "cache"}}) == config.SKILL_DIR / "cache"
