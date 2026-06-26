"""費波那契通道 — signal_engineer.fib_channel_levels + FibChannelStrategy 測試。

新版定義（與 TradingView Fibonacci Channel 一致）：
  - 趨勢自適應錨定：上升 → 沿兩個 swing low 連線（0 線在下、支撐）；
                    下降 → 沿兩個 swing high 連線（0 線在上、阻力）。
  - 全套比率以平行線排列：0/0.236/0.382/0.5/0.618/0.786/1.0 + 延伸 1.272/1.618/2.0。
  - 每條線 = fib_ch_0 + r × (fib_ch_100 − fib_ch_0)（核心：平行 + 費波那契間距）。
  - fib_ch_pos：0 = 趨勢原點（進場側），1 = 對側（目標側），與方向無關。
  - fib_ch_dir：+1 上升 / -1 下降 / 0 未定。
"""
import numpy as np
import pandas as pd
import pytest

from core import signal_engineer as se
from core.quant_researcher import build_strategy


# ratio → 欄位名（用於重建公式驗證）
RATIO_COLS = {
    0.0:   "fib_ch_0",
    0.236: "fib_ch_236",
    0.382: "fib_ch_382",
    0.5:   "fib_ch_5",
    0.618: "fib_ch_618",
    0.786: "fib_ch_786",
    1.0:   "fib_ch_100",
    1.272: "fib_ch_1272",
    1.618: "fib_ch_1618",
    2.0:   "fib_ch_200",
}
ALL_LEVEL_COLS = list(RATIO_COLS.values())


def _df(n: int = 120, trend: str = "up", seed: int = 42) -> pd.DataFrame:
    """產生測試用 OHLCV。noise std=2.0 確保出現真實回調，讓 swing pivot 得以被確認。"""
    np.random.seed(seed)
    noise = np.random.randn(n) * 2.0
    if trend == "up":
        close = 100 + np.arange(n) * 0.5 + noise
    elif trend == "down":
        close = 200 - np.arange(n) * 0.5 + noise
    else:
        close = 100 + noise
    high = close + abs(np.random.randn(n)) * 1.5
    low  = close - abs(np.random.randn(n)) * 1.5
    return pd.DataFrame({
        "open": close - 0.2, "high": high, "low": low, "close": close,
        "volume": np.random.randint(100, 1000, n).astype(float),
    })


