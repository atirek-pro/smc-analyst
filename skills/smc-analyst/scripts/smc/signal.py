"""Trade plan: turn a confirmed cascade setup into prices, risk, sizing, a counter-scenario and a hedge.

Input: a ``CascadeResult`` from ``mtf.run_cascade``. A priced plan is only built when its
status is ``ready``. Any other status produces a ``no_setup`` plan that carries the cascade's
``waiting_for``, because the gate rule never forces a trade.

Rules (all thresholds live in ``config.default.json``):
    Entry    proximal edge of the entry zone (``trade.entry_mode = "ob_edge"``) or its 50 % ("ob_50")
    SL       beyond the entry zone's distal edge or the invalidation (CHoCH-leg) extreme, whichever
             is further, plus ``trade.sl_atr_buffer`` × ATR of the confirmation timeframe
    TP1      nearest opposing liquidity beyond the entry (cascade internal target / LTF pools)
    TP2      next external swing beyond TP1 (1H, then 4H swing pools)
    TP3      external draw on liquidity (daily / HTF) or the nearest opposing HTF POI beyond TP2
    R:R      (target - entry) / (entry - SL) per target. The plan is **valid** when at least one
             target reaches ``trade.min_rr``; the first such target is the *primary* target.
             Otherwise the status is ``no_valid_setup``.

CLI (run from the scripts/ directory):
    python -m smc.signal BTCUSDT --account 10000 --risk-pct 1
    python -m smc.signal EURUSD --json --no-hedge
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from . import data
from .common import BEAR, BULL, dir_name, fmt_price, jsonable
from .config import get_config
from .indicators import correlation_and_beta
from .mtf import CascadeResult, TimeframeAnalysis
from .zones import Zone

log = logging.getLogger("smc.signal")

# Partner candidates per symbol, in priority order. Kept in sync with references/symbols.md
# (a test asserts both agree).
CORRELATION_MAP: dict[str, list[str]] = {
    "EURUSD": ["DXY", "USDCHF", "GBPUSD"],
    "GBPUSD": ["EURUSD", "DXY"],
    "AUDUSD": ["NZDUSD", "DXY"],
    "NZDUSD": ["AUDUSD", "DXY"],
    "USDJPY": ["DXY", "EURJPY"],
    "USDCHF": ["EURUSD", "DXY"],
    "USDCAD": ["USOIL", "DXY"],
    "EURJPY": ["GBPJPY", "USDJPY"],
    "GBPJPY": ["EURJPY", "USDJPY"],
    "XAUUSD": ["DXY", "XAGUSD"],
    "XAGUSD": ["XAUUSD", "DXY"],
    "USOIL": ["UKOIL", "USDCAD"],
    "UKOIL": ["USOIL"],
    "BTCUSDT": ["ETHUSDT"],
    "ETHUSDT": ["BTCUSDT"],
    "SOLUSDT": ["ETHUSDT", "BTCUSDT"],
    "SPX": ["NAS100", "US30"],
    "NAS100": ["SPX"],
    "US30": ["SPX"],
    "DXY": ["EURUSD"],
    "AAPL": ["QQQ", "SPY"], "MSFT": ["QQQ", "SPY"], "NVDA": ["QQQ", "SPY"], "AMZN": ["QQQ", "SPY"],
    "GOOGL": ["QQQ", "SPY"], "META": ["QQQ", "SPY"], "TSLA": ["QQQ", "SPY"],
}
DEFAULT_PARTNERS: dict[str, list[str]] = {
    "crypto": ["BTCUSDT", "ETHUSDT"],
    "forex": ["DXY"],
    "commodity": ["DXY"],
    "index": ["SPX"],
    "stock": ["SPY"],
    "stock_in": ["NIFTY"],  # Indian listings (.NS / .BO)
}
STANDARD_LOTS = {"forex": 100_000, "XAUUSD": 100, "XAGUSD": 5_000}


# =============================================================================
# Result types
# =============================================================================

@dataclass
class Target:
    name: str             # TP1 | TP2 | TP3
    price: float
    rr: float
    source: str           # e.g. "15m swing high", "PDH", "1D bearish OB"
    timeframe: str = ""


@dataclass
class Position:
    account_size: float
    account_currency: str
    risk_pct: float
    risk_amount: float
    units: float
    unit_name: str
    notional: float                   # in quote currency
    lots: float | None = None
    lot_name: str | None = None
    quote_to_account: float = 1.0     # multiply quote-currency amounts by this to get account currency
    notes: list[str] = field(default_factory=list)


@dataclass
class OppositeScenario:
    trigger_level: float
    trigger_rule: str
    direction: int
    poi: Zone | None
    entry: float | None
    stop_loss: float | None
    target: float | None
    target_source: str | None
    rr: float | None
    valid: bool
    note: str = ""

    _json_extra = ("side",)

    @property
    def side(self) -> str:
        return "long" if self.direction == BULL else "short"


@dataclass
class Hedge:
    status: str                       # suggested | suppressed | unavailable
    instrument: str | None = None
    direction: str | None = None      # long | short (the hedge leg)
    correlation: float | None = None
    beta: float | None = None
    ratio: float | None = None        # hedge notional per 1 unit of position notional (|beta|)
    effectiveness: float | None = None  # corr^2: share of variance the hedge offsets
    observations: int = 0
    hedge_notional: float | None = None
    hedge_units: float | None = None
    partner_price: float | None = None
    reason: str = ""
    candidates: list[dict] = field(default_factory=list)


@dataclass
class TradePlan:
    symbol: str
    asset_class: str
    status: str                       # valid | no_valid_setup | no_setup
    direction: int
    price: float
    generated_at: str
    cascade_status: str
    confluence: int
    reason: str = ""
    waiting_for: str | None = None
    entry: float | None = None
    entry_mode: str | None = None
    entry_type: str | None = None     # limit (wait for retest) | market (price already at/inside zone)
    stop_loss: float | None = None
    risk_per_unit: float | None = None
    targets: list[Target] = field(default_factory=list)
    primary_target: str | None = None
    best_rr: float | None = None
    min_rr: float | None = None
    invalidation_level: float | None = None
    entry_zone: Zone | None = None
    entry_timeframe: str | None = None
    tick_size: float | None = None
    position: Position | None = None
    opposite: OppositeScenario | None = None
    hedge: Hedge | None = None
    notes: list[str] = field(default_factory=list)

    _json_extra = ("side",)

    @property
    def side(self) -> str | None:
        return {BULL: "long", BEAR: "short"}.get(self.direction)

    @property
    def is_valid(self) -> bool:
        return self.status == "valid"

    def to_dict(self) -> dict:
        return jsonable(self)


# =============================================================================
# Helpers
# =============================================================================

def _round(inst, x: float | None) -> float | None:
    return None if x is None else data.round_to_tick(float(x), inst.tick_size)


def _beyond(price: float, ref: float, bias: int) -> bool:
    """``price`` lies beyond ``ref`` in the trade direction (above for longs, below for shorts)."""
    return price > ref if bias == BULL else price < ref


def _rr(entry: float, sl: float, target: float, bias: int) -> float:
    risk = (entry - sl) * bias
    return 0.0 if risk <= 0 else (target - entry) * bias / risk


def _confirmation_atr(analyses: dict[str, TimeframeAnalysis], cfg: dict) -> float:
    ctf = cfg["structure"].get("confirmation_timeframe", "15m")
    for tf in (ctf, "1H", "5m", "4H"):
        a = analyses.get(tf)
        if a is not None and a.atr:
            return float(a.atr)
    return 0.0


def entry_price(zone: Zone, bias: int, mode: str) -> float:
    if mode == "ob_50":
        return zone.mid
    return zone.top if bias == BULL else zone.bottom  # proximal edge


def _level_candidates(analyses: dict[str, TimeframeAnalysis], tfs: tuple[str, ...], bias: int,
                      kinds: tuple[str, ...] | None = None) -> list[tuple[float, str, str]]:
    """Resting pools on the target side, as (price, source label, timeframe)."""
    side = "buy" if bias == BULL else "sell"
    out = []
    for tf in tfs:
        a = analyses.get(tf)
        if a is None:
            continue
        for lv in a.liquidity.resting(side):
            if kinds is None or lv.kind in kinds:
                out.append((lv.price, f"{tf} {lv.label()}", tf))
    return out


def _nearest_beyond(cands: list[tuple[float, str, str]], ref: float, bias: int) -> tuple[float, str, str] | None:
    beyond = [c for c in cands if _beyond(c[0], ref, bias)]
    return min(beyond, key=lambda c: abs(c[0] - ref)) if beyond else None


def select_targets(res: CascadeResult, analyses: dict[str, TimeframeAnalysis], entry: float,
                   bias: int, cfg: dict) -> list[tuple[str, float, str, str]]:
    """TP1 (nearest opposing liquidity), TP2 (next swing), TP3 (external DOL / HTF POI)."""
    ctf = cfg["structure"].get("confirmation_timeframe", "15m")
    tp1_c = [(t["level"].price, f"{t['level'].label()} (internal)", ctf) for t in res.targets
             if t["role"] == "internal"] + _level_candidates(analyses, (ctf, "5m"), bias)
    tp2_c = _level_candidates(analyses, ("1H", "4H"), bias, kinds=("swing_high", "swing_low"))
    tp3_c = [(t["level"].price, f"{t['level'].label()} (external DOL)", "1D") for t in res.targets
             if t["role"] == "external"]
    for tf in ("1D", "1W"):
        a = analyses.get(tf)
        if a is not None:
            for z in a.zones.select(direction=-bias):  # opposing HTF POI: where longs take profit
                tp3_c.append((z.proximal, f"{z.label()} (opposing HTF POI)", tf))

    out: list[tuple[str, float, str, str]] = []
    ref = entry
    for name, cands in (("TP1", tp1_c), ("TP2", tp2_c), ("TP3", tp3_c)):
        pick = _nearest_beyond(cands, ref, bias)
        if pick is None and name == "TP1":  # no LTF pool: promote the next swing to TP1
            pick = _nearest_beyond(tp2_c, ref, bias)
        if pick is not None:
            out.append((name, *pick))
            ref = pick[0]
    return out


# =============================================================================
# Position sizing
# =============================================================================

def _split_pair(symbol: str) -> tuple[str, str] | None:
    return (symbol[:3], symbol[3:6]) if len(symbol) == 6 and symbol.isalpha() else None


def default_fx_rate(base: str, quote: str) -> float | None:
    """Latest daily close of ``base``/``quote`` from Yahoo (None if unavailable)."""
    try:
        df = data.get_ohlcv(f"{base}{quote}", "1D", 5)
        return float(df["close"].iloc[-1])
    except (data.DataError, ValueError) as exc:
        log.warning("fx rate %s%s unavailable: %s", base, quote, exc)
        return None


def quote_to_account_rate(quote: str, account_ccy: str, price: float, base: str | None,
                          fx_rate: Callable[[str, str], float | None]) -> float | None:
    """Multiplier converting an amount in ``quote`` currency into ``account_ccy``."""
    if quote == account_ccy:
        return 1.0
    if base == account_ccy and price:
        return 1.0 / price                      # e.g. USDJPY: JPY -> USD = 1 / price
    r = fx_rate(account_ccy, quote)             # e.g. USDJPY rate for a GBPJPY trade
    if r:
        return 1.0 / r
    r = fx_rate(quote, account_ccy)
    return r if r else None


def position_size(inst, entry: float, stop: float, account: float, risk_pct: float,
                  account_ccy: str = "USD", fx_rate: Callable[[str, str], float | None] | None = None
                  ) -> Position:
    """Units so that hitting the stop loses ``risk_pct`` % of ``account``.

    Units are the instrument's natural unit: coins (crypto), shares (stocks), base-currency
    units plus standard lots (forex), ounces plus lots (gold/silver), or per-point units (indices).
    """
    fx_rate = fx_rate or default_fx_rate
    risk_amount = account * risk_pct / 100.0
    per_unit_quote = abs(entry - stop)
    notes: list[str] = []
    quote, base, coin = "USD", None, None
    if inst.asset_class == "forex":
        base, quote = _split_pair(inst.symbol)
    elif inst.asset_class == "crypto":
        stable = next((q for q in ("FDUSD", "USDT", "USDC", "BUSD", "TUSD") if inst.symbol.endswith(q)), None)
        if stable:
            coin = inst.symbol[: -len(stable)]
            notes.append(f"{stable} treated as USD (stablecoin).")
        else:  # crypto-quoted pair such as ETHBTC: risk is in the quote coin
            coin, quote = inst.symbol[:-3], inst.symbol[-3:]
    elif inst.asset_class == "stock" and inst.session_tz == "Asia/Kolkata":
        quote = "INR"
    conv = quote_to_account_rate(quote, account_ccy, entry, base, fx_rate)
    if conv is None:
        conv = 1.0
        notes.append(f"No {quote}->{account_ccy} rate available; risk is computed in {quote}.")
    per_unit_acct = per_unit_quote * conv
    units = risk_amount / per_unit_acct if per_unit_acct > 0 else 0.0

    unit_name, lots, lot_name = "units", None, None
    if inst.asset_class == "crypto":
        units = math.floor(units * 1e6) / 1e6
        unit_name = coin or "coins"
    elif inst.asset_class == "stock":
        units = float(math.floor(units))
        unit_name = "shares"
        if units == 0:
            notes.append("Risk budget is smaller than the risk of one share (consider fractional shares).")
    elif inst.asset_class == "forex":
        unit_name = f"{base} units"
        lots, lot_name = units / STANDARD_LOTS["forex"], "standard lots (100k)"
    elif inst.symbol in ("XAUUSD", "XAGUSD", "GOLD", "SILVER"):
        key = "XAUUSD" if inst.symbol in ("XAUUSD", "GOLD") else "XAGUSD"
        unit_name = "oz"
        lots, lot_name = units / STANDARD_LOTS[key], f"lots ({STANDARD_LOTS[key]:,} oz)"
        notes.append("Priced from the futures proxy; broker spot may differ slightly.")
    elif inst.asset_class == "index":
        unit_name = "units per point (CFD)"
    notional_acct = units * entry * conv
    if account and notional_acct > account:
        notes.append(f"Exposure {notional_acct:,.0f} {account_ccy} is {notional_acct / account:.1f}x the account: "
                     f"requires leverage/margin.")
    actual_risk = units * per_unit_acct
    if units > 0 and abs(actual_risk - risk_amount) / risk_amount > 0.05:
        notes.append(f"Rounded size risks {actual_risk:.2f} {account_ccy} instead of {risk_amount:.2f}.")
    return Position(account, account_ccy, risk_pct, round(risk_amount, 2), units, unit_name,
                    round(units * entry, 2), None if lots is None else round(lots, 4), lot_name,
                    conv, notes)


# =============================================================================
# Opposite-bias scenario
# =============================================================================

def opposite_scenario(res: CascadeResult, analyses: dict[str, TimeframeAnalysis], inst, cfg: dict,
                      plan: "TradePlan") -> OppositeScenario:
    """The counter setup if price closes through the plan's invalidation level.

    Flipped bias. The POI is the nearest opposing zone (1H/15m/4H) beyond the trigger, inside
    the move back toward the plan's entry; otherwise the failed entry zone acts as a flip zone
    (breaker). Entry at its proximal edge, SL beyond its distal edge plus the ATR buffer, target
    at the nearest resting liquidity beyond the trigger.
    """
    bias = plan.direction
    flip = -bias
    trigger = plan.invalidation_level
    buf = cfg["trade"]["sl_atr_buffer"] * _confirmation_atr(analyses, cfg)
    ctf = cfg["structure"].get("confirmation_timeframe", "15m")
    rule = f"a {ctf} candle closes {'below' if bias == BULL else 'above'} {fmt_price(trigger, inst.tick_size)}"

    zones = []
    for tf in (ctf, "1H", "4H"):
        a = analyses.get(tf)
        if a is not None:
            zones += [z for z in a.zones.select(direction=flip)
                      if (z.bottom >= trigger if flip == BEAR else z.top <= trigger)
                      and (z.bottom <= plan.entry if flip == BEAR else z.top >= plan.entry)]
    if zones:
        poi = min(zones, key=lambda z: abs(z.proximal - trigger))
        note = f"Retest of {poi.label()} after the break."
    else:
        ez = plan.entry_zone
        poi = replace(ez, direction=flip, source=f"failed {ez.label()} (flip zone)")
        note = "No opposing zone nearby; the failed entry zone is expected to flip (breaker)."
    entry = _round(inst, poi.proximal)
    stop = _round(inst, poi.distal - buf if flip == BULL else poi.distal + buf)

    side = "buy" if flip == BULL else "sell"
    pools = []
    for tf in (ctf, "1H", "4H", "1D"):
        a = analyses.get(tf)
        if a is not None:
            pools += [(lv.price, f"{tf} {lv.label()}") for lv in a.liquidity.resting(side)
                      if _beyond(lv.price, trigger, flip)]
    tgt = min(pools, key=lambda p: abs(p[0] - trigger)) if pools else None
    target, src = (_round(inst, tgt[0]), tgt[1]) if tgt else (None, None)
    rr = _rr(entry, stop, target, flip) if target is not None else None
    min_rr = float(cfg["trade"]["min_rr"])
    valid = rr is not None and rr >= min_rr
    if rr is not None and not valid:
        note += f" R:R {rr:.2f} is below the {min_rr:g} minimum: treat as information only."
    return OppositeScenario(trigger, rule, flip, poi, entry, stop, target, src,
                            None if rr is None else round(rr, 2), valid, note)


# =============================================================================
# Correlated hedge
# =============================================================================

def hedge_candidates(inst) -> list[str]:
    if inst.symbol in CORRELATION_MAP:
        return list(CORRELATION_MAP[inst.symbol])
    if inst.asset_class == "stock" and inst.session_tz == "Asia/Kolkata":
        return list(DEFAULT_PARTNERS["stock_in"])
    defaults = [p for p in DEFAULT_PARTNERS.get(inst.asset_class, []) if p != inst.symbol]
    return defaults


def default_daily_closes(symbol: str, days: int) -> pd.Series:
    return data.get_ohlcv(symbol, "1D", days)["close"]


def correlated_hedge(inst, own_close: pd.Series, bias: int, cfg: dict,
                     fetch: Callable[[str, int], pd.Series] | None = None,
                     notional: float | None = None) -> Hedge:
    """Pick the best-correlated partner and size a beta-weighted hedge leg.

    Hedging a long in A with partner B: positive correlation → short B; negative → long B.
    Hedge notional = |beta| × position notional (minimum-variance hedge ratio).
    """
    fetch = fetch or default_daily_closes
    hc = cfg["hedge"]
    window, min_corr = int(hc["correlation_window_days"]), float(hc["min_abs_correlation"])
    days = int(hc.get("history_days", 120))
    cands = hedge_candidates(inst)
    if not cands:
        return Hedge("unavailable", reason=f"No hedge partner mapped for {inst.symbol}.")
    rows, closes = [], {}
    for sym in cands:
        try:
            partner = fetch(sym, days)
        except (data.DataError, ValueError) as exc:
            rows.append({"symbol": sym, "error": str(exc)})
            continue
        stats = correlation_and_beta(own_close, partner, window)
        closes[sym] = partner
        rows.append({"symbol": sym, **stats})
    usable = [r for r in rows if r.get("correlation") is not None]
    if not usable:
        return Hedge("unavailable", reason="Not enough overlapping daily data for any hedge partner.",
                     candidates=rows)
    best = max(usable, key=lambda r: abs(r["correlation"]))
    corr, beta = best["correlation"], best["beta"]
    if abs(corr) < min_corr:
        return Hedge("suppressed", instrument=best["symbol"], correlation=round(corr, 3), beta=round(beta, 3),
                     observations=best["observations"], candidates=rows,
                     reason=f"Best partner {best['symbol']} has |corr| {abs(corr):.2f} < {min_corr:g} "
                            f"over {window} days; a hedge would not reliably offset risk.")
    # long A + positive corr -> short B; flip for negative corr or a short position
    hedge_dir = -bias if corr > 0 else bias
    ratio = abs(beta)
    partner_price = float(closes[best["symbol"]].iloc[-1])
    h_notional = None if notional is None else round(ratio * notional, 2)
    h_units = None if h_notional is None or not partner_price else round(h_notional / partner_price, 6)
    return Hedge("suggested", best["symbol"], "long" if hedge_dir == BULL else "short", round(corr, 3),
                 round(beta, 3), round(ratio, 3), round(corr * corr, 3), best["observations"], h_notional,
                 h_units, partner_price,
                 f"{best['symbol']} {'moves with' if corr > 0 else 'moves against'} {inst.symbol} "
                 f"(corr {corr:+.2f}, beta {beta:.2f}, {window}d); the hedge offsets ~{corr * corr:.0%} of variance.",
                 rows)


# =============================================================================
# Plan builder
# =============================================================================

def build_trade_plan(res: CascadeResult, analyses: dict[str, TimeframeAnalysis], inst, cfg: dict | None = None,
                     account: float | None = None, risk_pct: float | None = None, with_hedge: bool = True,
                     hedge_fetch: Callable[[str, int], pd.Series] | None = None,
                     fx_rate: Callable[[str, str], float | None] | None = None) -> TradePlan:
    cfg = cfg or get_config()
    bias = res.bias
    plan = TradePlan(symbol=res.symbol, asset_class=res.asset_class, status="no_setup", direction=bias,
                     price=res.price, generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     cascade_status=res.status, confluence=int(res.confluence.get("score", 0)),
                     waiting_for=res.waiting_for, tick_size=getattr(inst, "tick_size", None),
                     min_rr=float(cfg["trade"]["min_rr"]))
    if res.status != "ready" or res.entry_zone is None or res.invalidation_level is None:
        plan.reason = (f"No confirmed setup (cascade status: {res.status})."
                       + (f" Waiting for {res.waiting_for}." if res.waiting_for else ""))
        return plan

    zone = res.entry_zone
    mode = cfg["trade"].get("entry_mode", "ob_edge")
    entry = _round(inst, entry_price(zone, bias, mode))
    buf = float(cfg["trade"]["sl_atr_buffer"]) * _confirmation_atr(analyses, cfg)
    if bias == BULL:
        stop = _round(inst, min(zone.bottom, res.invalidation_level) - buf)
    else:
        stop = _round(inst, max(zone.top, res.invalidation_level) + buf)
    plan.entry, plan.entry_mode, plan.stop_loss = entry, mode, stop
    plan.entry_zone, plan.entry_timeframe = zone, zone.timeframe
    plan.invalidation_level = _round(inst, res.invalidation_level)
    plan.entry_type = "limit" if _beyond(res.price, entry, bias) else "market"
    risk = (entry - stop) * bias
    if risk <= 0:
        plan.status, plan.reason = "no_valid_setup", "Stop loss is not beyond the entry (degenerate zone)."
        return plan
    plan.risk_per_unit = _round(inst, risk)

    for name, price, src, tf in select_targets(res, analyses, entry, bias, cfg):
        price = _round(inst, price)
        plan.targets.append(Target(name, price, round(_rr(entry, stop, price, bias), 2), src, tf))
    if not plan.targets:
        plan.status, plan.reason = "no_valid_setup", "No liquidity target beyond the entry."
        return plan
    plan.best_rr = max(t.rr for t in plan.targets)
    primary = next((t for t in plan.targets if t.rr >= plan.min_rr), None)
    if primary is None:
        plan.status = "no_valid_setup"
        plan.reason = (f"Best R:R {plan.best_rr:.2f} is below the {plan.min_rr:g} minimum "
                       f"(entry {fmt_price(entry, inst.tick_size)}, SL {fmt_price(stop, inst.tick_size)}).")
    else:
        plan.status, plan.primary_target = "valid", primary.name
        plan.reason = (f"{primary.name} at {fmt_price(primary.price, inst.tick_size)} gives {primary.rr:.2f}R "
                       f"(minimum {plan.min_rr:g}R).")
    tp1 = plan.targets[0]
    if not _beyond(tp1.price, res.price, bias):
        # Price already trades at/through TP1: the move this setup was built for has happened.
        plan.status, plan.primary_target = "no_valid_setup", None
        plan.reason = (f"Price {fmt_price(res.price, inst.tick_size)} has already reached TP1 "
                       f"{fmt_price(tp1.price, inst.tick_size)}: the move has played out. Wait for a new setup.")
    if plan.entry_type == "market":
        plan.notes.append("Price is already at/inside the entry zone: entry is at market.")
    else:
        tp1 = plan.targets[0].price
        span = (tp1 - entry) * bias
        done = (res.price - entry) * bias / span if span > 0 else 0.0
        if done >= 0.5:
            plan.notes.append(f"Price has already covered {min(done, 1):.0%} of the way from entry to TP1; "
                              f"the limit entry needs a retrace to {fmt_price(entry, inst.tick_size)}.")

    account = account if account is not None else cfg["risk"].get("account_size")
    risk_pct = float(risk_pct if risk_pct is not None else cfg["risk"].get("risk_pct", 1.0))
    if account:
        plan.position = position_size(inst, entry, stop, float(account), risk_pct,
                                      cfg["risk"].get("account_currency", "USD"), fx_rate)
    else:
        plan.notes.append("Pass --account (and optionally --risk-pct) to size the position.")

    plan.opposite = opposite_scenario(res, analyses, inst, cfg, plan)
    if with_hedge:
        daily = analyses.get("1D")
        if daily is None or daily.df.empty:
            plan.hedge = Hedge("unavailable", reason="No daily data for the traded symbol.")
        else:
            notional = plan.position.notional * plan.position.quote_to_account if plan.position else None
            plan.hedge = correlated_hedge(inst, daily.df["close"], bias, cfg, hedge_fetch, notional)
    return plan


# =============================================================================
# CLI
# =============================================================================

def format_plan(plan: TradePlan) -> str:
    p = lambda x: fmt_price(x, plan.tick_size)  # noqa: E731
    lines = [f"TRADE PLAN {plan.symbol} | {plan.status.upper()} | cascade {plan.cascade_status} | "
             f"confluence {plan.confluence}/100", f"  {plan.reason}"]
    if plan.entry is not None:
        lines += [f"  Direction : {plan.side.upper()} ({plan.entry_type} entry, mode {plan.entry_mode})",
                  f"  Entry     : {p(plan.entry)}   zone {p(plan.entry_zone.bottom)}-{p(plan.entry_zone.top)} "
                  f"({plan.entry_zone.label()})",
                  f"  Stop loss : {p(plan.stop_loss)}   (invalidation {p(plan.invalidation_level)}, risk "
                  f"{p(plan.risk_per_unit)}/unit)"]
        for t in plan.targets:
            star = " *" if t.name == plan.primary_target else ""
            lines.append(f"  {t.name}       : {p(t.price)}   {t.rr:.2f}R   {t.source}{star}")
    if plan.position:
        pos = plan.position
        lots = f" = {pos.lots} {pos.lot_name}" if pos.lots is not None else ""
        lines.append(f"  Size      : {pos.units:g} {pos.unit_name}{lots} (risk {pos.risk_amount} "
                     f"{pos.account_currency} = {pos.risk_pct:g}% of {pos.account_size:g})")
        lines += [f"              - {n}" for n in pos.notes]
    if plan.opposite:
        o = plan.opposite
        lines.append(f"  Opposite  : if {o.trigger_rule} -> {o.side.upper()} at {p(o.entry)}, SL {p(o.stop_loss)}, "
                     f"TP {p(o.target)} ({o.rr}R{'' if o.valid else ', info only'}). {o.note}")
    if plan.hedge:
        h = plan.hedge
        if h.status == "suggested":
            size = (f" = {h.hedge_units:g} units (~{h.hedge_notional:,.0f} notional)"
                    if h.hedge_units is not None else "")
            lines.append(f"  Hedge     : {h.direction.upper()} {h.instrument} at ratio {h.ratio} x notional{size}. "
                         f"{h.reason}")
        else:
            lines.append(f"  Hedge     : {h.status} - {h.reason}")
    lines += [f"  Note      : {n}" for n in plan.notes]
    lines.append("Educational analysis only - not financial advice.")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    from .mtf import analyze_symbol, format_report

    ap = argparse.ArgumentParser(prog="python -m smc.signal", description="SMC trade plan for a symbol.")
    ap.add_argument("symbol")
    ap.add_argument("--account", type=float, default=None, help="account size for position sizing")
    ap.add_argument("--risk-pct", type=float, default=None, help="percent of the account risked (default 1)")
    ap.add_argument("--no-hedge", action="store_true", help="skip the correlated-hedge lookup")
    ap.add_argument("--track", action="store_true", help="add a valid plan to the signal journal")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        res, analyses = analyze_symbol(args.symbol)
        inst = data.resolve_symbol(args.symbol)
        info = next(iter(analyses.values())).df.attrs.get("instrument", {})
        if info.get("tick_size"):  # exact exchange tick (e.g. Binance) found during the fetch
            inst = replace(inst, tick_size=info["tick_size"])
        plan = build_trade_plan(res, analyses, inst, account=args.account, risk_pct=args.risk_pct,
                                with_hedge=not args.no_hedge)
    except (data.DataError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}))
        return 1
    tracked = None
    if args.track and plan.is_valid:
        from .tracker import Journal
        tracked = Journal().add_plan(plan, inst)
    if args.json:
        out = {"cascade": res.to_dict(), "plan": plan.to_dict()}
        if tracked is not None:
            out["tracked_signal"] = tracked.to_dict()
        print(json.dumps(out))
    else:
        print(format_report(res).rsplit("\n", 1)[0])
        print()
        print(format_plan(plan))
        if tracked is not None:
            print(f"Tracking signal {tracked.id} ({tracked.status}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
