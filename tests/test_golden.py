"""Phase 7: golden-file tests for analyze.py's JSON and report, plus the JSON contract (schema.py).

The golden files freeze the complete output for deterministic synthetic markets. Any change to the
engine, plan or report shows up as a readable diff. After an *intended* change, regenerate them with:

    SMC_UPDATE_GOLDEN=1 python -m pytest tests/test_golden.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import analyze
from smc import schema

from test_analyze import waiting_case
from test_mtf import CFG
from test_signal import ready_case

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("SMC_UPDATE_GOLDEN") == "1"


def normalized(out: dict) -> dict:
    """Blank out the only volatile values: wall-clock timestamps and the package version."""
    out = json.loads(json.dumps(out))
    stamp = out["generated_at"][:16].replace("T", " ")
    out["report_markdown"] = out["report_markdown"].replace(stamp, "<now>")
    out["generated_at"] = out["cascade"]["generated_at"] = out["plan"]["generated_at"] = "<now>"
    out["version"] = "<version>"
    return out


def run(case, **kw) -> dict:
    return analyze.run_analysis("BTCUSDT", with_hedge=False, cfg=CFG, analyze_fn=lambda inst, cfg: case, **kw)


CASES = {
    "analyze_ready": lambda: run(ready_case(), account=10_000, risk_pct=1),
    "analyze_waiting": lambda: run(waiting_case()),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_analysis_json(name):
    out = CASES[name]()
    assert schema.validate(out, schema.ANALYSIS) == []
    got = normalized(out)
    path = GOLDEN / f"{name}.json"
    if UPDATE or not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"created missing golden file {path.name}; re-run to compare")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert got == expected, f"{name} differs from its golden file (SMC_UPDATE_GOLDEN=1 to accept)"


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_report_markdown(name):
    md = normalized(CASES[name]())["report_markdown"]
    path = GOLDEN / f"{name}.md"
    if UPDATE or not path.exists():
        path.write_text(md + "\n", encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"created missing golden file {path.name}; re-run to compare")
    assert md + "\n" == path.read_text(encoding="utf-8")


# --- the contract itself -----------------------------------------------------------------------------

def test_resolve_and_error_contracts():
    assert schema.validate(analyze.resolve_view("SOL"), schema.RESOLVE) == []
    assert schema.validate(analyze._error("SymbolError", "bad"), schema.ERROR) == []


def test_validator_reports_readable_paths():
    good = CASES["analyze_waiting"]()
    bad = json.loads(json.dumps(good))
    del bad["summary"]["headline"]
    bad["plan"]["status"] = "maybe"
    bad["cascade"]["stages"][0]["rationale"] = "not a list"
    bad["instrument"]["realtime"] = 1
    errs = schema.validate(bad, schema.ANALYSIS)
    assert "$.summary.headline: missing" in errs
    assert any(e.startswith("$.plan.status: 'maybe' not one of") for e in errs)
    assert "$.cascade.stages[0].rationale: expected list, got str" in errs
    assert any(e.startswith("$.instrument.realtime") for e in errs)


def test_validator_does_not_accept_bool_as_number():
    assert schema.validate(True, schema.NUM) and schema.validate(1.5, schema.NUM) == []
    assert schema.validate(None, schema.Nullable(int)) == [] and schema.validate(None, int)


def test_golden_files_have_no_volatile_values():
    for path in GOLDEN.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"20\d\d-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00\"", text.split('"cascade"')[0]), path.name
