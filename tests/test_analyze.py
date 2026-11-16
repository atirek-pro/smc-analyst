"""Phase 6: Claude workflow - symbol resolution, analyze.py contract, report rendering, SKILL.md."""

from __future__ import annotations

import json
import re

import pytest

import analyze
from smc import data
from smc.config import SKILL_DIR
from smc.mtf import run_cascade
from smc.signal import build_trade_plan
from smc.tracker import Journal

from test_mtf import BTC, CFG, bullish_htf, ta
from test_signal import ready_case

REFS = SKILL_DIR / "references"


# --- step 1: resolution and ambiguity ------------------------------------------------------------

@pytest.mark.parametrize("query,cls,symbol,asset_class,psym", [
    ("SOL", None, "SOLUSDT", "crypto", "SOLUSDT"),
    ("SOL", "stock", "SOL", "stock", "SOL"),
    ("NASDAQ:SOL", None, "SOL", "stock", "SOL"),
    ("BINANCE:SOL", None, "SOLUSDT", "crypto", "SOLUSDT"),
    ("NSE:RELIANCE", None, "RELIANCE.NS", "stock", "RELIANCE.NS"),
    ("LSE:SHEL", None, "SHEL.L", "stock", "SHEL.L"),
    ("BTCUSD", "crypto", "BTCUSDT", "crypto", "BTCUSDT"),
    ("eur/usd", "forex", "EURUSD", "forex", "EURUSD=X"),
    ("GOLD", "commodity", "GOLD", "commodity", "GC=F"),
    ("SPX", "index", "SPX", "index", "^GSPC"),
])
def test_resolve_with_asset_class_and_prefixes(query, cls, symbol, asset_class, psym):
    inst = data.resolve_symbol(query, cls)
    assert (inst.symbol, inst.asset_class, inst.provider_symbol) == (symbol, asset_class, psym)


@pytest.mark.parametrize("query,cls", [("AAPL", "forex"), ("XYZ", "commodity"), ("BTC", "options"), ("!!", "stock")])
def test_resolve_with_impossible_asset_class(query, cls):
    with pytest.raises(data.SymbolError):
        data.resolve_symbol(query, cls)


def test_alternative_readings():
    assert [(a.symbol, a.asset_class) for a in data.alternative_readings("SOL")] == [("SOL", "stock")]
    assert data.alternative_readings("GOLD")[0].data_note.startswith("Barrick")
    for q in ("BTCUSDT", "AAPL", "EURUSD", "NASDAQ:SOL", "btc/usdt", ""):
        assert data.alternative_readings(q) == []


def test_resolve_view_flags_ambiguity():
    v = analyze.resolve_view("SOL")
    assert v["ambiguous"] and v["instrument"]["symbol"] == "SOLUSDT"
    assert v["alternatives"][0]["select_with"] == "--asset-class stock"
    forced = analyze.resolve_view("SOL", "stock")
    assert not forced["ambiguous"] and forced["instrument"]["asset_class"] == "stock"
    assert not analyze.resolve_view("EURUSD")["ambiguous"]


# --- step 3: analysis contract ----------------------------------------------------------------------

def _fake_analyze(case):
    def fn(inst, cfg):
        return case
    return fn


def waiting_case():
    A = bullish_htf()
    A["15m"] = ta("15m", trend=1)
    return run_cascade(A, BTC, CFG), A


def test_run_analysis_ready_contract(tmp_path):
    out = analyze.run_analysis("BTCUSDT", account=10_000, risk_pct=1, with_hedge=False, track=True, cfg=CFG,
                               analyze_fn=_fake_analyze(ready_case()),
                               journal_factory=lambda: Journal(tmp_path / "j.json", CFG))
    for key in ("status", "summary", "instrument", "market", "cascade", "plan", "tracked_signal",
                "dashboard_args", "report_markdown", "timeframes", "warnings"):
        assert key in out
    assert out["status"] == "ok" and out["plan"]["status"] == "valid"
    assert out["summary"]["setup_status"] == "ready" and "valid LONG plan" in out["summary"]["headline"]
    assert out["tracked_signal"]["status"] == "pending"
    assert out["dashboard_args"] == ["--symbol", "BTCUSDT", "--account", "10000", "--risk-pct", "1"]
    json.dumps(out)


def test_run_analysis_forced_asset_class_in_dashboard_args():
    out = analyze.run_analysis("SOL", asset_class="stock", with_hedge=False, cfg=CFG,
                               analyze_fn=_fake_analyze(waiting_case()))
    assert out["dashboard_args"] == ["--symbol", "SOL", "--asset-class", "stock"]
    assert out["instrument"]["asset_class"] == "stock"


