"""ChartPatternBreakoutStrategy（chart_pattern_breakout）TDD 測試。

2026-07-06：測試 TradingView「Chart Patterns Screener」這類古典圖表形態
（三角形/楔形收斂後突破）背後的核心概念是否有 edge——用 core.signal_engineer.
trendline_pair() 算出的上下已確認樞紐趨勢線，兩線夾角收斂到位後價格突破其中
一側即進場，跌回被突破的線內視為結構失敗、出場。這跟均線密集/發散判斷的思路
類似，但訊號來源完全不同（真實樞紐趨勢線 vs 六線價差），是獨立驗證用的新策略。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import STRATEGIES, build_strategy


def _mk_df(n=200, seed=9):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0.02, 1.0, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open": close, "high": close + np.abs(rng.normal(0, 0.5, n)) + 0.1,
        "low": close - np.abs(rng.normal(0, 0.5, n)) - 0.1,
        "close": close, "volume": np.abs(rng.normal(1000, 200, n)) + 1,
    }, index=idx)


def _row(res_line=110.0, sup_line=90.0, close=100.0,
        is_pattern_breakout=False, pattern_breakout_dir=0):
    return {"res_line": res_line, "sup_line": sup_line, "close": close,
            "is_pattern_breakout": is_pattern_breakout,
            "pattern_breakout_dir": pattern_breakout_dir}


def test_registered_and_buildable():
    assert "chart_pattern_breakout" in STRATEGIES
    s = build_strategy("chart_pattern_breakout")
    assert s.allow_short is True


def test_prepare_adds_all_columns():
    s = build_strategy("chart_pattern_breakout")
    out = s.prepare(_mk_df())
    for col in ("res_line", "sup_line", "is_pattern_breakout", "pattern_breakout_dir", "atr"):
        assert col in out.columns, f"缺欄位 {col}"


def test_long_entry_on_upward_breakout():
    s = build_strategy("chart_pattern_breakout")
    r = _row(is_pattern_breakout=True, pattern_breakout_dir=1)
    assert s.signal(r, 0) == 1


def test_short_entry_on_downward_breakout():
    s = build_strategy("chart_pattern_breakout")
    r = _row(is_pattern_breakout=True, pattern_breakout_dir=-1)
    assert s.signal(r, 0) == -1


def test_no_entry_when_not_breakout():
    s = build_strategy("chart_pattern_breakout")
    r = _row(is_pattern_breakout=False)
    assert s.signal(r, 0) == 0


def test_long_exit_when_close_falls_back_below_resistance():
    """多單持倉：收盤跌回被突破的阻力線內 → 視為結構失敗，出場。"""
    s = build_strategy("chart_pattern_breakout")
    r = _row(res_line=110.0, close=105.0)
    assert s.signal(r, 1) == 0


def test_long_holds_while_close_stays_above_resistance():
    s = build_strategy("chart_pattern_breakout")
    r = _row(res_line=110.0, close=115.0)
    assert s.signal(r, 1) == 1


def test_short_exit_when_close_rises_back_above_support():
    s = build_strategy("chart_pattern_breakout")
    r = _row(sup_line=90.0, close=95.0)
    assert s.signal(r, -1) == 0


def test_short_holds_while_close_stays_below_support():
    s = build_strategy("chart_pattern_breakout")
    r = _row(sup_line=90.0, close=85.0)
    assert s.signal(r, -1) == -1


@pytest.mark.parametrize("pos", [0, 1, -1])
def test_holds_position_when_lines_unavailable(pos):
    """樞紐不足（res_line/sup_line 皆 NaN，暖機期）→ 維持現狀，不強制平倉。"""
    s = build_strategy("chart_pattern_breakout")
    r = _row(res_line=float("nan"), sup_line=float("nan"))
    assert s.signal(r, pos) == pos


def test_runs_through_backtester():
    from backtest.backtester import run_backtest
    from core.risk_officer import RiskOfficer
    from config import Config
    cfg = Config(interval="4h", max_daily_loss_pct=10.0)
    res = run_backtest(_mk_df(300), build_strategy("chart_pattern_breakout"),
                       RiskOfficer(cfg), cfg)
    assert res.equity_curve is not None and len(res.equity_curve) > 0
