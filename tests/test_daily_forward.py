"""每日 forward 追蹤器（research/scratchpad/daily_forward_tracker.py）純函式測試。

取代測試網 live bot：每天在剛收完的真實 4h K 線上重跑籃子回測，累積乾淨的
樣本外 forward 紀錄（因果、只用到當根為止的資料）。本檔驗證純邏輯——
增量 K 線合併、forward 起點切片、forward 彙總（bootstrap 下界）。
"""
import importlib.util
import os

import pandas as pd
import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "research", "scratchpad", "daily_forward_tracker.py")


def _load():
    spec = importlib.util.spec_from_file_location("daily_forward_tracker", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ft = _load()


# ── 增量 K 線合併（每日只抓新棒 append）───────────────────────────────
def test_merge_klines_dedups_and_sorts():
    idx1 = pd.date_range("2026-07-01", periods=3, freq="4h")
    old = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx1)
    idx2 = pd.date_range("2026-07-01 08:00", periods=3, freq="4h")   # 與 old 尾端重疊一根
    new = pd.DataFrame({"close": [99.0, 4.0, 5.0]}, index=idx2)       # 重疊那根用新值
    out = ft.merge_klines(old, new)
    assert list(out["close"]) == [1.0, 2.0, 99.0, 4.0, 5.0]           # 去重取新、時間正序
    assert out.index.is_monotonic_increasing
    assert not out.index.has_duplicates


def test_merge_klines_empty_new_returns_old():
    idx = pd.date_range("2026-07-01", periods=2, freq="4h")
    old = pd.DataFrame({"close": [1.0, 2.0]}, index=idx)
    out = ft.merge_klines(old, pd.DataFrame())
    assert list(out["close"]) == [1.0, 2.0]


# ── forward 起點切片（只算乾淨紀錄起點之後的交易）─────────────────────
def test_trades_since_filters_inclusive_by_ts():
    closed = [
        {"ts": "2026-07-05 00:00:00", "side": "exit_tp", "pnl": 5.0},
        {"ts": "2026-07-06 00:00:00", "side": "exit_sl", "pnl": -3.0},
        {"ts": "2026-07-07 04:00:00", "side": "exit_tp", "pnl": 8.0},
    ]
    out = ft.trades_since(closed, "2026-07-06")
    assert [t["pnl"] for t in out] == [-3.0, 8.0]        # 含起點當天、排除更早


def test_trades_since_handles_timestamp_and_str():
    closed = [{"ts": pd.Timestamp("2026-07-07 00:00:00"), "side": "exit_tp", "pnl": 1.0}]
    assert len(ft.trades_since(closed, pd.Timestamp("2026-07-06"))) == 1


# ── forward 彙總（筆數/總損益/期望/bootstrap 下界）────────────────────
def test_forward_report_aggregates_with_lower_bound():
    r = ft.forward_report([10.0, -4.0, 6.0, -2.0])
    assert r["n"] == 4
    assert r["total"] == 10.0
    assert r["expectancy"] == pytest.approx(2.5)
    assert isinstance(r["lb"], float)          # bootstrap 信賴下界
    assert r["lb"] <= r["expectancy"]          # 下界不高於點估計


def test_forward_report_empty_is_safe():
    r = ft.forward_report([])
    assert r["n"] == 0 and r["total"] == 0.0
    assert r["expectancy"] is None