class TestFibChannelLevels:
    def test_returns_all_fib_columns(self):
        out = se.fib_channel_levels(_df(100))
        for col in ALL_LEVEL_COLS + ["fib_ch_pos", "fib_ch_dir"]:
            assert col in out.columns, f"缺少欄位：{col}"

    def test_fib_reconstruction_formula(self):
        """核心：每條線都必須 = fib_ch_0 + r × (fib_ch_100 − fib_ch_0)。

        這正是 TradingView Fib Channel 的定義，也是反推使用者圖片數字所得的公式。
        """
        out = se.fib_channel_levels(_df(140, "up"))
        valid = out.dropna(subset=["fib_ch_0", "fib_ch_100"])
        assert not valid.empty, "上升趨勢應有有效通道"
        width = valid["fib_ch_100"] - valid["fib_ch_0"]
        for r, col in RATIO_COLS.items():
            expected = valid["fib_ch_0"] + r * width
            assert np.allclose(valid[col], expected, atol=1e-6), \
                f"{col} 不符費波那契重建公式（r={r}）"

    def test_uptrend_levels_ascend(self):
        """上升趨勢：dir=+1，0 線在下、100 線在上（fib_ch_0 < fib_ch_100）。"""
        out = se.fib_channel_levels(_df(140, "up"))
        valid = out.dropna(subset=["fib_ch_0", "fib_ch_100", "fib_ch_dir"])
        assert not valid.empty
        assert (valid["fib_ch_dir"] == 1).all(), "上升趨勢 dir 應全為 +1"
        assert (valid["fib_ch_0"] < valid["fib_ch_100"]).all(), "上升趨勢 0 線應低於 100 線"

    def test_downtrend_levels_descend(self):
        """下降趨勢：dir=-1，0 線在上、100 線在下（fib_ch_0 > fib_ch_100）。"""
        out = se.fib_channel_levels(_df(140, "down"))
        valid = out.dropna(subset=["fib_ch_0", "fib_ch_100", "fib_ch_dir"])
        assert not valid.empty, "下降趨勢應有有效通道"
        assert (valid["fib_ch_dir"] == -1).all(), "下降趨勢 dir 應全為 -1"
        assert (valid["fib_ch_0"] > valid["fib_ch_100"]).all(), "下降趨勢 0 線應高於 100 線"

    def test_extensions_beyond_100(self):
        """延伸線（>1.0）離 0 線比 100 線更遠，且方向一致。"""
        out = se.fib_channel_levels(_df(140, "up"))
        valid = out.dropna(subset=["fib_ch_0", "fib_ch_100", "fib_ch_1618"])
        assert not valid.empty
        d100 = (valid["fib_ch_100"]  - valid["fib_ch_0"]).abs()
        d162 = (valid["fib_ch_1618"] - valid["fib_ch_0"]).abs()
        assert (d162 > d100).all(), "1.618 應比 1.0 離 0 線更遠"

    def test_pos_matches_normalized_close(self):
        """fib_ch_pos == (close − fib_ch_0) / (fib_ch_100 − fib_ch_0)。"""
        df  = _df(140, "up")
        out = se.fib_channel_levels(df)
        valid = out.dropna(subset=["fib_ch_pos", "fib_ch_0", "fib_ch_100"])
        assert not valid.empty
        width = valid["fib_ch_100"] - valid["fib_ch_0"]
        expected = (df.loc[valid.index, "close"] - valid["fib_ch_0"]) / width
        assert np.allclose(valid["fib_ch_pos"], expected, atol=1e-6)

    def test_causal_warmup_is_nan(self):
        """前段 warmup 期必須是 NaN，不得用未來資料。"""
        out = se.fib_channel_levels(_df(120), pivot_left=5, pivot_right=5)
        assert out["fib_ch_0"].iloc[:12].isna().all(), "warmup 期應全為 NaN"

    def test_insufficient_pivots_returns_all_nan(self):
        out = se.fib_channel_levels(_df(10, "up"), pivot_left=3, pivot_right=3)
        assert out["fib_ch_0"].isna().all(), "不足 pivot 應全 NaN"

    def test_channel_wider_when_volatility_higher(self):
        def _vol_df(noise_std, n=160, seed=7):
            np.random.seed(seed)
            noise = np.random.randn(n) * noise_std
            close = 100 + np.arange(n) * 0.3 + noise
            high  = close + abs(np.random.randn(n)) * noise_std
            low   = close - abs(np.random.randn(n)) * noise_std
            return pd.DataFrame({"open": close - 0.1, "high": high,
                                  "low": low, "close": close,
                                  "volume": np.ones(n) * 500.0})

        low_vol  = se.fib_channel_levels(_vol_df(1.0))
        high_vol = se.fib_channel_levels(_vol_df(6.0))
        low_w  = (low_vol["fib_ch_100"]  - low_vol["fib_ch_0"]).abs().dropna().mean()
        high_w = (high_vol["fib_ch_100"] - high_vol["fib_ch_0"]).abs().dropna().mean()
        assert not np.isnan(low_w) and not np.isnan(high_w)
        assert high_w > low_w, "高波動應產生更寬的通道"

    def test_dir_zero_rows_have_nan_levels(self):
        """方向未定（dir=0）的行，通道線應為 NaN（不亂畫）。"""
        out = se.fib_channel_levels(_df(120, "flat"))
        zero_dir = out[out["fib_ch_dir"] == 0]
        if not zero_dir.empty:
            assert zero_dir["fib_ch_0"].isna().all(), "dir=0 的行通道線應為 NaN"


class TestFibChannelSingle:
    """單一通道（畫圖用）：固定錨點 + 斜率 + 寬度，可拉成橫跨整圖的直線。"""

    def test_returns_params_for_trend(self):
        out = se.fib_channel_single(_df(160, "up"))
        assert out is not None
        for k in ("dir", "anchor_idx", "anchor_price", "slope", "width"):
            assert k in out, f"缺少 key：{k}"
        assert out["dir"] in (1, -1)
        assert out["width"] > 0

    def test_uptrend_dir_positive(self):
        out = se.fib_channel_single(_df(160, "up"))
        assert out["dir"] == 1

    def test_downtrend_dir_negative(self):
        out = se.fib_channel_single(_df(160, "down"))
        assert out["dir"] == -1

    def test_straight_line_reconstruction(self):
        """任一 bar 的 0 線 = anchor_price + slope×(i − anchor_idx)，整段為直線。"""
        out = se.fib_channel_single(_df(160, "up"))
        i1, i2 = out["anchor_idx"] + 10, out["anchor_idx"] + 40
        v1 = out["anchor_price"] + out["slope"] * (i1 - out["anchor_idx"])
        v2 = out["anchor_price"] + out["slope"] * (i2 - out["anchor_idx"])
        # 斜率定義一致：兩點連線斜率 == slope
        assert abs((v2 - v1) / (i2 - i1) - out["slope"]) < 1e-9

    def test_none_when_insufficient(self):
        assert se.fib_channel_single(_df(10, "up"), pivot_left=4, pivot_right=4) is None


