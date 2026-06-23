"""tests/test_plotting.py — core/plotting.py 視覺化測試。

只測「畫得出來、存得了檔、回傳路徑正確」這層，不碰交易決策。
matplotlib 用 Agg backend（plotting.py 內已設），無視窗也能存 PNG。

規則：
  - 所有 PNG 一律寫到 pytest tmp_path，絕不污染專案根目錄。
  - 確定性資料、明確 assert。
  - plot_equity 可吃任何具備所需屬性的物件；這裡用真正的
    backtest.backtester.BacktestResult，確保契約一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.backtester import BacktestResult
from core.plotting import plot_equity, plot_heatmap


# --------------------------------------------------------------------------- #
# 輔助：造一個確定性的 BacktestResult
# --------------------------------------------------------------------------- #

def _make_result() -> BacktestResult:
    """先漲一段、回檔、再創高 —— 保證 total_return>0 且有非零回撤可畫。"""
    idx = pd.date_range("2024-01-01", periods=12, freq="5min")
    eq = pd.Series(
        [1000.0, 1010.0, 1025.0, 1040.0, 1030.0,
         1015.0, 1020.0, 1050.0, 1070.0, 1060.0,
         1080.0, 1100.0],
        index=idx,
    )
    trades = [
        {"ts": idx[3], "side": "exit_signal", "pnl": 40.0, "dir": 1},
        {"ts": idx[6], "side": "exit_sltp", "pnl": -25.0, "dir": 1},
        {"ts": idx[10], "side": "exit_signal", "pnl": 60.0, "dir": 1},
    ]
    return BacktestResult(equity_curve=eq, trades=trades)


def _make_sweep_df() -> pd.DataFrame:
    """兩個參數欄 fast/slow + sharpe 欄的小掃描表（確定性）。"""
    return pd.DataFrame(
        {
            "fast": [5, 5, 10, 10],
            "slow": [20, 30, 20, 30],
            "sharpe": [1.20, 0.40, 2.10, 0.85],
        }
    )


# --------------------------------------------------------------------------- #
# 1. plot_equity：產出 PNG、檔案存在且 >0、回傳路徑正確
# --------------------------------------------------------------------------- #

def test_plot_equity_creates_png(tmp_path):
    result = _make_result()
    out = tmp_path / "equity.png"

    returned = plot_equity(result, str(out))

    # 回傳的就是我們傳進去的路徑
    assert returned == str(out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_plot_equity_is_real_png(tmp_path):
    """檔頭符合 PNG magic number，確認真的存成圖檔。"""
    out = tmp_path / "eq_magic.png"
    plot_equity(_make_result(), str(out))
    header = out.read_bytes()[:8]
    assert header == b"\x89PNG\r\n\x1a\n"


def test_plot_equity_accepts_custom_title(tmp_path):
    """傳 title 不影響回傳契約，仍正常出圖。"""
    out = tmp_path / "eq_title.png"
    returned = plot_equity(_make_result(), str(out), title="My Run")
    assert returned == str(out)
    assert out.stat().st_size > 0


def test_plot_equity_empty_curve_raises(tmp_path):
    """equity_curve 為空時應明確報錯，而不是畫出壞圖。"""
    empty = BacktestResult(equity_curve=pd.Series([], dtype=float), trades=[])
    out = tmp_path / "eq_empty.png"
    with pytest.raises(ValueError):
        plot_equity(empty, str(out))
    # 報錯後不應留下半成品
    assert not out.exists()


def test_plot_equity_works_with_lightweight_object(tmp_path):
    """plot_equity 只靠鴨子型別：任何具備所需屬性的輕量物件都能畫。"""

    class _LightResult:
        def __init__(self):
            idx = pd.date_range("2024-02-01", periods=6, freq="h")
            self.equity_curve = pd.Series(
                [500.0, 520.0, 510.0, 540.0, 560.0, 555.0], index=idx
            )
            self.total_return = 0.11
            self.max_drawdown = -0.018
            self.win_rate = 0.5
            self.sharpe = 1.3
            self.trades = [{"pnl": 20.0}, {"pnl": -10.0}]

    out = tmp_path / "eq_light.png"
    returned = plot_equity(_LightResult(), str(out))
    assert returned == str(out)
    assert out.stat().st_size > 0


# --------------------------------------------------------------------------- #
# 2. plot_heatmap：兩參數欄 + metric 欄 → PNG
# --------------------------------------------------------------------------- #

def test_plot_heatmap_creates_png(tmp_path):
    df = _make_sweep_df()
    out = tmp_path / "heatmap.png"

    returned = plot_heatmap(df, "fast", "slow", metric="sharpe", path=str(out))

    assert returned == str(out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_plot_heatmap_is_real_png(tmp_path):
    out = tmp_path / "hm_magic.png"
    plot_heatmap(_make_sweep_df(), "fast", "slow", metric="sharpe", path=str(out))
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_plot_heatmap_default_metric_is_sharpe(tmp_path):
    """不傳 metric 時預設用 sharpe 欄，仍能出圖。"""
    out = tmp_path / "hm_default.png"
    returned = plot_heatmap(_make_sweep_df(), "fast", "slow", path=str(out))
    assert returned == str(out)
    assert out.stat().st_size > 0


def test_plot_heatmap_masks_neg_inf_without_crash(tmp_path):
    """metric 含 -inf（掃描中常見的廢值）時應被遮罩而非崩潰。"""
    df = pd.DataFrame(
        {
            "fast": [5, 5, 10, 10],
            "slow": [20, 30, 20, 30],
            "sharpe": [1.0, float("-inf"), 2.0, 0.5],
        }
    )
    out = tmp_path / "hm_neginf.png"

    returned = plot_heatmap(df, "fast", "slow", metric="sharpe", path=str(out))

    assert returned == str(out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_plot_heatmap_masks_nan_without_crash(tmp_path):
    """metric 含 NaN（某些格子沒掃到）時同樣應被遮罩而非崩潰。"""
    df = pd.DataFrame(
        {
            "fast": [5, 5, 10, 10],
            "slow": [20, 30, 20, 30],
            "sharpe": [1.0, np.nan, 2.0, 0.5],
        }
    )
    out = tmp_path / "hm_nan.png"
    returned = plot_heatmap(df, "fast", "slow", metric="sharpe", path=str(out))
    assert returned == str(out)
    assert out.stat().st_size > 0


def test_plot_heatmap_custom_title(tmp_path):
    out = tmp_path / "hm_title.png"
    returned = plot_heatmap(
        _make_sweep_df(), "fast", "slow", metric="sharpe",
        path=str(out), title="Sharpe scan",
    )
    assert returned == str(out)
    assert out.stat().st_size > 0


# --------------------------------------------------------------------------- #
# 3. 不污染專案根目錄：所有輸出都落在 tmp_path 底下
# --------------------------------------------------------------------------- #

def test_outputs_stay_inside_tmp_path(tmp_path):
    eq_path = plot_equity(_make_result(), str(tmp_path / "e.png"))
    hm_path = plot_heatmap(
        _make_sweep_df(), "fast", "slow", metric="sharpe",
        path=str(tmp_path / "h.png"),
    )
    assert str(tmp_path) in eq_path
    assert str(tmp_path) in hm_path
