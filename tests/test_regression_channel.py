"""RegressionChannelStrategy（regression_channel）TDD 測試。

2026-07-06：測試 TradingView「Polynomial/Linear Regression Volume Profile」
這類指標背後的核心概念是否有 edge——用滾動 OLS 線性迴歸配適的通道（統計配適，
非樞紐點錨定）當進出場依據：觸及下軌賭均值回歸、回到中心線視為停利出場。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import STRATEGIES, build_strategy


def _mk_df(n=200, seed=4):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0.0, 1.0, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open": close, "high": close + np.abs(rng.normal(0, 0.5, n)) + 0.1,
        "low": close - np.abs(rng.normal(0, 0.5, n)) - 0.1,
        "close": close, "volume": np.abs(rng.normal(1000, 200, n)) + 1,
    }, index=idx)


def _row(center=100.0, upper=110.0, lower=90.0, close=100.0):
    return {"reg_center": center, "reg_upper": upper, "reg_lower": lower, "close": close}


def test_registered_and_buildable():
    assert "regression_channel" in STRATEGIES
    s = build_strategy("regression_channel")
    assert s.allow_short is True


def test_prepare_adds_all_columns():
    s = build_strategy("regression_channel", window=20)
    out = s.prepare(_mk_df())
    for col in ("reg_center", "reg_upper", "reg_lower", "reg_slope", "atr"):
        assert col in out.columns, f"缺欄位 {col}"


def test_center_between_lower_and_upper_when_defined():
    s = build_strategy("regression_channel", window=20, band_mult=2.0)
    out = s.prepare(_mk_df()).dropna(subset=["reg_center"])
    assert (out["reg_lower"] <= out["reg_center"]).all()
    assert (out["reg_center"] <= out["reg_upper"]).all()


def test_no_channel_values_before_window_bars():
    """window 根數不足前，通道值必須是 NaN（不能提前算出配適線，因果性）。"""
    s = build_strategy("regression_channel", window=50)
    out = s.prepare(_mk_df(80))
    assert out["reg_center"].iloc[:49].isna().all()
    assert out["reg_center"].iloc[49:].notna().all()


def test_is_causal_no_repaint():
    """因果/非重繪：截斷尾端重算，前綴的迴歸通道值完全不變（每根只用自己往前 window 根）。"""
    s = build_strategy("regression_channel", window=20)
    df = _mk_df(120)
    full = s.prepare(df)
    for k in (60, 90, 110):
        prefix = s.prepare(df.iloc[:k])
        for col in ("reg_center", "reg_upper", "reg_lower"):
            a, b = full[col].iloc[:k], prefix[col]
            np.testing.assert_allclose(a.values, b.values)


def test_long_entry_when_touching_lower_band():
    s = build_strategy("regression_channel")
    r = _row(lower=90.0, close=89.0)
    assert s.signal(r, 0) == 1


def test_short_entry_when_touching_upper_band():
    s = build_strategy("regression_channel")
    r = _row(upper=110.0, close=111.0)
    assert s.signal(r, 0) == -1


def test_no_entry_when_inside_band():
    s = build_strategy("regression_channel")
    r = _row(lower=90.0, upper=110.0, close=100.0)
    assert s.signal(r, 0) == 0


def test_long_exit_at_centerline():
    """多單持倉：收盤回到(或超過)中心線 → 結構性停利出場。"""
    s = build_strategy("regression_channel")
    r = _row(center=100.0, close=101.0)
    assert s.signal(r, 1) == 0


def test_long_holds_below_centerline():
    s = build_strategy("regression_channel")
    r = _row(center=100.0, close=95.0)
    assert s.signal(r, 1) == 1


def test_short_exit_at_centerline():
    s = build_strategy("regression_channel")
    r = _row(center=100.0, close=99.0)
    assert s.signal(r, -1) == 0


def test_short_holds_above_centerline():
    s = build_strategy("regression_channel")
    r = _row(center=100.0, close=105.0)
    assert s.signal(r, -1) == -1


@pytest.mark.parametrize("pos", [0, 1, -1])
def test_holds_position_when_channel_unavailable(pos):
    """暖機期(window 根數不足，通道欄位 NaN) → 維持現狀，不強制平倉。"""
    s = build_strategy("regression_channel")
    r = _row(center=float("nan"), upper=float("nan"), lower=float("nan"))
    assert s.signal(r, pos) == pos


def test_runs_through_backtester():
    from backtest.backtester import run_backtest
    from core.risk_officer import RiskOfficer
    from config import Config
    cfg = Config(interval="4h", max_daily_loss_pct=10.0)
    res = run_backtest(_mk_df(300), build_strategy("regression_channel", window=50),
                       RiskOfficer(cfg), cfg)
    assert res.equity_curve is not None and len(res.equity_curve) > 0
