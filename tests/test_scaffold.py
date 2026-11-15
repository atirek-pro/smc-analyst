"""Phase 1 structural tests: manifests, SKILL.md, config, requirements, setup.py.

Run from the repo root:  python -m pytest -q
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "smc-analyst"
SETUP = SKILL_DIR / "scripts" / "setup.py"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_frontmatter(text: str) -> dict:
    m = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.S)
    assert m, "SKILL.md must start with a --- frontmatter block"
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


# --- repository layout -------------------------------------------------------

def test_expected_layout_exists():
    for rel in [
        "LICENSE", "README.md", "CHANGELOG.md", ".gitignore",
        ".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
        "skills/smc-analyst/SKILL.md", "skills/smc-analyst/config.default.json",
        "skills/smc-analyst/scripts/setup.py", "skills/smc-analyst/scripts/requirements.txt",
        "skills/smc-analyst/references", "skills/smc-analyst/scripts/smc",
        "skills/smc-analyst/assets/dashboard/vendor",
    ]:
        assert (ROOT / rel).exists(), f"missing {rel}"


def test_gitignore_excludes_runtime_artifacts():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ["smc-skill-env/", ".venv/", "__pycache__/", "cache/", "journal.json"]:
        assert pattern in ignored


# --- SKILL.md ----------------------------------------------------------------

def test_skill_frontmatter():
    fm = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    assert fm.get("name") == "smc-analyst" == SKILL_DIR.name
    assert NAME_RE.match(fm["name"])
    desc = fm.get("description", "")
    assert 50 < len(desc) <= 1024, "description must exist and stay within 1024 chars"
    for trigger in ["SMC", "ICT", "BOS", "CHoCH", "order blocks", "stock", "crypto", "forex"]:
        assert trigger in desc, f"description should mention trigger '{trigger}'"


# --- plugin + marketplace ----------------------------------------------------

def test_plugin_manifest():
    plugin = load_json(ROOT / ".claude-plugin" / "plugin.json")
    assert plugin["name"] == "smc-analyst" and NAME_RE.match(plugin["name"])
    assert re.match(r"^\d+\.\d+\.\d+$", plugin["version"])
    assert plugin["description"]


def test_marketplace_matches_plugin():
    market = load_json(ROOT / ".claude-plugin" / "marketplace.json")
    plugin = load_json(ROOT / ".claude-plugin" / "plugin.json")
    assert NAME_RE.match(market["name"])
    assert market["owner"]["name"]
    entries = {p["name"]: p for p in market["plugins"]}
    assert plugin["name"] in entries, "marketplace entry name must equal plugin.json name"
    source = entries[plugin["name"]]["source"]
    assert ".." not in source
    assert (ROOT / source / ".claude-plugin" / "plugin.json").exists()


# --- config + requirements ---------------------------------------------------

def test_default_config_shape():
    cfg = load_json(SKILL_DIR / "config.default.json")
    for section in ["timeframes", "swings", "atr", "structure", "liquidity", "zones",
                    "pd_array", "sessions", "trade", "risk", "hedge", "tracker", "data", "server"]:
        assert section in cfg, f"config missing section '{section}'"
    assert cfg["timeframes"]["ladder"][0] == "1W" and cfg["timeframes"]["ladder"][-1] == "1m"
    assert set(cfg["timeframes"]["ladder"]) <= set(cfg["swings"]["lookback"])
    assert cfg["trade"]["min_rr"] >= 1
    lo, hi = cfg["pd_array"]["ote"]
    assert 0 < lo < hi < 1
    p_lo, p_hi = cfg["server"]["port_range"]
    assert 1024 < p_lo <= p_hi < 65536


def test_requirements_are_pinned():
    lines = [ln.strip() for ln in (SKILL_DIR / "scripts" / "requirements.txt")
             .read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.startswith("#")]
    assert lines
    for ln in lines:
        assert re.match(r"^[A-Za-z0-9_.\-]+==[\w.]+$", ln), f"not exactly pinned: {ln}"


# --- setup.py ----------------------------------------------------------------

def _run_setup(*args: str, env_venv: Path | None = None) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    if env_venv is not None:
        env["SMC_ANALYST_VENV"] = str(env_venv)
    return subprocess.run([sys.executable, str(SETUP), *args], capture_output=True,
                          text=True, env=env, timeout=120)


def test_setup_check_reports_not_ready_for_missing_venv(tmp_path):
    proc = _run_setup("--check", "--json", env_venv=tmp_path / "nope")
    assert proc.returncode == 2
    status = json.loads(proc.stdout.strip().splitlines()[-1])
    assert status["status"] == "not_ready"
    assert status["venv_python"].endswith(("python.exe", "python"))


def test_setup_check_does_not_create_anything(tmp_path):
    target = tmp_path / "venv"
    _run_setup("--check", env_venv=target)
    assert not target.exists()
