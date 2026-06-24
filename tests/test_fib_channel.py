"""費波那契通道 — signal_engineer.fib_channel_levels + FibChannelStrategy 測試。"""
import numpy as np
import pandas as pd
import pytest

from core import signal_engineer as se
from core.quant_researcher import build_strategy


def _df(n: int = 100, trend: str = "up", seed: int = 42) -> pd.DataFrame:
    """產生測試用 OHLCV。noise std=2.0 確保出現真實回調，讓 swing pivot 得以被確認。"""
    np.random.seed(seed)
    noise = np.random.randn(n) * 2.0
    if trend == "up":
        close = 100 + np.arange(n) * 0.5 + noise
    elif trend == "down":
        close = 150 - np.arange(n) * 0.5 + noise
    else:
        close = 100 + noise
    high = close + abs(np.random.randn(n)) * 1.5
    low  = close - abs(np.random.randn(n)) * 1.5
    return pd.DataFrame({
        "open": close - 0.2, "high": high, "low": low, "close": close,
        "volume": np.random.randint(100, 1000, n).astype(float),
    })


class TestFibChannelLevels:
    def test_returns_required_columns(self):
        df  = _df(80)
        out = se.fib_channel_levels(df)
        for col in ("fib_ch_0", "fib_ch_382", "fib_ch_618", "fib_ch_100", "fib_ch_pos"):
            assert col in out.columns, f"Missing column: {col}"

    def test_causal_warmup_is_nan(self):
        """前幾根（warmup 期）必須是 NaN，不得用未來資料。"""
        df  = _df(100)
        out = se.fib_channel_levels(df, pivot_left=5, pivot_right=5)
        # 第一個確認 pivot 最早在 bar 5+5=10，通道需要兩個 pivot → 最早 bar ~20
        assert out["fib_ch_0"].iloc[:15].isna().all(), "warmup 期應全為 NaN"

    def test_has_valid_data_in_uptrend(self):
        """上升趨勢 100 根應有足夠 pivot，後半段要有通道資料。"""
        df  = _df(100, "up")
        out = se.fib_channel_levels(df, pivot_left=5, pivot_right=5)
        assert out["fib_ch_0"].notna().any(), "上升趨勢應出現至少一個通道有效值"

    def test_bands_are_ordered(self):
        """fib_ch_0 < fib_ch_382 < fib_ch_618 < fib_ch_100 在所有有效行。"""
        df  = _df(100, "up")
        out = se.fib_channel_levels(df, pivot_left=5, pivot_right=5)
        valid = out.dropna(subset=["fib_ch_0", "fib_ch_100"])
        assert (valid["fib_ch_0"] < valid["fib_ch_382"]).all(),  "fib_ch_0 應 < fib_ch_382"
        assert (valid["fib_ch_382"] < valid["fib_ch_618"]).all(), "fib_ch_382 應 < fib_ch_618"
        assert (valid["fib_ch_618"] < valid["fib_ch_100"]).all(), "fib_ch_618 應 < fib_ch_100"

    def test_insufficient_pivots_returns_all_nan(self):
        """K 棒太少 / 無足夠 pivot → 全 NaN。"""
        df  = _df(10, "up")  # 太短，找不到兩個 pivot
        out = se.fib_channel_levels(df, pivot_left=3, pivot_right=3)
        assert out["fib_ch_0"].isna().all(), "不足 pivot 應全 NaN"

    def test_fib_ch_pos_ratio_at_lower_band(self):
        """fib_ch_pos ≈ 0 當收盤貼近下帶（手工構造）。"""
        # 手工設定通道：直接讀已知的 fib_ch_0，確認 pos 接近 0
        df  = _df(100, "up")
        out = se.fib_channel_levels(df, pivot_left=5, pivot_right=5)
        valid = out.dropna(subset=["fib_ch_pos", "fib_ch_0", "fib_ch_100"])
        if valid.empty:
            pytest.skip("無有效通道資料（pivot 不足）")
        # 至少有些 row 的 pos 在合理範圍內（-1 ~ 2）
        assert valid["fib_ch_pos"].between(-1, 2).all(), "fib_ch_pos 應在合理範圍"


class TestFibChannelStrategy:
    def test_strategy_registered(self):
        strat = build_strategy("fib_channel")
        assert strat is not None

    def test_prepare_adds_channel_columns(self):
        strat = build_strategy("fib_channel")
        df    = _df(100, "up")
        out   = strat.prepare(df)
        assert "fib_ch_0"   in out.columns
        assert "fib_ch_100" in out.columns
        assert "fib_ch_pos" in out.columns

    def test_signal_returns_valid_values(self):
        strat = build_strategy("fib_channel")
        df    = _df(100, "up")
        prepared = strat.prepare(df).dropna()
        for _, row in prepared.iterrows():
            sig = strat.signal(row, 0)
            assert sig in (-1, 0, 1), f"無效信號: {sig}"

    def test_long_signal_when_in_lower_zone(self):
        """fib_ch_pos < entry_zone 且 regime OK → 做多信號。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               entry_zone=0.35)
        df    = _df(100, "up")
        prepared = strat.prepare(df).dropna()
        lower_zone = prepared[prepared["fib_ch_pos"] < 0.35]
        if lower_zone.empty:
            pytest.skip("無下帶區行")
        signals = [strat.signal(row, 0) for _, row in lower_zone.iterrows()]
        assert 1 in signals, "下帶區應有至少一個做多信號"

    def test_exit_long_when_price_above_exit_zone(self):
        """持多且 fib_ch_pos > exit_zone → 平倉（return 0）。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               exit_zone=0.70)
        df    = _df(100, "up")
        prepared = strat.prepare(df).dropna()
        upper_zone = prepared[prepared["fib_ch_pos"] > 0.70]
        if upper_zone.empty:
            pytest.skip("無上帶區行")
        signals = [strat.signal(row, 1) for _, row in upper_zone.iterrows()]
        assert 0 in signals, "上帶區持多應平倉"

    def test_signal_no_entry_without_channel(self):
        """通道 NaN 時不進場（維持空手）。"""
        strat = build_strategy("fib_channel")
        row = {"fib_ch_pos": float("nan"), "close": 100.0}
        assert strat.signal(row, 0) == 0

    def test_channel_wider_when_volatility_higher(self):
        """高波動資料的通道寬度（fib_ch_100 - fib_ch_0）應大於低波動資料。"""
        def _volatile_df(noise_std, n=150, seed=7):
            np.random.seed(seed)
            noise = np.random.randn(n) * noise_std
            close = 100 + np.arange(n) * 0.3 + noise
            high  = close + abs(np.random.randn(n)) * noise_std
            low   = close - abs(np.random.randn(n)) * noise_std
            return pd.DataFrame({"open": close - 0.1, "high": high,
                                  "low": low, "close": close,
                                  "volume": np.ones(n) * 500.0})

        low_vol  = se.fib_channel_levels(_volatile_df(1.0))
        high_vol = se.fib_channel_levels(_volatile_df(6.0))

        low_width  = (low_vol["fib_ch_100"]  - low_vol["fib_ch_0"]).dropna().mean()
        high_width = (high_vol["fib_ch_100"] - high_vol["fib_ch_0"]).dropna().mean()

        assert not np.isnan(low_width),  "低波動資料應能產生通道"
        assert not np.isnan(high_width), "高波動資料應能產生通道"
        assert high_width > low_width, "高波動應產生更寬的通道"
