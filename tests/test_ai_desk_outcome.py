"""ai_desk.outcome 測試 — 純函式結算，用合成 K 線，不碰網路。"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.outcome import evaluate_outcome, summarize


def kl(bars):
    """bars: [(high, low, close), ...] → DataFrame（時間僅為序）。"""
    idx = pd.date_range("2026-07-17", periods=len(bars), freq="1h")
    return pd.DataFrame(
        {"high": [b[0] for b in bars], "low": [b[1] for b in bars],
         "close": [b[2] for b in bars]}, index=idx)


SHORT = dict(direction=-1, entry=100.0, stop=110.0, take_profit=80.0, qty=2.0)


def test_never_touched_entry_is_unfilled():
    # 空單掛在 100（市價下方沒漲上來）→ 從未成交
    r = evaluate_outcome(klines=kl([(95, 90, 92), (97, 93, 96)]), **SHORT)
    assert r["state"] == "unfilled"
    assert r["pnl"] == 0.0


def test_short_filled_then_stopped():
    # 第2根觸及 100 成交；第3根衝到 110 → 停損
    r = evaluate_outcome(klines=kl([(95, 90, 92), (101, 96, 100), (112, 105, 111)]), **SHORT)
    assert r["state"] == "stopped"
    assert r["pnl"] == pytest.approx(-(110.0 - 100.0) * 2.0)   # -20


def test_short_filled_then_target():
    # 第2根成交；第3根跌到 80 → 停利
    r = evaluate_outcome(klines=kl([(95, 90, 92), (101, 96, 100), (99, 79, 81)]), **SHORT)
    assert r["state"] == "target"
    assert r["pnl"] == pytest.approx((100.0 - 80.0) * 2.0)     # +40


def test_short_filled_still_open_marks_to_market():
    r = evaluate_outcome(klines=kl([(101, 96, 100), (103, 97, 98)]), **SHORT)
    assert r["state"] == "open"
    assert r["pnl"] == pytest.approx((100.0 - 98.0) * 2.0)     # 空單賺價差 +4


def test_same_bar_stop_and_target_counts_as_stop():
    """同一根同時觸及停損與停利 → 保守算停損（不美化績效）。"""
    r = evaluate_outcome(klines=kl([(101, 96, 100), (111, 79, 95)]), **SHORT)
    assert r["state"] == "stopped"


def test_long_side_symmetric():
    long = dict(direction=1, entry=100.0, stop=90.0, take_profit=120.0, qty=1.0)
    # 第1根跌到100成交，第2根漲到120 → 停利
    r = evaluate_outcome(klines=kl([(105, 99, 101), (121, 110, 119)]), **long)
    assert r["state"] == "target"
    assert r["pnl"] == pytest.approx(20.0)


def test_empty_klines_is_no_data():
    r = evaluate_outcome(klines=kl([]), **SHORT)
    assert r["state"] == "no_data"


def test_summarize_counts_and_totals():
    rows = [{"state": "stopped", "pnl": -45.09}, {"state": "target", "pnl": 30.0},
            {"state": "open", "pnl": 5.0}, {"state": "unfilled", "pnl": 0.0}]
    s = summarize(rows)
    assert s["closed"] == 2                 # 只有停損/停利算已結算
    assert s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["realized_pnl"] == pytest.approx(-15.09)
    assert s["open_pnl"] == pytest.approx(5.0)
    assert s["total"] == 4
