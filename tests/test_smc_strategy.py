"""SMC Strategy — Break of Structure + Fair Value Gap 測試。"""
import pandas as pd
import numpy as np
import pytest

from core import signal_engineer as se
from core.quant_researcher import build_strategy


def _df(n=80, trend="up"):
    """產生測試用 OHLCV。trend='up'=上升、'down'=下降、'flat'=盤整。

    噪音 std=3.0 確保在趨勢中出現真實回調，讓 swing pivot 得以被確認。
    """
    np.random.seed(42)
    noise = np.random.randn(n) * 3.0
    if trend == "up":
        close = 100 + np.arange(n) * 0.5 + noise
    elif trend == "down":
        close = 140 - np.arange(n) * 0.5 + noise
    else:
        close = 100 + noise
    high = close + abs(np.random.randn(n)) * 1.5
    low = close - abs(np.random.randn(n)) * 1.5
    return pd.DataFrame({
        "open": close - 0.2,
        "high": high,
        "low": low,
        "close": close,
        "volume": np.random.randint(100, 1000, n).astype(float),
    })


class TestSmcLevels:
    def test_returns_required_columns(self):
        df = _df(60)
        out = se.smc_levels(df)
        for col in ("bos_bull", "bos_bear", "fvg_bull", "fvg_bear"):
            assert col in out.columns, f"Missing column: {col}"

    def test_bos_bull_triggers_in_uptrend(self):
        df = _df(60, trend="up")
        out = se.smc_levels(df, pivot_left=3, pivot_right=3)
        assert out["bos_bull"].sum() > 0, "上升趨勢應有看漲 BOS"

    def test_bos_bear_triggers_in_downtrend(self):
        df = _df(60, trend="down")
        out = se.smc_levels(df, pivot_left=3, pivot_right=3)
        assert out["bos_bear"].sum() > 0, "下降趨勢應有看跌 BOS"

    def test_fvg_bull_detected_on_gap_up(self):
        """手工構造看漲缺口：high[i-2] < low[i]"""
        n = 20
        data = {
            "open":   [100.0] * n,
            "high":   [101.0] * n,
            "low":    [99.0] * n,
            "close":  [100.5] * n,
            "volume": [500.0] * n,
        }
        # 在第 10 根製造跳空向上
        data["high"][8] = 102.0   # candle i-2: high=102
        data["low"][10] = 104.0   # candle i  : low=104 > high[i-2]=102 → bullish FVG
        data["close"][10] = 105.0
        df = pd.DataFrame(data)
        out = se.smc_levels(df)
        assert out["fvg_bull"].iloc[10] == 1.0

    def test_fvg_bear_detected_on_gap_down(self):
        """手工構造看跌缺口：low[i-2] > high[i]"""
        n = 20
        data = {
            "open":   [100.0] * n,
            "high":   [101.0] * n,
            "low":    [99.0] * n,
            "close":  [100.5] * n,
            "volume": [500.0] * n,
        }
        data["low"][8] = 98.0    # candle i-2: low=98
        data["high"][10] = 96.0  # candle i  : high=96 < low[i-2]=98 → bearish FVG
        data["close"][10] = 95.0
        df = pd.DataFrame(data)
        out = se.smc_levels(df)
        assert out["fvg_bear"].iloc[10] == 1.0

    def test_causal_no_future_data(self):
        """BOS 不得用未來資料（pivot_right=3 → 延遲 3 根才確認）"""
        df = _df(60, trend="up")
        out = se.smc_levels(df, pivot_left=3, pivot_right=3)
        # 第 0-5 根（warmup）應為 NaN 或 0，不應在頭幾根就有 BOS
        assert out["bos_bull"].iloc[:6].fillna(0).sum() == 0


class TestSmcStrategy:
    def test_strategy_registered(self):
        strat = build_strategy("smc_structure")
        assert strat is not None

    def test_prepare_adds_smc_columns(self):
        strat = build_strategy("smc_structure")
        df = _df(80)
        out = strat.prepare(df)
        assert "bos_bull" in out.columns
        assert "fvg_bull" in out.columns

    def test_signal_returns_valid_values(self):
        strat = build_strategy("smc_structure")
        df = _df(80, trend="up")
        prepared = strat.prepare(df).dropna()
        for _, row in prepared.iterrows():
            sig = strat.signal(row, 0)
            assert sig in (-1, 0, 1)

    def test_long_signal_in_uptrend(self):
        # 關閉 regime 閘門（er_trend=0/chop_trend=100/adx_trend=0），
        # 單獨驗證 BOS+FVG 信號邏輯，避免合成資料的 regime 分類干擾。
        strat = build_strategy("smc_structure",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0)
        df = _df(80, trend="up")
        prepared = strat.prepare(df).dropna()
        signals = [strat.signal(row, 0) for _, row in prepared.iterrows()]
        assert 1 in signals, "上升趨勢應產生至少一個做多訊號"

    def test_short_signal_in_downtrend(self):
        strat = build_strategy("smc_structure",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0)
        df = _df(80, trend="down")
        prepared = strat.prepare(df).dropna()
        signals = [strat.signal(row, 0) for _, row in prepared.iterrows()]
        assert -1 in signals, "下降趨勢應產生至少一個做空訊號"