def test_track_ignored_for_invalid_plan(tmp_path):
    out = analyze.run_analysis("BTCUSDT", with_hedge=False, track=True, cfg=CFG,
                               analyze_fn=_fake_analyze(waiting_case()),
                               journal_factory=lambda: Journal(tmp_path / "j.json", CFG))
    assert out["tracked_signal"] is None and out["plan"]["status"] == "no_setup"


# --- step 5: report rendering ---------------------------------------------------------------------------

SECTIONS = ["## SMC analysis —", "**Verdict:**", "### Top-down read", "### Timeframe alignment",
            "### Trade plan", "### Risks & caveats", "not financial advice"]


def test_report_ready_has_all_sections_and_levels():
    out = analyze.run_analysis("BTCUSDT", account=10_000, with_hedge=False, cfg=CFG,
                               analyze_fn=_fake_analyze(ready_case()))
    md = out["report_markdown"]
    for s in SECTIONS + ["### If the setup fails (opposite scenario)", "> **Valid LONG setup:**"]:
        assert s in md, s
    assert "| Entry (limit) | 99.50 |" in md and "| Stop loss | 97.25 | −1.00R |" in md
    assert "| TP1 ★ | 108.00 | 3.78R |" in md and "**Position size:**" in md
    assert md.count("✅") == 5


def test_report_waiting_shows_levels_and_unreached_stages():
    out = analyze.run_analysis("BTCUSDT", with_hedge=False, cfg=CFG, analyze_fn=_fake_analyze(waiting_case()))
    md = out["report_markdown"]
    for s in SECTIONS:
        assert s in md, s
    assert "> **No trade yet — waiting for price to reach" in md
    assert "**Levels to watch**" in md and "**POI to watch:** 4H bullish OB 96.00–99.00" in md
    assert md.count("⏸️") == 1 and "⏳" in md                     # stage 4 waiting, stage 5 not reached
    assert "### If the setup fails" not in md


def test_report_no_valid_trade_callout():
    res, A = ready_case()
    res.price = 109.0                       # price already through TP1 (108): played out
    out = analyze.run_analysis("BTCUSDT", with_hedge=False, cfg=CFG, analyze_fn=_fake_analyze((res, A)))
    assert out["plan"]["status"] == "no_valid_setup"
    assert "> **No valid trade:**" in out["report_markdown"] and "played out" in out["report_markdown"]
    assert "but no valid trade" in out["summary"]["headline"]


def test_played_out_plan_is_not_valid():
    res, A = ready_case()
    res.price = 108.5
    plan = build_trade_plan(res, A, BTC, CFG, with_hedge=False)
    assert plan.status == "no_valid_setup" and plan.primary_target is None and "already reached TP1" in plan.reason


# --- CLI ----------------------------------------------------------------------------------------------------

def test_cli_resolve_json(capsys):
    assert analyze.main(["--symbol", "GOLD", "--resolve", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "resolve" and out["ambiguous"] and out["instrument"]["provider_symbol"] == "GC=F"


def test_cli_bad_symbol_exit_1(capsys):
    assert analyze.main(["--symbol", "!!!", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "SymbolError" and "hint" in out


def test_cli_data_error_exit_2(monkeypatch, capsys):
    def boom(*a, **k):
        raise data.DataError("Binance unavailable")
    monkeypatch.setattr(analyze, "run_analysis", boom)
    assert analyze.main(["--symbol", "BTCUSDT", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "DataError"


def test_cli_markdown_output(monkeypatch, capsys):
    real = analyze.run_analysis
    monkeypatch.setattr(analyze, "run_analysis", lambda *a, **k: real(
        "BTCUSDT", with_hedge=False, cfg=CFG, analyze_fn=_fake_analyze(waiting_case())))
    assert analyze.main(["--symbol", "BTCUSDT"]) == 0
    assert capsys.readouterr().out.startswith("## SMC analysis — BTCUSDT")


# --- SKILL.md and references -------------------------------------------------------------------------------

def test_skill_md_budget_and_wiring():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) < 300
    assert "Build status" not in text, "the work-in-progress banner must be gone"
    for ref in REFS.glob("*.md"):
        assert f"references/{ref.name}" in text, f"SKILL.md never points to {ref.name}"
    for script in ("setup.py", "analyze.py", "serve.py"):
        assert f"scripts/{script}" in text
        assert (SKILL_DIR / "scripts" / script).exists()
    for flag in re.findall(r"analyze\.py\"? (--[a-z-]+)", text):
        assert flag in ("--symbol", "--resolve", "--json"), flag


def test_output_format_documents_report_sections():
    doc = (REFS / "output-format.md").read_text(encoding="utf-8")
    for heading in ("Top-down read", "Timeframe alignment", "Trade plan", "If the setup fails", "Hedge",
                    "Risks & caveats", "Live dashboard", "not financial advice"):
        assert heading in doc, heading