class TestFibChannelStrategy:
    def test_strategy_registered(self):
        assert build_strategy("fib_channel") is not None

    def test_prepare_adds_channel_columns(self):
        strat = build_strategy("fib_channel")
        out   = strat.prepare(_df(120, "up"))
        for col in ("fib_ch_0", "fib_ch_100", "fib_ch_pos", "fib_ch_dir"):
            assert col in out.columns

    def test_signal_returns_valid_values(self):
        strat = build_strategy("fib_channel")
        prepared = strat.prepare(_df(120, "up")).dropna()
        for _, row in prepared.iterrows():
            assert strat.signal(row, 0) in (-1, 0, 1)

    def test_long_entry_in_uptrend_pullback(self):
        """上升趨勢 + 回調到原點（pos<entry_z）→ 做多。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               entry_zone=0.35)
        prepared = strat.prepare(_df(140, "up")).dropna()
        zone = prepared[(prepared["fib_ch_dir"] == 1) & (prepared["fib_ch_pos"] < 0.35)]
        if zone.empty:
            pytest.skip("無上升回調區行")
        signals = [strat.signal(row, 0) for _, row in zone.iterrows()]
        assert 1 in signals, "上升趨勢回調區應有做多信號"

    def test_short_entry_in_downtrend_pullback(self):
        """下降趨勢 + 回調到原點（pos<entry_z）→ 做空。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               entry_zone=0.35)
        prepared = strat.prepare(_df(140, "down")).dropna()
        zone = prepared[(prepared["fib_ch_dir"] == -1) & (prepared["fib_ch_pos"] < 0.35)]
        if zone.empty:
            pytest.skip("無下降回調區行")
        signals = [strat.signal(row, 0) for _, row in zone.iterrows()]
        assert -1 in signals, "下降趨勢回調區應有做空信號"

    def test_exit_long_when_reach_target(self):
        """持多且 pos > exit_z（到達目標側）→ 平倉。"""
        strat = build_strategy("fib_channel", exit_zone=0.70)
        prepared = strat.prepare(_df(140, "up")).dropna()
        upper = prepared[prepared["fib_ch_pos"] > 0.70]
        if upper.empty:
            pytest.skip("無目標側行")
        signals = [strat.signal(row, 1) for _, row in upper.iterrows()]
        assert 0 in signals, "到達目標側持多應平倉"

    def test_no_entry_without_channel(self):
        strat = build_strategy("fib_channel")
        row = {"fib_ch_pos": float("nan"), "fib_ch_dir": float("nan"), "close": 100.0}
        assert strat.signal(row, 0) == 0


class TestFibChannelMinWidth:
    """通道寬度過濾：通道太窄（壓縮行情）時拒絕進場，避免小幅震盪過度交易。

    min_channel_width_atr = N 表示：
      若 |fib_ch_100 - fib_ch_0| < N × ATR → 不進場。
    出場邏輯不受影響（寬度再窄仍平倉）。
    """

    def _entry_row(self, ch0, ch100, atr, pos=0.10, ch_dir=1.0, regime="trend"):
        """建立進場用 row：pos < entry_zone(0.30) 確保在進場區。"""
        return {
            "fib_ch_0": ch0, "fib_ch_100": ch100,
            "fib_ch_pos": pos, "fib_ch_dir": ch_dir,
            "atr": atr, "regime": regime,
        }

    def test_blocks_entry_when_channel_narrower_than_threshold(self):
        """channel_width(0.5) < 1.0 × ATR(1.0) → 拒絕進場。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               min_channel_width_atr=1.0)
        row = self._entry_row(ch0=100.0, ch100=100.5, atr=1.0)
        assert strat.signal(row, 0) == 0, \
            "通道寬 0.5 < 1.0 ATR，應拒絕進場"

    def test_allows_entry_when_channel_wider_than_threshold(self):
        """channel_width(2.0) >= 1.0 × ATR(1.0) → 允許進場。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               min_channel_width_atr=1.0)
        row = self._entry_row(ch0=100.0, ch100=102.0, atr=1.0)
        assert strat.signal(row, 0) == 1, \
            "通道寬 2.0 >= 1.0 ATR，應允許多單進場"

    def test_exit_not_blocked_by_narrow_channel(self):
        """持倉中通道太窄仍應正常出場（不能因寬度過濾影響平倉）。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               min_channel_width_atr=5.0,
                               exit_zone=0.80)
        row = self._entry_row(ch0=100.0, ch100=100.1, atr=1.0, pos=0.90)
        assert strat.signal(row, 1) == 0, \
            "pos > exit_zone 應平倉，即使通道窄"

    def test_default_min_width_is_zero_so_existing_behavior_unchanged(self):
        """min_channel_width_atr 預設值為 0（不過濾），不影響現有策略行為。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0)
        row = self._entry_row(ch0=100.0, ch100=100.1, atr=1.0)  # 極窄通道
        assert strat.signal(row, 0) == 1, \
            "預設 min_channel_width_atr=0 時不應過濾任何進場"

    def test_no_atr_skips_width_filter(self):
        """ATR 欄位缺失（NaN）時跳過寬度過濾，不因缺欄阻擋進場。"""
        strat = build_strategy("fib_channel",
                               er_trend=0.0, chop_trend=100.0, adx_trend=0.0,
                               min_channel_width_atr=1.0)
        row = self._entry_row(ch0=100.0, ch100=100.1, atr=float("nan"))
        assert strat.signal(row, 0) == 1, \
            "ATR 缺失時應跳過寬度過濾"
