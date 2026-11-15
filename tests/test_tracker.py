"""Phase 4: signal tracker state machine, journal persistence, catch-up and stats."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd
import pytest
from smc_builders import T0, candles

from smc import tracker as tr
from smc.signal import Target, TradePlan
from smc.zones import Zone

NOW_LATE = (T0 + timedelta(days=30)).to_pydatetime()


def make_signal(direction=1, targets=((104, 2.0), (106, 3.0), (110, 5.0)), entry=100.0, stop=98.0,
                hours=48) -> tr.TrackedSignal:
    tps = [{"name": f"TP{i + 1}", "price": p, "rr": r} for i, (p, r) in enumerate(targets)]
    return tr.TrackedSignal("x", "BTCUSDT", "crypto", direction, entry, stop, tps, 0.01, "5m",
                            T0.isoformat(), (T0 + timedelta(hours=hours)).isoformat())


def bars(rows, start=T0):
    return candles(rows, start=start, freq="5min")


# --- state machine -------------------------------------------------------------------------------------

def test_full_win_scales_out_across_targets():
    s = make_signal()
    df = bars([(101, 101, 99.8, 100.5), (100.5, 104.2, 100.4, 104), (104, 106.3, 103.8, 106),
               (106, 110.5, 105.5, 110)])
    assert s.process(df, now=NOW_LATE)
    assert s.status == "tp3_hit" and s.result == "win" and not s.is_open
    assert s.r_multiple == pytest.approx((2 + 3 + 5) / 3, abs=1e-3)
    assert [e["event"] for e in s.events] == ["triggered", "tp1_hit", "tp2_hit", "tp3_hit"]


def test_stop_before_tp1_is_minus_one_r():
    s = make_signal()
    s.process(bars([(101, 101, 99.9, 100.2), (100.2, 100.5, 97.9, 98.1)]), now=NOW_LATE)
    assert (s.status, s.result, s.r_multiple) == ("sl_hit", "loss", -1.0)


def test_breakeven_stop_after_tp1():
    s = make_signal()
    s.process(bars([(101, 101, 99.9, 100.2), (100.2, 104.1, 100.1, 103), (103, 103, 99.9, 100)]), now=NOW_LATE)
    assert s.status == "stopped_breakeven" and s.result == "win"
    assert s.r_multiple == pytest.approx(2 / 3, abs=1e-3)       # 1/3 at 2R, rest at 0R


def test_stop_on_fill_candle():
    s = make_signal()
    s.process(bars([(101, 101, 97.5, 98)]), now=NOW_LATE)
    assert s.status == "sl_hit" and s.triggered_at is not None


def test_same_bar_stop_and_target_counts_as_stop():
    s = make_signal()
    s.process(bars([(101, 101, 99.9, 100.2), (100.2, 105, 97.5, 101)]), now=NOW_LATE)
    assert s.status == "sl_hit"


def test_target_before_fill_is_missed():
    s = make_signal()
    s.process(bars([(101, 104.5, 100.5, 104)]), now=NOW_LATE)
    assert s.status == "expired" and s.result == "expired" and "missed" in s.events[-1]["note"]


def test_expiry_without_fill():
    s = make_signal(hours=1)
    df = bars([(101, 102, 100.5, 101)] * 20)  # 100 minutes, never touches 100
    s.process(df, now=NOW_LATE)
    assert s.status == "expired"


def test_expiry_with_no_candles_at_all():
    s = make_signal(hours=1)
    s.process(bars([(101, 102, 100.5, 101)]), now=NOW_LATE)
    assert s.status == "expired"


def test_short_mirror():
    s = make_signal(direction=-1, targets=((96, 2.0), (94, 3.0)), entry=100.0, stop=102.0)
    s.process(bars([(99, 100.1, 99, 99.5), (99.5, 99.6, 95.9, 96), (96, 96.2, 93.9, 94)]), now=NOW_LATE)
    assert s.status == "tp2_hit" and s.result == "win" and not s.is_open    # 2 targets: last one closes it
    assert s.r_multiple == pytest.approx(2.5, abs=1e-3)


def test_semantic_target_names_in_status():
    s = make_signal(targets=((104, 2.0), (110, 5.0)))
    s.targets[1]["name"] = "TP3"
    s.process(bars([(101, 101, 99.9, 100.2), (100.2, 104.2, 100.1, 104)]), now=NOW_LATE)
    assert s.status == "tp1_hit" and s.is_open
    s.process(bars([(104, 110.2, 103.9, 110)], start=T0 + timedelta(minutes=10)), now=NOW_LATE)
    assert s.status == "tp3_hit" and not s.is_open


def test_replay_is_idempotent_and_incremental():
    rows = [(101, 101, 99.9, 100.2), (100.2, 104.1, 100.1, 103), (103, 106.2, 102.9, 106)]
    one = make_signal()
    one.process(bars(rows), now=NOW_LATE)
    assert not one.process(bars(rows), now=NOW_LATE)              # nothing new
    inc = make_signal()
    inc.process(bars(rows[:1]), now=NOW_LATE)
    inc.process(bars(rows), now=NOW_LATE)                         # catch-up over overlapping window
    assert (inc.status, inc.targets_hit, len(inc.events)) == (one.status, one.targets_hit, len(one.events))


def test_forming_bar_is_checked_but_not_marked_processed():
    s = make_signal()
    df = bars([(101, 102, 100.5, 101), (101, 101, 99.9, 100.1)])
    now = (T0 + timedelta(minutes=7)).to_pydatetime()                # 2nd bar (05:00-10:00) still forming
    s.process(df, now=now)
    assert s.status == "triggered"
    assert s.last_processed == T0.isoformat()


def test_bars_before_creation_are_ignored():
    s = make_signal()
    df = bars([(101, 101, 95, 96)], start=T0 - timedelta(minutes=5))
    s.process(df, now=NOW_LATE)
    assert s.status != "sl_hit"


# --- journal -----------------------------------------------------------------------------------------------

def plan(status="valid", entry=100.0, stop=98.0, symbol="BTCUSDT"):
    z = Zone("order_block", 1, 100.0, 99.0, 0, T0, 0, T0, "15m")
    return TradePlan(symbol=symbol, asset_class="crypto", status=status, direction=1, price=101.0,
                     generated_at=T0.isoformat(), cascade_status="ready", confluence=80, entry=entry,
                     stop_loss=stop, targets=[Target("TP1", 104.0, 2.0, "x"), Target("TP2", 106.0, 3.0, "y")],
                     primary_target="TP1", entry_zone=z, tick_size=0.01)


def test_journal_add_dedupe_and_persist(tmp_path):
    path = tmp_path / "j.json"
    j = tr.Journal(path)
    a = j.add_plan(plan(), now=T0.to_pydatetime())
    b = j.add_plan(plan(), now=T0.to_pydatetime())
    assert a is b and len(j.signals) == 1 and a.events[0]["event"] == "created"
    j2 = tr.Journal(path)
    assert len(j2.signals) == 1 and j2.signals[0].id == a.id and j2.signals[0].targets[0]["price"] == 104


def test_journal_rejects_invalid_plans(tmp_path):
    with pytest.raises(ValueError):
        tr.Journal(tmp_path / "j.json").add_plan(plan(status="no_valid_setup"))


def test_journal_refresh_catches_up_and_stats(tmp_path):
    j = tr.Journal(tmp_path / "j.json")
    win = j.add_plan(plan(), now=T0.to_pydatetime())
    loss = j.add_plan(plan(entry=100.0, stop=99.0, symbol="ETHUSDT"), now=T0.to_pydatetime())
    feeds = {
        "BTCUSDT": bars([(101, 101, 99.9, 100.2), (100.2, 104.1, 100.1, 104), (104, 106.1, 103.9, 106)]),
        "ETHUSDT": bars([(101, 101, 99.9, 100.2), (100.2, 100.3, 98.9, 99)]),
    }
    seen = []
    changed = j.refresh(fetch=lambda s, tf, n: seen.append((s, tf, n)) or feeds[s], now=NOW_LATE)
    assert {c.id for c in changed} == {win.id, loss.id} and all(tf == "5m" for _, tf, _ in seen)
    st = tr.Journal(tmp_path / "j.json").stats()                  # persisted
    assert (st["closed"], st["wins"], st["losses"], st["hit_rate"]) == (2, 1, 1, 0.5)
    assert st["avg_r"] == pytest.approx((2.5 - 1.0) / 2, abs=1e-3)


def test_refresh_skips_closed_and_survives_fetch_errors(tmp_path):
    from smc.data import DataError
    j = tr.Journal(tmp_path / "j.json")
    j.add_plan(plan(), now=T0.to_pydatetime())

    def boom(*a):
        raise DataError("offline")

    assert j.refresh(fetch=boom, now=NOW_LATE) == []
    assert j.signals[0].status == "pending"


def test_update_with_live_candle(tmp_path):
    j = tr.Journal(tmp_path / "j.json")
    s = j.add_plan(plan(), now=T0.to_pydatetime())
    candle = {"time": int(T0.timestamp()), "open": 101, "high": 101, "low": 99.9, "close": 100.1, "volume": 1}
    assert j.update_with_candle("BTCUSDT", "5m", candle, now=NOW_LATE) == [s]
    assert s.status == "triggered"


def test_bars_needed_is_capped(tmp_path):
    j = tr.Journal(tmp_path / "j.json")
    s = j.add_plan(plan(), now=T0.to_pydatetime())
    assert j.bars_needed(s, now=(T0 + timedelta(hours=1)).to_pydatetime()) == 14
    assert j.bars_needed(s, now=(T0 + timedelta(days=365)).to_pydatetime()) == 5000


def test_journal_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SMC_ANALYST_JOURNAL", str(tmp_path / "custom.json"))
    assert tr.journal_path() == (tmp_path / "custom.json").resolve()


def test_format_journal_empty_and_filled(tmp_path):
    j = tr.Journal(tmp_path / "j.json")
    assert "no tracked signals" in tr.format_journal(j)
    j.add_plan(plan(), now=T0.to_pydatetime())
    assert "BTCUSDT" in tr.format_journal(j) and "hit rate n/a" in tr.format_journal(j)
