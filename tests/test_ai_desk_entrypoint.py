"""run_ai_desk_once 測試 — 只測純函式 prepare_df（設索引+丟未收盤根），不碰網路。"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_ai_desk_once import prepare_df


def test_prepare_df_sets_index_and_drops_open_bar():
    n = 10
    idx = pd.date_range("2026-07-01", periods=n, freq="4h", name="open_time")
    raw = pd.DataFrame({"open": np.ones(n), "high": np.ones(n), "low": np.ones(n),
                        "close": np.ones(n), "volume": np.ones(n)}, index=idx)
    df = prepare_df(raw)
    assert len(df) == n - 1                          # 最後一根（未收盤）被丟掉
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index[-1] == idx[-2]


def test_auto_approve_enabled_defaults_to_false(monkeypatch):
    from run_ai_desk_once import auto_approve_enabled
    monkeypatch.delenv("AI_DESK_AUTO_APPROVE", raising=False)
    assert auto_approve_enabled() is False


def test_auto_approve_enabled_requires_exact_true(monkeypatch):
    from run_ai_desk_once import auto_approve_enabled
    monkeypatch.setenv("AI_DESK_AUTO_APPROVE", "1")
    assert auto_approve_enabled() is False
    monkeypatch.setenv("AI_DESK_AUTO_APPROVE", "true")
    assert auto_approve_enabled() is True
    monkeypatch.setenv("AI_DESK_AUTO_APPROVE", "TRUE")
    assert auto_approve_enabled() is True
