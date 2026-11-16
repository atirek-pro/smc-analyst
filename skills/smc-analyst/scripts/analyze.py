#!/usr/bin/env python3
"""analyze.py - the single entry point Claude runs for an SMC analysis.

    python analyze.py --symbol SOL --resolve --json        # offline: how is the symbol read? ambiguous?
    python analyze.py --symbol BTCUSDT --json              # full top-down analysis + trade plan + report
    python analyze.py --symbol EURUSD --account 10000 --risk-pct 1 --json
    python analyze.py --symbol SOL --asset-class stock --json
    python analyze.py --symbol AAPL --format markdown      # human-readable report only

JSON output (``--json``): one object with ``status``, ``summary``, ``instrument``, ``market``,
``cascade``, ``plan``, ``tracked_signal``, ``dashboard_args`` and a ready-to-present
``report_markdown`` that follows ``references/output-format.md``.

Exit codes: 0 ok · 1 bad symbol / input · 2 market-data error · 3 environment not set up.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import pandas  # noqa: F401  (fails fast outside the skill's virtualenv)

    from smc import __version__, data
    from smc.common import jsonable
    from smc.config import get_config
    from smc.mtf import TF_ORDER, analyze_symbol
    from smc.signal import build_trade_plan
except ImportError as _exc:  # pragma: no cover - exercised only outside the venv
    print(json.dumps({
        "status": "error", "error": "EnvironmentError",
        "message": f"Missing dependency: {getattr(_exc, 'name', None) or _exc}.",
        "hint": "Run `python <skill-dir>/scripts/setup.py --json` and re-run this script with the "
                "`venv_python` it prints.",
    }))
    sys.exit(3)

STATUS_ICON = {"passed": "✅", "failed": "❌", "waiting": "⏳", "skipped": "➖"}
STAGE_NAMES = ["HTF bias", "Daily confirmation and target POI", "4H refinement and inducement",
               "POI tap and LTF confirmation", "Entry refinement"]
STATUS_TEXT = {
    "ready": "READY - confirmed setup",
    "waiting": "WAITING - aligned, price has not reached the POI",
    "in_poi": "IN POI - waiting for the lower-timeframe CHoCH",
    "no_setup": "NO SETUP",
    "no_bias": "NO BIAS",
}
ZONE_NAMES = {"order_block": "OB", "fvg": "FVG", "demand": "demand zone", "supply": "supply zone"}
LIQ_NAMES = {"swing_high": "swing high", "swing_low": "swing low", "equal_highs": "equal highs",
             "equal_lows": "equal lows"}


# =============================================================================
# Symbol resolution (step 1 of the workflow)
# =============================================================================

def resolve_view(symbol: str, asset_class: str | None = None) -> dict:
    """How the input will be read, and the other plausible readings (offline)."""
    inst = data.resolve_symbol(symbol, asset_class)
    alts = [] if asset_class else data.alternative_readings(symbol)
    return {
        "status": "ok", "mode": "resolve", "query": symbol,
        "instrument": inst.to_dict(), "market": data.market_status(inst),
        "ambiguous": bool(alts),
        "alternatives": [{"symbol": a.symbol, "asset_class": a.asset_class, "provider_symbol": a.provider_symbol,
                          "note": a.data_note, "select_with": f"--asset-class {a.asset_class}"} for a in alts],
        "default_select_with": f"--asset-class {inst.asset_class}",
    }


# =============================================================================
# Analysis
# =============================================================================

def run_analysis(symbol: str, asset_class: str | None = None, account: float | None = None,
                 risk_pct: float | None = None, with_hedge: bool = True, track: bool = False,
                 cfg: dict | None = None, analyze_fn: Callable = analyze_symbol,
                 journal_factory: Callable | None = None) -> dict:
    cfg = cfg or get_config()
    inst = data.resolve_symbol(symbol, asset_class)
    res, analyses = analyze_fn(inst, cfg)
    if analyses:
        info = next(iter(analyses.values())).df.attrs.get("instrument") or {}
        if info:  # exact exchange tick / provider details discovered while fetching
            inst = replace(inst, tick_size=info.get("tick_size", inst.tick_size),
                           provider=info.get("provider", inst.provider),
                           provider_symbol=info.get("provider_symbol", inst.provider_symbol),
                           realtime=info.get("realtime", inst.realtime),
                           data_note=info.get("data_note", inst.data_note))
    plan = build_trade_plan(res, analyses, inst, cfg, account=account, risk_pct=risk_pct, with_hedge=with_hedge)
    tracked = None
    if track and plan.is_valid:
        if journal_factory is None:
            from smc.tracker import Journal
            journal_factory = Journal
        tracked = journal_factory().add_plan(plan, inst).to_dict()

    cascade, plan_d = res.to_dict(), plan.to_dict()
    dash = ["--symbol", inst.symbol]
    if asset_class:
        dash += ["--asset-class", asset_class]
    if account:
        dash += ["--account", f"{account:g}"]
    if risk_pct:
        dash += ["--risk-pct", f"{risk_pct:g}"]
    result = {
        "status": "ok", "mode": "analysis", "version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "symbol": inst.symbol, "instrument": inst.to_dict(), "market": data.market_status(inst),
        "timeframes": [tf for tf in TF_ORDER if tf in analyses],
        "summary": summarize(cascade, plan_d, inst.symbol),
        "cascade": cascade, "plan": plan_d, "tracked_signal": tracked,
        "warnings": cascade.get("warnings", []),
        "dashboard_args": dash,
    }
    result["report_markdown"] = render_report(result)
    return jsonable(result)


def summarize(cascade: dict, plan: dict, symbol: str) -> dict:
    status = cascade["status"]
    headline = f"{symbol}: {cascade['bias_name']} bias"
    if cascade.get("bias_source"):
        headline += f" ({cascade['bias_source']})"
    headline += f" - {STATUS_TEXT.get(status, status.upper())}"
    if plan["status"] == "valid":
        headline += f"; valid {plan['side'].upper()} plan, {plan['primary_target']} at " \
                    f"{next(t['rr'] for t in plan['targets'] if t['name'] == plan['primary_target']):.2f}R"
    elif plan["status"] == "no_valid_setup":
        headline += f"; but no valid trade: {plan['reason']}"
    elif cascade.get("waiting_for"):
        headline += f"; waiting for {cascade['waiting_for']}"
    return {
        "headline": headline, "bias": cascade["bias_name"], "bias_source": cascade.get("bias_source"),
        "setup_status": status, "confluence": cascade.get("confluence", {}).get("score", 0),
        "waiting_for": cascade.get("waiting_for"), "plan_status": plan["status"],
        "side": plan.get("side") if plan["status"] != "no_setup" else None,
    }


# =============================================================================
# Report rendering (references/output-format.md)
# =============================================================================

def _p(x, dec: int) -> str:
    return "-" if x is None else f"{x:,.{dec}f}"


def _zone(z: dict | None, dec: int) -> str:
    if not z:
        return "-"
    kind = ZONE_NAMES.get(z["kind"], z["kind"])
    prefix = "" if z["kind"] in ("demand", "supply") else f"{z.get('direction_name', '')} "
    return f"{z.get('timeframe', '')} {prefix}{kind} {_p(z['bottom'], dec)}–{_p(z['top'], dec)}".strip()


def _level(lv: dict | None, dec: int) -> str:
    if not lv:
        return "-"
    return f"{LIQ_NAMES.get(lv['kind'], lv['kind'])} {_p(lv['price'], dec)}"


def render_report(r: dict) -> str:
    inst, cas, plan, mkt = r["instrument"], r["cascade"], r["plan"], r["market"]
    dec = inst.get("price_precision", 2)
    p = lambda x: _p(x, dec)  # noqa: E731
    status = cas["status"]
    bias_icon = {"bullish": "🟢", "bearish": "🔴"}.get(cas["bias_name"], "⚪")
    L: list[str] = []

    # 1. header + verdict
    L.append(f"## SMC analysis — {inst['symbol']} ({inst['asset_class']})")
    verdict = f"**Verdict:** {bias_icon} {cas['bias_name'].capitalize()} bias"
    if cas.get("bias_source"):
        verdict += f" ({cas['bias_source']})"
    verdict += f" · **{STATUS_TEXT.get(status, status.upper())}** · confluence **{cas['confluence']['score']}/100**"
    L.append(verdict)
    when = r.get("generated_at", "")[:16].replace("T", " ")
    market = "market open" if mkt.get("state") == "open" else (
        f"market closed (next open {mkt['next_open'].replace('T', ' ')} UTC)" if mkt.get("next_open")
        else f"market {mkt.get('state', 'unknown')}")
    L.append(f"Price **{p(cas['price'])}** · {when} UTC · {inst.get('data_note', '')} · {market}")
    if cas.get("waiting_for"):
        L.append(f"\n> **No trade yet — waiting for {cas['waiting_for']}.**")
    elif plan.get("status") == "no_valid_setup":
        L.append(f"\n> **No valid trade:** {plan.get('reason', '')}")
    elif plan.get("status") == "valid":
        L.append(f"\n> **Valid {(plan.get('side') or '').upper()} setup:** {plan.get('reason', '')}")

    # 2. top-down read
    L.append("\n### Top-down read")
    done = {s["stage"]: s for s in cas["stages"]}
    for n, name in enumerate(STAGE_NAMES, start=1):
        st = done.get(n)
        if st is None:
            L.append(f"{n}. ⏸️ **{name}** — not reached (an earlier stage stopped the cascade).")
            continue
        L.append(f"{n}. {STATUS_ICON.get(st['status'], '•')} **{st['name']}** "
                 f"({', '.join(st['timeframes'])}) — {st['status']}")
        L.extend(f"   - {line}" for line in st["rationale"])

    # 3. alignment
    L.append("\n### Timeframe alignment")
    L.append("| TF | Swing | Internal | Last break | Price in |")
    L.append("|---|---|---|---|---|")
    for tf, v in cas.get("alignment", {}).items():
        mark = " ✓" if v.get("aligned") else ""
        L.append(f"| {tf} | {v['swing_trend']}{mark} | {v['internal_trend']} | {v.get('last_break') or '-'} "
                 f"| {v.get('pd_zone') or '-'} |")

    # 4. trade plan
    L.append("\n### Trade plan")
    if plan.get("entry") is None:
        L.append(f"No trade. {plan.get('reason', '')}")
        watch = []
        if cas.get("refined_poi"):
            watch.append(f"- **POI to watch:** {_zone(cas['refined_poi'], dec)}")
        elif cas.get("target_poi"):
            watch.append(f"- **POI to watch:** {_zone(cas['target_poi'], dec)}")
        ind = cas.get("inducement")
        if ind:
            watch.append(f"- **Inducement:** {p(ind['price'])} ({'swept' if ind.get('swept') else 'still resting'})")
        if cas.get("draw_on_liquidity"):
            watch.append(f"- **Draw on liquidity:** {_level(cas['draw_on_liquidity'], dec)}")
        if watch:
            L.append("\n**Levels to watch**")
            L.extend(watch)
    else:
        side = (plan.get("side") or "").upper()
        L.append(f"**{side}** · {plan['status'].replace('_', ' ')} · {plan.get('entry_type')} entry · "
                 f"{plan.get('reason', '')}")
        L.append("\n| | Price | R:R | Basis |")
        L.append("|---|---|---|---|")
        L.append(f"| Entry ({plan.get('entry_type')}) | {p(plan['entry'])} | | {_zone(plan.get('entry_zone'), dec)} |")
        L.append(f"| Stop loss | {p(plan['stop_loss'])} | −1.00R | beyond invalidation {p(plan.get('invalidation_level'))}"
                 f" + ATR buffer |")
        for t in plan.get("targets", []):
            star = " ★" if t["name"] == plan.get("primary_target") else ""
            L.append(f"| {t['name']}{star} | {p(t['price'])} | {t['rr']:.2f}R | {t['source']} |")
        pos = plan.get("position")
        if pos:
            lots = f" = {pos['lots']} {pos['lot_name']}" if pos.get("lots") is not None else ""
            L.append(f"\n**Position size:** {pos['units']:,g} {pos['unit_name']}{lots} — risking "
                     f"{pos['risk_amount']:,.2f} {pos['account_currency']} ({pos['risk_pct']:g}% of "
                     f"{pos['account_size']:,.0f}).")
        for n in plan.get("notes", []):
            L.append(f"- {n}")

    # 5. opposite scenario
    o = plan.get("opposite")
    if o and o.get("entry") is not None:
        L.append("\n### If the setup fails (opposite scenario)")
        L.append(f"If {o['trigger_rule']}: **{o['side'].upper()}** at {p(o['entry'])}, stop {p(o['stop_loss'])}, "
                 f"target {p(o['target'])} ({o['target_source'] or '-'}) — "
                 f"{o['rr'] if o['rr'] is not None else '-'}R{'' if o['valid'] else ' (information only)'}. {o['note']}")

    # 6. hedge
    h = plan.get("hedge")
    if h:
        L.append("\n### Hedge")
        if h["status"] == "suggested":
            size = f" ≈ {h['hedge_units']:,g} units ({h['hedge_notional']:,.0f} notional)" if h.get("hedge_units") else ""
            L.append(f"**{h['direction'].upper()} {h['instrument']}** at {h['ratio']}× the position notional{size}. "
                     f"{h['reason']}")
        else:
            L.append(f"No hedge ({h['status']}): {h['reason']}")

    # 7. risks
    L.append("\n### Risks & caveats")
    risks = [f"Data: {inst.get('data_note', '')}."]
    if mkt.get("state") == "closed":
        risks.append("The market is closed; price can gap through levels at the next open.")
    pos = plan.get("position") or {}
    risks += [n for n in pos.get("notes", []) if "leverage" in n]
    if plan.get("entry_type") == "limit":
        risks.append("Limit entry: no position until price retraces to the entry; a pending signal expires after "
                     "48 h, or if TP1 trades first.")
    risks += [f"Engine warning: {w}" for w in cas.get("warnings", [])]
    risks.append("This is a rules-based reading of Smart Money Concepts (see references/smc-methodology.md); it "
                 "will not match every trader's markup.")
    L.extend(f"- {x}" for x in risks)

    L.append("\n---\n*Educational analysis only — not financial advice. Trading involves substantial risk of loss.*")
    return "\n".join(L)


# =============================================================================
# CLI
# =============================================================================

def _error(kind: str, message: str, hint: str | None = None) -> dict:
    out = {"status": "error", "error": kind, "message": message}
    if hint:
        out["hint"] = hint
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SMC Analyst: top-down analysis, trade plan and report.")
    ap.add_argument("--symbol", required=True, help="e.g. BTCUSDT, EURUSD, XAUUSD, AAPL, NASDAQ:SOL")
    ap.add_argument("--asset-class", choices=data.ASSET_CLASSES, default=None,
                    help="force how an ambiguous symbol is read")
    ap.add_argument("--resolve", action="store_true", help="only resolve the symbol (offline, instant)")
    ap.add_argument("--account", type=float, default=None, help="account size for position sizing")
    ap.add_argument("--risk-pct", type=float, default=None, help="percent of the account risked")
    ap.add_argument("--no-hedge", action="store_true", help="skip the correlated-hedge lookup (faster)")
    ap.add_argument("--track", action="store_true", help="add a valid plan to the signal journal")
    ap.add_argument("--json", action="store_true", help="print one JSON object")
    ap.add_argument("--format", choices=("json", "markdown"), default=None)
    args = ap.parse_args(argv)
    fmt = "json" if args.json else (args.format or "markdown")
    if fmt == "markdown":
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # icons / dashes on Windows consoles
        except (AttributeError, ValueError):
            pass

    try:
        out = resolve_view(args.symbol, args.asset_class) if args.resolve else run_analysis(
            args.symbol, args.asset_class, args.account, args.risk_pct, not args.no_hedge, args.track)
        code = 0
    except data.SymbolError as exc:
        out, code = _error("SymbolError", str(exc), "Check the symbol, or add an exchange prefix "
                                                    "(e.g. NASDAQ:SOL, BINANCE:SOLUSDT, FX:EURUSD)."), 1
    except data.DataError as exc:
        out, code = _error("DataError", str(exc), "Market data is temporarily unavailable; retry shortly."), 2
    except ValueError as exc:
        out, code = _error("ValueError", str(exc)), 1

    if fmt == "json":
        print(json.dumps(out))
    elif out["status"] != "ok":
        print(f"Error ({out['error']}): {out['message']}" + (f"\n{out['hint']}" if out.get("hint") else ""))
    elif out["mode"] == "resolve":
        inst = out["instrument"]
        print(f"{args.symbol} -> {inst['symbol']} [{inst['asset_class']}] via {inst['provider']} "
              f"({inst['provider_symbol']})")
        for a in out["alternatives"]:
            print(f"  also possible: {a['symbol']} [{a['asset_class']}] - {a['note']} ({a['select_with']})")
    else:
        print(out["report_markdown"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
