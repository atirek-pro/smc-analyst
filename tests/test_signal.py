"""Phase 4: trade plan (entry/SL/TP/R:R), sizing, opposite scenario, correlated hedge."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from smc_builders import T0
from test_mtf import BTC, CFG, bullish_htf, brk, lvl, ltf_frame, ta, zone

from smc import signal as sig
from smc.config import SKILL_DIR
from smc.data import resolve_symbol
from smc.mtf import run_cascade


def ready_case(extra_1d_levels=(), h1_zones=()):
    """The bullish READY scenario from the cascade tests, returning (cascade, analyses)."""
    A = bullish_htf()
    df = ltf_frame()
    breaks = [brk(1, "CHoCH", 102.0, at=9, origin=6, df=df, structure="internal")]
    ob = zone("order_block", 1, 99.5, 97.5, "15m", created=df.index[9], idx=6)
    target = lvl(108, "buy", active=df.index[2])
    sweep = lvl(98.0, "sell", "swing_low", active=df.index[1], status="swept", si=6, st=df.index[6])
    A["15m"] = ta("15m", df=df, trend=-1, internal_trend=1, internal_breaks=breaks, zones=[ob],
                  levels=[target, sweep], atr=1.0)
    if extra_1d_levels:
        A["1D"].liquidity.levels.extend(extra_1d_levels)
    if h1_zones:
        A["1H"] = ta("1H", df=df, zones=list(h1_zones))
    res = run_cascade(A, BTC, CFG)
    assert res.status == "ready"
    return res, A


def cfg_with(**trade):
    c = copy.deepcopy(CFG)
    c["trade"].update(trade)
    return c


# --- plan levels ---------------------------------------------------------------------------------

def test_no_setup_passthrough():
    A = bullish_htf()
    A["15m"] = ta("15m", trend=1)
    res = run_cascade(A, BTC, CFG)
    plan = sig.build_trade_plan(res, A, BTC, CFG, with_hedge=False)
    assert plan.status == "no_setup" and plan.entry is None and "price to reach" in plan.waiting_for
    json.dumps(plan.to_dict())


def test_ready_plan_levels_and_rr():
    res, A = ready_case()
    plan = sig.build_trade_plan(res, A, BTC, CFG, with_hedge=False)
    assert plan.status == "valid" and plan.side == "long"
    assert plan.entry == 99.5                       # proximal edge of the 15m OB
    assert plan.stop_loss == 97.25                  # min(zone bottom, invalidation 97.5) - 0.25 x ATR(1.0)
    assert plan.risk_per_unit == 2.25
    assert [t.name for t in plan.targets] == ["TP1", "TP3"]
    tp1, tp3 = plan.targets
    assert tp1.price == 108 and tp1.rr == pytest.approx(3.78, abs=0.01)
    assert tp3.price == 120 and tp3.rr == pytest.approx(9.11, abs=0.01)   # daily DOL (PDH)
    assert plan.primary_target == "TP1" and plan.entry_type == "limit"
    assert any("covered" in n for n in plan.notes)  # price 105 is 65% of the way to TP1
    json.dumps(plan.to_dict())


def test_entry_mode_50_percent():
    res, A = ready_case()
    plan = sig.build_trade_plan(res, A, BTC, cfg_with(entry_mode="ob_50"), with_hedge=False)
    assert plan.entry == 98.5 and plan.entry_mode == "ob_50"


def test_min_rr_rejects_plan():
    res, A = ready_case()
    plan = sig.build_trade_plan(res, A, BTC, cfg_with(min_rr=20), with_hedge=False)
    assert plan.status == "no_valid_setup" and "below the 20 minimum" in plan.reason
    assert plan.targets  # levels still reported for transparency


def test_targets_strictly_beyond_each_other():
    res, A = ready_case()
    A["1H"] = ta("1H", levels=[lvl(112, "buy", "swing_high"), lvl(103, "buy", "swing_high")])
    plan = sig.build_trade_plan(res, A, BTC, CFG, with_hedge=False)
    prices = [t.price for t in plan.targets]
    assert [t.name for t in plan.targets] == ["TP1", "TP2", "TP3"] and prices == [108, 112, 120]
    assert prices == sorted(prices)


def test_entry_price_and_rr_helpers():
    z = zone("order_block", -1, 110, 105, "5m")
    assert sig.entry_price(z, -1, "ob_edge") == 105 and sig.entry_price(z, -1, "ob_50") == 107.5
    assert sig._rr(105, 110, 95, -1) == 2.0 and sig._rr(100, 100, 110, 1) == 0.0


# --- position sizing -------------------------------------------------------------------------------

def no_fx(base, quote):
    return None


def test_size_crypto_coins():
    pos = sig.position_size(resolve_symbol("BTCUSDT"), 100.0, 98.0, 10_000, 1.0, fx_rate=no_fx)
    assert pos.units == 50 and pos.unit_name == "BTC" and pos.risk_amount == 100
    assert any("stablecoin" in n for n in pos.notes)


def test_size_stock_whole_shares_and_leverage_note():
    pos = sig.position_size(resolve_symbol("AAPL"), 354.65, 354.20, 10_000, 1.0, fx_rate=no_fx)
    assert pos.units == 222 and pos.unit_name == "shares"
    assert any("requires leverage" in n for n in pos.notes)


def test_size_forex_usd_quote_and_lots():
    pos = sig.position_size(resolve_symbol("EURUSD"), 1.10558, 1.10628, 10_000, 1.0, fx_rate=no_fx)
    assert round(pos.units) == 142_857 and pos.lots == pytest.approx(1.4286, abs=1e-4)
    assert pos.unit_name == "EUR units"


def test_size_forex_usd_base_converts_with_price():
    pos = sig.position_size(resolve_symbol("USDJPY"), 150.0, 149.5, 10_000, 1.0, fx_rate=no_fx)
    assert pos.quote_to_account == pytest.approx(1 / 150) and pos.units == pytest.approx(30_000)


def test_size_cross_uses_fx_rate_or_notes_missing():
    gj = resolve_symbol("GBPJPY")
    pos = sig.position_size(gj, 200.0, 199.0, 10_000, 1.0, fx_rate=lambda b, q: 150.0 if (b, q) == ("USD", "JPY") else None)
    assert pos.units == pytest.approx(15_000)          # 100 USD / (1 JPY / 150)
    pos2 = sig.position_size(gj, 200.0, 199.0, 10_000, 1.0, fx_rate=no_fx)
    assert any("No JPY->USD rate" in n for n in pos2.notes)


def test_size_gold_ounces_and_lots():
    pos = sig.position_size(resolve_symbol("XAUUSD"), 3900.0, 3890.0, 10_000, 1.0, fx_rate=no_fx)
    assert pos.units == 10 and pos.unit_name == "oz" and pos.lots == 0.1


# --- opposite scenario ------------------------------------------------------------------------------

def test_opposite_scenario_breaker_flip():
    res, A = ready_case(extra_1d_levels=[lvl(92, "sell", "PDL")])
    plan = sig.build_trade_plan(res, A, BTC, CFG, with_hedge=False)
    o = plan.opposite
    assert o.direction == -1 and o.side == "short" and o.trigger_level == 97.5
    assert "closes below" in o.trigger_rule
    assert o.entry == 97.5 and o.stop_loss == 99.75   # flipped zone: proximal 97.5, distal 99.5 + 0.25
    assert o.target == 92 and o.rr == pytest.approx(2.44, abs=0.01) and o.valid
    assert "flip" in o.note


def test_opposite_scenario_prefers_real_opposing_zone():
    bear = zone("order_block", -1, 99.0, 98.0, "1H")
    res, A = ready_case(extra_1d_levels=[lvl(92, "sell", "PDL")], h1_zones=[bear])
    o = sig.build_trade_plan(res, A, BTC, CFG, with_hedge=False).opposite
    assert o.poi is bear and o.entry == 98.0 and o.stop_loss == 99.25


# --- hedge -----------------------------------------------------------------------------------------------

def series(values, start="2026-08-01"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq="1D", tz="UTC"))


def walk(seed=1, n=60):
    rng = np.random.default_rng(seed)
    return 100 * np.cumprod(1 + rng.normal(0, 0.01, n))


def test_hedge_positive_correlation_shorts_partner_for_long():
    own = series(walk(1))
    partner = series(100 * np.cumprod(1 + own.pct_change().fillna(0) * 0.5))
    h = sig.correlated_hedge(resolve_symbol("BTCUSDT"), own, 1, CFG, fetch=lambda s, d: partner, notional=10_000)
    assert h.status == "suggested" and h.instrument == "ETHUSDT" and h.direction == "short"
    assert h.correlation == pytest.approx(1.0, abs=1e-6) and h.beta == pytest.approx(2.0, abs=1e-3)
    assert h.ratio == pytest.approx(2.0, abs=1e-3) and h.hedge_notional == pytest.approx(20_000, rel=1e-3)


def test_hedge_negative_correlation_goes_long_partner():
    own = series(walk(2))
    partner = series(100 * np.cumprod(1 - own.pct_change().fillna(0)))
    h = sig.correlated_hedge(resolve_symbol("EURUSD"), own, 1, CFG, fetch=lambda s, d: partner)
    assert h.status == "suggested" and h.direction == "long" and h.correlation < -0.99
    h_short = sig.correlated_hedge(resolve_symbol("EURUSD"), own, -1, CFG, fetch=lambda s, d: partner)
    assert h_short.direction == "short"


def test_hedge_suppressed_when_weak():
    own, partner = series(walk(3)), series(walk(4))
    h = sig.correlated_hedge(resolve_symbol("ETHUSDT"), own, 1, CFG, fetch=lambda s, d: partner)
    assert h.status == "suppressed" and "< 0.5" in h.reason


def test_hedge_unavailable_on_fetch_errors():
    from smc.data import DataError

    def boom(s, d):
        raise DataError("offline")

    h = sig.correlated_hedge(resolve_symbol("EURUSD"), series(walk(5)), 1, CFG, fetch=boom)
    assert h.status == "unavailable" and all("error" in c for c in h.candidates)


def test_hedge_candidates_mapping_and_defaults():
    assert sig.hedge_candidates(resolve_symbol("EURUSD")) == ["DXY", "USDCHF", "GBPUSD"]
    assert sig.hedge_candidates(resolve_symbol("BTCUSDT")) == ["ETHUSDT"]
    assert sig.hedge_candidates(resolve_symbol("DOGEUSDT")) == ["BTCUSDT", "ETHUSDT"]
    assert sig.hedge_candidates(resolve_symbol("RELIANCE.NS")) == ["NIFTY"]
    assert sig.hedge_candidates(resolve_symbol("KO")) == ["SPY"]


def test_symbols_md_matches_correlation_map():
    text = (SKILL_DIR / "references" / "symbols.md").read_text(encoding="utf-8")
    section = text.split("## Correlated-hedge partners", 1)[1].split("### Defaults", 1)[0]
    doc = {m.group(1): re.findall(r"`([^`]+)`", m.group(2))
           for m in re.finditer(r"^\| `([^`]+)` \| (.+) \|$", section, re.M)}
    assert doc == sig.CORRELATION_MAP


def test_full_plan_with_account_and_hedge():
    res, A = ready_case()
    A["1D"].df = pd.DataFrame({"open": walk(6), "high": walk(6), "low": walk(6), "close": walk(6),
                               "volume": 1.0}, index=pd.date_range("2026-08-01", periods=60, freq="1D", tz="UTC"))
    partner = series(100 * np.cumprod(1 + A["1D"].df["close"].pct_change().fillna(0)))
    plan = sig.build_trade_plan(res, A, BTC, CFG, account=10_000, risk_pct=0.5,
                                hedge_fetch=lambda s, d: partner, fx_rate=no_fx)
    assert plan.position.risk_amount == 50 and plan.position.units == pytest.approx(50 / 2.25, abs=1e-6)
    assert plan.hedge.status == "suggested" and plan.hedge.hedge_notional is not None
    out = sig.format_plan(plan)
    assert "TRADE PLAN" in out and "Hedge" in out and "not financial advice" in out
