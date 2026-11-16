"""Live signal tracking: a state machine per signal, persisted in a JSON journal.

States
    pending           waiting for price to reach the entry
    triggered         entry filled
    tp1_hit, tp2_hit  partial targets reached (still open)
    tp3_hit           final target reached (closed). Targets keep their plan names, so with only
                      TP1 and TP3 the statuses are tp1_hit then tp3_hit; the last target closes it.
    sl_hit            stopped out before TP1 (closed, loss, -1R)
    stopped_breakeven stopped at entry after at least TP1 (closed, win)
    expired           never filled within ``tracker.signal_expiry_hours``, or TP1 traded before the
                      entry was filled (the move left without us). Excluded from hit rate.

Trade-management model (so outcomes are honest, not best-case)
    * The position is split equally across the n targets and 1/n is closed at each TP.
    * After TP1 the stop moves to breakeven (the entry price).
    * Realised R = sum of rr_k / n over the targets hit, + the remaining fraction × (-1 before
      TP1, 0 after TP1).
    * Same-bar ambiguity is resolved conservatively: a candle that touches the stop and a target
      counts as the stop. On the fill bar only the stop is checked.

Catch-up
    Each signal remembers the last *closed* candle it processed. ``Journal.refresh()`` fetches
    the candles since then (tracking timeframe ``tracker.timeframe``, default 5m) and replays
    them, so outcomes stay correct even if nothing was running in between. The still-forming
    candle is also checked (its high and low really traded) but never marked as processed.

CLI (run from the scripts/ directory):
    python -m smc.tracker              # show journal + stats
    python -m smc.tracker --refresh    # catch up open signals first
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import data
from .common import BULL, fmt_price
from .config import SKILL_DIR, get_config

log = logging.getLogger("smc.tracker")

OPEN_STATES = ("pending", "triggered", "tp1_hit", "tp2_hit")
FINAL_STATES = ("tp3_hit", "sl_hit", "stopped_breakeven", "expired")


def _ts(x: str | None) -> pd.Timestamp | None:
    return None if x is None else pd.Timestamp(x).tz_convert("UTC") if pd.Timestamp(x).tzinfo \
        else pd.Timestamp(x).tz_localize("UTC")


def _iso(t) -> str:
    return pd.Timestamp(t).tz_convert("UTC").isoformat() if pd.Timestamp(t).tzinfo \
        else pd.Timestamp(t).tz_localize("UTC").isoformat()


@dataclass
class TrackedSignal:
    id: str
    symbol: str
    asset_class: str
    direction: int
    entry: float
    stop_loss: float
    targets: list[dict]               # [{"name": "TP1", "price": .., "rr": ..}]
    tick_size: float
    timeframe: str
    created_at: str
    expires_at: str
    status: str = "pending"
    triggered_at: str | None = None
    closed_at: str | None = None
    targets_hit: int = 0
    result: str | None = None         # win | loss | expired | None (open)
    r_multiple: float | None = None
    last_processed: str | None = None # open time of the last fully processed (closed) candle
    events: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        # Openness comes from the result, not the status name: with only two targets, a final
        # "tp2_hit" is closed even though "tp2_hit" is an open state for three-target plans.
        return self.result is None

    @property
    def side(self) -> str:
        return "long" if self.direction == BULL else "short"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side
        return d

    @classmethod
    def from_dict(cls, d: dict) -> TrackedSignal:
        names = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in names})

    # --- state machine --------------------------------------------------------------

    def _event(self, t, kind: str, price: float | None, note: str = "") -> None:
        self.events.append({"time": _iso(t), "event": kind, "price": price, "note": note})

    def _realised_r(self, stopped: bool) -> float:
        n = len(self.targets)
        hit = self.targets[: self.targets_hit]
        r = sum(t["rr"] for t in hit) / n
        if stopped:
            r += (n - self.targets_hit) / n * (0.0 if self.targets_hit else -1.0)
        return round(r, 3)

    def _close(self, t, status: str, result: str, r: float | None) -> None:
        self.status, self.result, self.r_multiple, self.closed_at = status, result, r, _iso(t)

    def process(self, df: pd.DataFrame, tf: str | None = None, now: datetime | None = None) -> bool:
        """Replay candles (open time > last_processed). Returns True if anything changed."""
        if not self.is_open or df.empty:
            return False
        tf = tf or self.timeframe
        d = self.direction
        created = _ts(self.created_at)
        expires = _ts(self.expires_at)
        last = _ts(self.last_processed)
        before = (self.status, self.targets_hit, len(self.events), self.last_processed)
        for t, bar in df.iterrows():
            if t < created or (last is not None and t <= last):
                continue
            hi, lo = float(bar["high"]), float(bar["low"])
            # adverse / favourable side of this candle (values bound now: evaluated in this iteration)
            touches = (lambda px, lo=lo: lo <= px) if d == BULL else (lambda px, hi=hi: hi >= px)
            reaches = (lambda px, hi=hi: hi >= px) if d == BULL else (lambda px, lo=lo: lo <= px)

            if self.status == "pending":
                if t >= expires:
                    self._event(t, "expired", None, "entry not filled before expiry")
                    self._close(t, "expired", "expired", None)
                    break
                if touches(self.entry):
                    self.status, self.triggered_at = "triggered", _iso(t)
                    self._event(t, "triggered", self.entry)
                    if touches(self.stop_loss):
                        self._event(t, "sl_hit", self.stop_loss, "stop touched on the fill candle")
                        self._close(t, "sl_hit", "loss", -1.0)
                        break
                elif reaches(self.targets[0]["price"]):
                    self._event(t, "expired", None, "TP1 traded before the entry was filled (missed)")
                    self._close(t, "expired", "expired", None)
                    break
            else:
                stop = self.entry if self.targets_hit else self.stop_loss
                if touches(stop):
                    if self.targets_hit:
                        self._event(t, "stopped_breakeven", stop)
                        self._close(t, "stopped_breakeven", "win", self._realised_r(True))
                    else:
                        self._event(t, "sl_hit", stop)
                        self._close(t, "sl_hit", "loss", self._realised_r(True))
                    break
                while self.targets_hit < len(self.targets) and reaches(self.targets[self.targets_hit]["price"]):
                    tgt = self.targets[self.targets_hit]
                    self.targets_hit += 1
                    self.status = f"{tgt['name'].lower()}_hit"   # targets keep semantic names (TP1/TP3...)
                    self._event(t, self.status, tgt["price"])
                if self.targets_hit == len(self.targets):
                    self._close(t, self.status, "win", self._realised_r(False))  # last target closes it
                    break
            if data.is_bar_closed(t, tf, now):
                self.last_processed = _iso(t)
        # a pending signal can also expire while no candles arrive (e.g. market closed)
        now_ts = pd.Timestamp(now or datetime.now(UTC))
        if self.status == "pending" and now_ts >= expires:
            self._event(expires, "expired", None, "entry not filled before expiry")
            self._close(expires, "expired", "expired", None)
        return before != (self.status, self.targets_hit, len(self.events), self.last_processed)


# =============================================================================
# Journal
# =============================================================================

def journal_path(cfg: dict | None = None) -> Path:
    override = os.environ.get("SMC_ANALYST_JOURNAL")
    if override:
        return Path(override).expanduser().resolve()
    cfg = cfg or get_config()
    p = Path(cfg["tracker"].get("journal_path", "journal.json")).expanduser()
    return p if p.is_absolute() else SKILL_DIR / p


def signal_id(symbol: str, direction: int, entry: float, stop: float) -> str:
    raw = f"{symbol}|{direction}|{entry:.10g}|{stop:.10g}"
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def default_fetch(symbol: str, tf: str, limit: int) -> pd.DataFrame:
    return data.get_ohlcv(symbol, tf, limit, use_cache=False)


class Journal:
    def __init__(self, path: Path | str | None = None, cfg: dict | None = None):
        self.cfg = cfg or get_config()
        self.path = Path(path) if path else journal_path(self.cfg)
        self.signals: list[TrackedSignal] = self._load()

    def _load(self) -> list[TrackedSignal]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            log.warning("journal %s unreadable (%s); starting empty", self.path, exc)
            return []
        return [TrackedSignal.from_dict(d) for d in raw.get("signals", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "signals": [s.to_dict() for s in self.signals]}, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, sid: str) -> TrackedSignal | None:
        return next((s for s in self.signals if s.id == sid), None)

    def add_plan(self, plan, inst=None, now: datetime | None = None, save: bool = True) -> TrackedSignal:
        """Start tracking a *valid* trade plan. Re-adding the same open plan returns the existing signal."""
        if not getattr(plan, "is_valid", False):
            raise ValueError(f"Only valid plans can be tracked (status: {plan.status}).")
        sid = signal_id(plan.symbol, plan.direction, plan.entry, plan.stop_loss)
        existing = self.get(sid)
        if existing is not None and existing.is_open:
            return existing
        now = pd.Timestamp(now or datetime.now(UTC))
        now = now.tz_localize("UTC") if now.tzinfo is None else now
        hours = float(self.cfg["tracker"]["signal_expiry_hours"])
        sig = TrackedSignal(
            id=sid, symbol=plan.symbol, asset_class=plan.asset_class, direction=plan.direction,
            entry=plan.entry, stop_loss=plan.stop_loss,
            targets=[{"name": t.name, "price": t.price, "rr": t.rr} for t in plan.targets],
            tick_size=plan.tick_size or getattr(inst, "tick_size", 0.01),
            timeframe=self.cfg["tracker"].get("timeframe", "5m"),
            created_at=_iso(now), expires_at=_iso(now + timedelta(hours=hours)),
            meta={"confluence": plan.confluence, "primary_target": plan.primary_target,
                  "entry_zone": None if plan.entry_zone is None else
                  {"top": plan.entry_zone.top, "bottom": plan.entry_zone.bottom, "label": plan.entry_zone.label()}},
        )
        sig._event(now, "created", plan.entry, f"{sig.side} {plan.symbol}")
        if existing is not None:  # closed signal with identical levels: keep history, new id suffix
            sig.id = f"{sid}-{len([s for s in self.signals if s.id.startswith(sid)])}"
        self.signals.append(sig)
        if save:
            self.save()
        return sig

    def bars_needed(self, sig: TrackedSignal, now: datetime | None = None) -> int:
        start = _ts(sig.last_processed) or _ts(sig.created_at)
        now_ts = pd.Timestamp(now or datetime.now(UTC))
        span = (now_ts - start).total_seconds()
        n = int(math.ceil(span / data.TF_SECONDS[data.normalize_timeframe(sig.timeframe)])) + 2
        return max(2, min(n, int(self.cfg["tracker"].get("max_catchup_bars", 5000))))

    def refresh(self, fetch: Callable[[str, str, int], pd.DataFrame] | None = None,
                now: datetime | None = None, save: bool = True) -> list[TrackedSignal]:
        """Catch up every open signal; returns those that changed."""
        fetch = fetch or default_fetch
        changed = []
        for sig in self.signals:
            if not sig.is_open:
                continue
            try:
                df = fetch(sig.symbol, sig.timeframe, self.bars_needed(sig, now))
            except (data.DataError, ValueError) as exc:
                log.warning("cannot refresh %s: %s", sig.id, exc)
                continue
            if sig.process(df, sig.timeframe, now):
                changed.append(sig)
        if save and changed:
            self.save()
        return changed

    def update_with_candle(self, symbol: str, tf: str, candle: dict,
                           now: datetime | None = None) -> list[TrackedSignal]:
        """Feed one live candle event (from ``feeds.stream``) to the matching open signals."""
        df = pd.DataFrame([{k: candle[k] for k in ("open", "high", "low", "close", "volume")}],
                          index=pd.DatetimeIndex([pd.Timestamp(candle["time"], unit="s", tz="UTC")], name="time"))
        out = [s for s in self.signals if s.is_open and s.symbol == symbol and s.timeframe == tf
               and s.process(df, tf, now)]
        if out:
            self.save()
        return out

    def stats(self) -> dict:
        closed = [s for s in self.signals if s.result in ("win", "loss")]
        wins = [s for s in closed if s.result == "win"]
        rs = [s.r_multiple for s in closed if s.r_multiple is not None]
        return {
            "total": len(self.signals),
            "open": sum(s.is_open for s in self.signals),
            "pending": sum(s.status == "pending" for s in self.signals),
            "closed": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "expired": sum(s.result == "expired" for s in self.signals),
            "hit_rate": round(len(wins) / len(closed), 3) if closed else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "total_r": round(sum(rs), 3) if rs else 0.0,
        }


# =============================================================================
# CLI
# =============================================================================

def format_journal(j: Journal) -> str:
    lines = [f"Journal: {j.path}"]
    if not j.signals:
        lines.append("  (no tracked signals yet - use `python -m smc.signal SYMBOL --track`)")
    for s in j.signals:
        p = lambda x, tick=s.tick_size: fmt_price(x, tick)
        tps = " / ".join(p(t["price"]) for t in s.targets)
        r = "" if s.r_multiple is None else f"  {s.r_multiple:+.2f}R"
        lines.append(f"  {s.id}  {s.symbol:<10} {s.side:<5} {s.status:<17} entry {p(s.entry)}  SL {p(s.stop_loss)}"
                     f"  TP {tps}{r}  ({s.created_at[:16]})")
    st = j.stats()
    hr = "n/a" if st["hit_rate"] is None else f"{st['hit_rate']:.0%}"
    ar = "n/a" if st["avg_r"] is None else f"{st['avg_r']:+.2f}R"
    lines.append(f"Stats: {st['total']} signals, {st['open']} open, {st['closed']} closed "
                 f"({st['wins']}W/{st['losses']}L), {st['expired']} expired | hit rate {hr} | avg {ar} | "
                 f"total {st['total_r']:+.2f}R")
    return "\n".join(lines)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m smc.tracker", description="Signal journal and stats.")
    ap.add_argument("--refresh", action="store_true", help="catch up open signals from market data")
    ap.add_argument("--journal", default=None, help="journal path (default: config tracker.journal_path)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    j = Journal(args.journal)
    changed = j.refresh() if args.refresh else []
    if args.json:
        print(json.dumps({"journal": str(j.path), "changed": [s.id for s in changed],
                          "signals": [s.to_dict() for s in j.signals], "stats": j.stats()}))
    else:
        if args.refresh:
            print(f"Refreshed: {len(changed)} signal(s) changed.")
        print(format_journal(j))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
