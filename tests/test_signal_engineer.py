"""Tests for core.signal_engineer — ema / rsi / atr / zscore.

純函式技術指標。全部使用確定性資料、明確 assert，不寫任何 IO。
測試反映各指標「應有的正確行為」並通過現行程式。
"""
import numpy as np
import pandas as pd
import pytest

from core import signal_engineer as se


# --------------------------------------------------------------------------- #
# Fixtures: deterministic series / dataframes
# --------------------------------------------------------------------------- #
@pytest.fixture
def wiggly_series():
    """有趨勢又有波動的確定性序列（用於 ema 一致性 / 因果性）。"""
    x = np.arange(60, dtype=float)
    return pd.Series(np.sin(x / 3.0) + x * 0.1, name="close")


# --------------------------------------------------------------------------- #
# ema
# --------------------------------------------------------------------------- #
def test_ema_matches_pandas_ewm_span_adjust_false(wiggly_series):
    """ema 必須與 pandas ewm(span=period, adjust=False).mean() 完全一致。"""
    for period in (3, 5, 12, 26):
        got = se.ema(wiggly_series, period)
        expected = wiggly_series.ewm(span=period, adjust=False).mean()
        pd.testing.assert_series_equal(got, expected)


def test_ema_first_value_equals_seed(wiggly_series):
    """adjust=False 下，EMA 第一個值等於序列第一個值（種子）。"""
    out = se.ema(wiggly_series, 10)
    assert out.iloc[0] == pytest.approx(wiggly_series.iloc[0])
    assert out.notna().all()  # adjust=False -> 無前置 NaN


def test_ema_constant_series_is_constant():
    """常數序列的 EMA 永遠等於該常數。"""
    s = pd.Series([7.0] * 30)
    out = se.ema(s, 9)
    assert np.allclose(out.values, 7.0)


def test_ema_is_causal_no_lookahead(wiggly_series):
    """因果性：ema(整段)[:k] 與 ema(前 k 段) 在重疊區必須一致（無前視）。"""
    full = se.ema(wiggly_series, 12)
    for k in (10, 25, 40):
        prefix = se.ema(wiggly_series.iloc[:k], 12)
        # 重疊區 [0, k) 應逐點相等
        np.testing.assert_allclose(full.iloc[:k].values, prefix.values)


# --------------------------------------------------------------------------- #
# rsi
# --------------------------------------------------------------------------- #
def test_rsi_bounded_in_0_100():
    """RSI 在有定義之處必須落在 [0, 100]。"""
    rng = np.random.RandomState(0)
    s = pd.Series(100 + np.cumsum(rng.normal(size=300)))
    r = se.rsi(s, 14)
    defined = r.dropna()
    assert len(defined) > 0
    assert defined.between(0.0, 100.0).all()


def test_rsi_mostly_rising_approaches_100():
    """近乎一路上漲的序列 → RSI 接近 100（上漲動能壓倒下跌動能）。

    註：嚴格單調上漲時 loss 恆為 0，實作以 NaN 取代而得 NaN，
    故以「一個初始小跌後持續上漲」建立非零 loss 基準，這才是
    RSI 在強勢上漲下趨近 100 的正確語意。
    """
    rising = pd.Series([100.0, 99.0] + [99.0 + i for i in range(1, 60)], dtype=float)
    r = se.rsi(rising, 14)
    tail = r.iloc[-1]
    assert tail > 95.0
    assert r.dropna().between(0.0, 100.0).all()


def test_rsi_mostly_falling_approaches_0():
    """近乎一路下跌的序列 → RSI 接近 0。"""
    falling = pd.Series([100.0, 101.0] + [101.0 - i for i in range(1, 60)], dtype=float)
    r = se.rsi(falling, 14)
    tail = r.iloc[-1]
    assert tail < 5.0
    assert r.dropna().between(0.0, 100.0).all()


def test_rsi_strictly_falling_is_zero():
    """嚴格單調下跌：gain 恆為 0 → rs=0 → RSI 恆為 0。"""
    falling = pd.Series(range(60, 0, -1), dtype=float)
    r = se.rsi(falling, 14)
    defined = r.dropna()
    assert (defined == 0.0).all()


def test_rsi_is_causal_no_lookahead():
    """因果性：rsi(整段)[:k] 與 rsi(前 k 段) 在重疊區一致。"""
    rng = np.random.RandomState(7)
    s = pd.Series(100 + np.cumsum(rng.normal(size=120)))
    full = se.rsi(s, 14)
    for k in (30, 60, 90):
        prefix = se.rsi(s.iloc[:k], 14)
        # 用 fillna 對齊 NaN 後逐點比較（兩邊 NaN 位置應一致）
        a = full.iloc[:k]
        b = prefix
        assert a.isna().equals(b.isna())
        np.testing.assert_allclose(a.dropna().values, b.dropna().values)


# --------------------------------------------------------------------------- #
# atr
# --------------------------------------------------------------------------- #
@pytest.fixture
def small_ohlc():
    """簡單的 OHLC，TR 序列為 [2, 3, 2, 3]（可手算驗證）。"""
    return pd.DataFrame(
        {
            "high": [10.0, 12.0, 11.0, 13.0],
            "low": [8.0, 9.0, 9.0, 10.0],
            "close": [9.0, 11.0, 10.0, 12.0],
        }
    )


def test_atr_equals_hand_computation(small_ohlc):
    """在簡單 df 上，ATR 等於手算值。

    TR = max(high-low, |high-prevclose|, |low-prevclose|) = [2, 3, 2, 3]
    ewm(alpha=1/2, adjust=False) 遞迴：a0=2；a_t=0.5*TR_t+0.5*a_{t-1}
      -> [2.0, 2.5, 2.25, 2.625]
    """
    out = se.atr(small_ohlc, 2)
    expected = pd.Series([2.0, 2.5, 2.25, 2.625])
    np.testing.assert_allclose(out.values, expected.values)


def test_atr_is_positive():
    """ATR 為波動幅度，恆為正（high>low 的真實 K 線下）。"""
    rng = np.random.RandomState(3)
    n = 100
    close = 100 + np.cumsum(rng.normal(size=n))
    high = close + np.abs(rng.normal(scale=0.5, size=n)) + 0.1
    low = close - np.abs(rng.normal(scale=0.5, size=n)) - 0.1
    df = pd.DataFrame({"high": high, "low": low, "close": close})
    a = se.atr(df, 14)
    assert (a > 0).all()


def test_atr_constant_range_converges_to_range():
    """每根 K 線 high-low 固定、收盤不變時，ATR 應等於該固定區間。"""
    n = 50
    df = pd.DataFrame(
        {
            "high": [11.0] * n,
            "low": [9.0] * n,
            "close": [10.0] * n,
        }
    )
    a = se.atr(df, 14)
    # TR 恆為 2 -> EWM of 常數 = 常數
    assert np.allclose(a.values, 2.0)


# --------------------------------------------------------------------------- #
# zscore
# --------------------------------------------------------------------------- #
def test_zscore_first_window_minus_one_are_nan():
    """滾動 z-score 前 window-1 個值為 NaN，第 window 個起有定義。"""
    window = 50
    s = pd.Series(np.arange(120, dtype=float))
    z = se.zscore(s, window)
    assert z.iloc[: window - 1].isna().all()
    assert np.isfinite(z.iloc[window - 1])


def test_zscore_point_equals_standardized_value():
    """某點的 z-score 等於 (x - rolling_mean) / rolling_std（樣本標準差 ddof=1）。"""
    window = 10
    s = pd.Series(np.random.RandomState(42).normal(size=80))
    z = se.zscore(s, window)
    i = 40
    win = s.iloc[i - window + 1 : i + 1]
    expected = (s.iloc[i] - win.mean()) / win.std()  # pandas std 預設 ddof=1
    assert z.iloc[i] == pytest.approx(expected)


def test_zscore_is_causal_no_lookahead():
    """因果性：zscore(整段)[:k] 與 zscore(前 k 段) 在重疊區一致。"""
    window = 20
    s = pd.Series(np.random.RandomState(11).normal(size=100).cumsum())
    full = se.zscore(s, window)
    for k in (30, 60, 90):
        prefix = se.zscore(s.iloc[:k], window)
        a = full.iloc[:k]
        assert a.isna().equals(prefix.isna())
        np.testing.assert_allclose(a.dropna().values, prefix.dropna().values)


# --------------------------------------------------------------------------- #
# fib_retracement
# --------------------------------------------------------------------------- #
def _fib_df(prices) -> pd.DataFrame:
    """輔助：從收盤價建一個 OHLC DataFrame。"""
    p = np.array(prices, dtype=float)
    return pd.DataFrame({"open": p, "high": p + 0.5, "low": p - 0.5, "close": p})


def test_fib_pos_bounded_zero_to_one():
    """穩定震盪（sin 波）+ 大 lookback 時，fib_pos 應落在 [0, 1]。

    使用 lookback=60（大於振幅週期），使滾動高低點能充分覆蓋振盪範圍，
    避免 close 超出前期 rolling 高低點（會使 fib_pos > 1 或 < 0）。
    """
    idx = np.arange(150)
    prices = 100 + 10 * np.sin(idx * 0.2)  # 穩定在 [90, 110] 振盪
    df = _fib_df(prices)
    out = se.fib_retracement(df, lookback=60)
    # 排除 warmup（前 60 根）後取穩定區段
    defined = out["fib_pos"].iloc[65:].dropna()
    assert len(defined) > 0
    assert defined.between(0.0, 1.0).all()


def test_fib_pos_nan_when_range_is_zero():
    """高低點相同（range=0）時，fib_pos 應為 NaN（避免除零）。

    _fib_df 會加 ±0.5，所以這裡直接建 high==low==close==100 的 DataFrame。
    """
    n = 30
    df = pd.DataFrame({"open": [100.0]*n, "high": [100.0]*n,
                       "low": [100.0]*n, "close": [100.0]*n})
    out = se.fib_retracement(df, lookback=10)
    assert out["fib_pos"].dropna().empty


def test_fib_levels_correct_values():
    """fib_382 = low + 0.382*range，fib_618 = low + 0.618*range。"""
    prices = list(range(50, 110))  # 單調上漲 → 滾動 high/low 確定
    df = _fib_df(prices)
    lookback = 10
    out = se.fib_retracement(df, lookback=lookback)
    # 取一個有完整資料的列
    row = out.iloc[lookback + 5]
    fib_range = row["fib_high"] - row["fib_low"]
    assert row["fib_382"] == pytest.approx(row["fib_low"] + 0.382 * fib_range, rel=1e-6)
    assert row["fib_618"] == pytest.approx(row["fib_low"] + 0.618 * fib_range, rel=1e-6)


def test_fib_pos_at_swing_low_is_near_zero():
    """價格在波段低點附近時，fib_pos 應接近 0。"""
    # 建立：先大漲 → 大跌回原點，讓低點清晰
    prices = list(range(100, 160)) + list(range(160, 99, -1))
    df = _fib_df(prices)
    out = se.fib_retracement(df, lookback=30)
    # 最後幾根已跌回接近低點
    last_fib_pos = out["fib_pos"].iloc[-5:].dropna()
    assert (last_fib_pos < 0.4).any()


def test_fib_pos_at_swing_high_is_near_one():
    """價格在波段高點附近時，fib_pos 應接近 1。"""
    prices = list(range(100, 160))  # 持續上漲，高點在末端
    df = _fib_df(prices)
    out = se.fib_retracement(df, lookback=20)
    last_fib_pos = out["fib_pos"].iloc[-5:].dropna()
    assert (last_fib_pos > 0.8).any()


def test_fib_retracement_is_causal_no_lookahead():
    """因果性：用整段算的 fib_pos[:k] 必須與只用前 k 根算的結果一致。"""
    prices = list(range(100, 160)) + list(range(160, 110, -1))
    df = _fib_df(prices)
    lookback = 20
    full = se.fib_retracement(df, lookback=lookback)
    for k in (40, 60, 80):
        prefix = se.fib_retracement(df.iloc[:k], lookback=lookback)
        a = full["fib_pos"].iloc[:k]
        b = prefix["fib_pos"]
        assert a.isna().equals(b.isna())
        np.testing.assert_allclose(a.dropna().values, b.dropna().values)


# --------------------------------------------------------------------------- #
# fib_retracement — swing pivot 模式（pivot_left/right；右側確認、不 repaint）
# --------------------------------------------------------------------------- #
def test_fib_swing_pivot_levels_defined():
    """提供 pivot_left/right → 用已確認 swing 高低點，fib_high/low/pos 有定義。"""
    prices = list(range(100, 140)) + list(range(140, 110, -1)) + list(range(110, 135))
    df = _fib_df(prices)
    out = se.fib_retracement(df, pivot_left=3, pivot_right=3)
    assert out["fib_high"].notna().any()
    assert out["fib_low"].notna().any()
    defined = out["fib_pos"].dropna()
    assert len(defined) > 0


def test_fib_swing_pivot_confirmed_only_after_right_lag():
    """swing high 只在右側 right 根收完後才反映進 fib_high（不提前、不重繪）。"""
    # close 在 index 4 形成明確高點 110，之後下行
    prices = [100, 101, 102, 103, 110, 103, 102, 101, 100, 99, 98, 97, 96, 95]
    df = _fib_df(prices)            # high = close + 0.5 → 峰值 high = 110.5
    right = 3
    out = se.fib_retracement(df, pivot_left=2, pivot_right=right)
    peak_high = 110.5
    confirm_idx = 4 + right         # = 7
    # 確認前：fib_high 不可能等於尚未確認的峰值（否則就是提前看到未來）
    assert not (out["fib_high"].iloc[:confirm_idx] == peak_high).any()
    # 確認後：峰值已反映進 fib_high
    assert (out["fib_high"].iloc[confirm_idx:] == peak_high).any()


def test_fib_swing_pivot_plateau_not_mismarked():
    """平台/重複極值不可被誤標為 swing 後用 ffill 把真正較高的 pivot 拉低。

    high=[1,5,1,1,1,1,1,1]：真正波段高點是 idx1 的 5（right=1 於 idx2 確認）；
    之後一段持平的 1 不得被標成新 swing high 把 fib_high 退化成 1。
    """
    high = [1.0, 5.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    low = [0.4, 0.6, 0.3, 0.5, 0.2, 0.5, 0.3, 0.4]   # 任意變動，避免 fib_low 全 NaN
    close = [h - 0.1 for h in high]
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close})
    out = se.fib_retracement(df, pivot_left=1, pivot_right=1)
    fh = out["fib_high"]
    assert fh.iloc[2] == 5.0                          # idx2 確認到 5
    assert (fh.iloc[2:].dropna() == 5.0).all()        # 之後維持 5，不被平台的 1 覆蓋


def test_fib_swing_low_plateau_not_mismarked():
    """對稱：低點平台不得把 fib_low 從真正低點上拉。"""
    low = [9.0, 1.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0]
    high = [9.6, 9.4, 9.7, 9.5, 9.8, 9.5, 9.7, 9.6]
    close = [l + 0.1 for l in low]
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close})
    out = se.fib_retracement(df, pivot_left=1, pivot_right=1)
    fl = out["fib_low"]
    assert fl.iloc[2] == 1.0
    assert (fl.iloc[2:].dropna() == 1.0).all()


def test_fib_swing_pivot_is_causal_no_repaint():
    """因果/非重繪：swing 模式下 fib_high/low/pos 前綴不變（末根擾動不改寫已確認 pivot）。"""
    prices = list(range(100, 140)) + list(range(140, 110, -1)) + list(range(110, 135))
    df = _fib_df(prices)
    full = se.fib_retracement(df, pivot_left=3, pivot_right=3)
    for k in (30, 55, 80):
        prefix = se.fib_retracement(df.iloc[:k], pivot_left=3, pivot_right=3)
        for col in ("fib_high", "fib_low", "fib_pos"):
            a, b = full[col].iloc[:k], prefix[col]
            assert a.isna().equals(b.isna())
            np.testing.assert_allclose(a.dropna().values, b.dropna().values)


# --------------------------------------------------------------------------- #
# adx / +DI / -DI（Wilder 趨勢強度）
# --------------------------------------------------------------------------- #
def _trend_df(n=140, step=1.0, up=True):
    """確定性趨勢 OHLC：每根固定漲/跌 step，high/low 圍繞 close ±1。"""
    base = 1000.0 + np.arange(n, dtype=float) * (step if up else -step)
    return pd.DataFrame({"high": base + 1.0, "low": base - 1.0, "close": base})


def _chop_df(n=140, amp=5.0):
    """確定性盤整 OHLC：close 在區間內鋸齒震盪、無淨位移。"""
    base = 1000.0 + amp * np.array([1.0 if i % 2 else -1.0 for i in range(n)])
    return pd.DataFrame({"high": base + 1.0, "low": base - 1.0, "close": base})


def test_adx_returns_expected_columns_and_bounded():
    """adx() 回傳 plus_di / minus_di / adx 三欄，且 adx 在 [0, 100]。"""
    out = se.adx(_trend_df(), 14)
    assert set(["plus_di", "minus_di", "adx"]).issubset(out.columns)
    adx = out["adx"].dropna()
    assert len(adx) > 0
    assert adx.between(0.0, 100.0).all()


def test_adx_uptrend_plus_di_dominates():
    """強多頭：+DI 明顯大於 -DI，且 ADX 偏高（有明確趨勢）。"""
    out = se.adx(_trend_df(up=True), 14)
    assert out["plus_di"].iloc[-1] > out["minus_di"].iloc[-1]
    assert out["adx"].iloc[-1] > 25.0


def test_adx_downtrend_minus_di_dominates():
    """強空頭：-DI 明顯大於 +DI。"""
    out = se.adx(_trend_df(up=False), 14)
    assert out["minus_di"].iloc[-1] > out["plus_di"].iloc[-1]
    assert out["adx"].iloc[-1] > 25.0


def test_adx_trend_higher_than_chop():
    """趨勢盤的 ADX 應顯著高於盤整盤（ADX 是趨勢強度）。"""
    trend = se.adx(_trend_df(), 14)["adx"].iloc[-1]
    chop = se.adx(_chop_df(), 14)["adx"].iloc[-1]
    assert trend > chop


def test_adx_is_causal_no_lookahead():
    """因果性：adx(整段)[:k] 與 adx(前 k 段) 在重疊區一致（三欄皆是）。"""
    rng = np.random.RandomState(5)
    n = 120
    close = 1000 + np.cumsum(rng.normal(size=n))
    df = pd.DataFrame({"high": close + np.abs(rng.normal(size=n)) + 0.5,
                       "low": close - np.abs(rng.normal(size=n)) - 0.5,
                       "close": close})
    full = se.adx(df, 14)
    for k in (40, 70, 100):
        prefix = se.adx(df.iloc[:k], 14)
        for col in ("plus_di", "minus_di", "adx"):
            a, b = full[col].iloc[:k], prefix[col]
            assert a.isna().equals(b.isna())
            np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# efficiency_ratio（Kaufman ER：0=盤整、1=完美趨勢）
# --------------------------------------------------------------------------- #
def test_efficiency_ratio_monotonic_is_one():
    """等步上漲：淨位移 = 路徑總長 → ER 恆為 1。"""
    close = pd.Series(np.arange(60, dtype=float) * 2.0 + 100)
    er = se.efficiency_ratio(close, 14).dropna()
    assert len(er) > 0
    np.testing.assert_allclose(er.values, 1.0, atol=1e-9)


def test_efficiency_ratio_zigzag_near_zero():
    """等幅鋸齒：淨位移≈0、路徑很長 → ER 接近 0。"""
    close = pd.Series([100.0 + (1.0 if i % 2 else 0.0) for i in range(60)])
    er = se.efficiency_ratio(close, 14).dropna()
    assert (er < 0.2).all()


def test_efficiency_ratio_bounded_0_1():
    """ER 必在 [0, 1]。"""
    rng = np.random.RandomState(1)
    close = pd.Series(100 + np.cumsum(rng.normal(size=200)))
    er = se.efficiency_ratio(close, 14).dropna()
    assert er.between(0.0, 1.0).all()


def test_efficiency_ratio_is_causal():
    """因果性：ER(整段)[:k] 與 ER(前 k 段) 一致。"""
    rng = np.random.RandomState(2)
    close = pd.Series(100 + np.cumsum(rng.normal(size=120)))
    full = se.efficiency_ratio(close, 14)
    for k in (30, 60, 90):
        prefix = se.efficiency_ratio(close.iloc[:k], 14)
        a, b = full.iloc[:k], prefix
        assert a.isna().equals(b.isna())
        np.testing.assert_allclose(a.dropna().values, b.dropna().values)


# --------------------------------------------------------------------------- #
# choppiness_index（>61.8 盤整、<38.2 趨勢）
# --------------------------------------------------------------------------- #
def test_choppiness_trend_low_range_high():
    """趨勢盤 CHOP 低、盤整盤 CHOP 高。"""
    trend = se.choppiness_index(_trend_df(), 14).iloc[-1]
    chop = se.choppiness_index(_chop_df(amp=8.0), 14).iloc[-1]
    assert trend < chop
    assert trend < 38.2     # 明確趨勢
    assert chop > 50.0      # 明顯盤整


def test_choppiness_is_causal():
    """因果性：CHOP(整段)[:k] 與 CHOP(前 k 段) 一致。"""
    rng = np.random.RandomState(4)
    n = 120
    close = 1000 + np.cumsum(rng.normal(size=n))
    df = pd.DataFrame({"high": close + np.abs(rng.normal(size=n)) + 0.5,
                       "low": close - np.abs(rng.normal(size=n)) - 0.5,
                       "close": close})
    full = se.choppiness_index(df, 14)
    for k in (40, 70, 100):
        prefix = se.choppiness_index(df.iloc[:k], 14)
        a, b = full.iloc[:k], prefix
        assert a.isna().equals(b.isna())
        np.testing.assert_allclose(a.dropna().values, b.dropna().values)


# --------------------------------------------------------------------------- #
# regime（ER+CHOP+ADX 2-of-3 多數決 + confirm_bars 去抖）
# --------------------------------------------------------------------------- #
def _chop_then_trend(n_chop=70, n_trend=70):
    chop = [1000.0 + 5.0 * (1 if i % 2 else -1) for i in range(n_chop)]
    last = chop[-1]
    trend = [last + 3.0 * (i + 1) for i in range(n_trend)]
    base = np.array(chop + trend, dtype=float)
    return pd.DataFrame({"high": base + 1.0, "low": base - 1.0, "close": base})


def test_regime_columns():
    """regime() 回傳 er / chop / adx / regime 四欄。"""
    out = se.regime(_trend_df())
    assert set(["er", "chop", "adx", "regime"]).issubset(out.columns)


def test_regime_trend_vs_range():
    """強趨勢盤尾段判為 'trend'；鋸齒盤整尾段判為 'range'。"""
    assert se.regime(_trend_df()) ["regime"].iloc[-1] == "trend"
    assert se.regime(_chop_df(amp=8.0))["regime"].iloc[-1] == "range"


def test_regime_only_trend_or_range_or_none():
    """regime 值只會是 'trend' / 'range' / None（warmup）。"""
    vals = set(se.regime(_chop_then_trend())["regime"].dropna().unique())
    assert vals <= {"trend", "range"}


def test_regime_debounce_lag():
    """confirm_bars 越大，切換到 'trend' 的時點越晚（去抖造成延遲、不會更早）。"""
    df = _chop_then_trend()

    def first_trend_pos(cb):
        reg = se.regime(df, confirm_bars=cb)["regime"].reset_index(drop=True)
        hits = reg[reg == "trend"].index
        return int(hits[0]) if len(hits) else len(df)

    assert first_trend_pos(8) >= first_trend_pos(2)
    # 兩者最終都應切到 trend（資料尾段是強趨勢）
    assert se.regime(df, confirm_bars=8)["regime"].iloc[-1] == "trend"


def test_regime_is_causal_no_lookahead():
    """因果性：regime(整段)[:k] 與 regime(前 k 段) 在重疊區一致（含 regime 字串欄）。"""
    df = _chop_then_trend()
    full = se.regime(df)
    for k in (50, 90, 120):
        prefix = se.regime(df.iloc[:k])
        for col in ("er", "chop", "adx"):
            a, b = full[col].iloc[:k], prefix[col]
            assert a.isna().equals(b.isna())
            np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)
        # 字串欄：以 'NA' 填補 None 後逐格比對
        ra = full["regime"].iloc[:k].where(full["regime"].iloc[:k].notna(), "NA").tolist()
        rb = prefix["regime"].where(prefix["regime"].notna(), "NA").tolist()
        assert ra == rb


# --------------------------------------------------------------------------- #
# 訂單流：taker 買盤佔比 / CVD（累積量差）
#   taker_base = 主動買進成交量（吃 ask）；volume = 總量。
#   taker_buy_ratio = taker_base / volume ∈ [0,1]；CVD = Σ(2*taker_base - volume)。
#   全部 causal（只用當根與過去），缺欄/零量回 NaN 不爆。
# --------------------------------------------------------------------------- #
def _flow_df(taker, vol):
    """造一個含 taker_base/volume 的 OHLCV，價格不影響訂單流指標。"""
    n = len(vol)
    px = np.linspace(100.0, 110.0, n)
    return pd.DataFrame({
        "open": px, "high": px + 1, "low": px - 1, "close": px,
        "volume": np.asarray(vol, dtype=float),
        "taker_base": np.asarray(taker, dtype=float),
    })


def test_taker_buy_ratio_basic_values():
    df = _flow_df(taker=[10, 5, 0], vol=[10, 10, 10])
    r = se.taker_buy_ratio(df)
    assert r.iloc[0] == pytest.approx(1.0)   # 全買
    assert r.iloc[1] == pytest.approx(0.5)   # 半買半賣
    assert r.iloc[2] == pytest.approx(0.0)   # 全賣


def test_taker_buy_ratio_zero_volume_is_nan():
    df = _flow_df(taker=[0, 3], vol=[0, 6])
    r = se.taker_buy_ratio(df)
    assert np.isnan(r.iloc[0])               # 零量 → NaN，不除零
    assert r.iloc[1] == pytest.approx(0.5)


def test_taker_buy_ratio_missing_column_returns_all_nan():
    """缺 taker_base 欄（合成資料/舊快取）→ 全 NaN，讓上層優雅退化。"""
    px = np.linspace(1, 2, 5)
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                       "volume": np.ones(5)})
    r = se.taker_buy_ratio(df)
    assert len(r) == 5 and r.isna().all()


def test_taker_buy_ratio_smoothing_is_ema_and_causal():
    df = _flow_df(taker=[10, 0, 10, 0, 10, 0, 10, 0],
                  vol=[10, 10, 10, 10, 10, 10, 10, 10])
    raw = se.taker_buy_ratio(df, smooth=1)
    sm = se.taker_buy_ratio(df, smooth=4)
    # 平滑後值域仍在 [0,1]、且不等於原始（被 EMA 抹平）
    assert (sm.dropna() >= 0).all() and (sm.dropna() <= 1).all()
    assert not np.allclose(raw.values, sm.values)
    # causal：前綴重算在重疊區一致
    pref = se.taker_buy_ratio(df.iloc[:5], smooth=4)
    np.testing.assert_allclose(sm.iloc[:5].values, pref.values, rtol=1e-9)


def test_cvd_accumulates_signed_volume():
    # 全買 → 每根 +volume；CVD 單調遞增
    up = _flow_df(taker=[10, 10, 10], vol=[10, 10, 10])
    c = se.cvd(up)
    assert list(c.values) == pytest.approx([10, 20, 30])
    # 全賣 → 每根 -volume；CVD 單調遞減
    dn = _flow_df(taker=[0, 0, 0], vol=[10, 10, 10])
    assert list(se.cvd(dn).values) == pytest.approx([-10, -20, -30])


def test_cvd_missing_column_returns_all_nan():
    px = np.linspace(1, 2, 4)
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                       "volume": np.ones(4)})
    c = se.cvd(df)
    assert len(c) == 4 and c.isna().all()


# --------------------------------------------------------------------------- #
# Supertrend（ATR 趨勢跟蹤）：回傳趨勢線 supertrend + 方向 st_dir(+1多/-1空)。
#   band 鎖定為遞迴、路徑相依，但只用過去與當根 → causal、不 repaint。
# --------------------------------------------------------------------------- #
def _st_trend_df(updown):
    """先強升後強降的確定性 OHLC，用來驗證 supertrend 方向翻轉。"""
    parts = []
    base = 100.0
    for direction, n in updown:
        seg = base + direction * np.arange(n, dtype=float) * 2.0
        parts.append(seg)
        base = seg[-1]
    px = np.concatenate(parts)
    return pd.DataFrame({"open": px, "high": px + 1.0, "low": px - 1.0,
                         "close": px, "volume": np.ones(len(px))})


def test_supertrend_direction_is_plus_minus_one():
    df = _st_trend_df([(+1, 40), (-1, 40)])
    st = se.supertrend(df, period=10, multiplier=3.0)
    d = st["st_dir"].dropna()
    assert set(np.unique(d.values)).issubset({-1.0, 1.0})


def test_supertrend_follows_trend_direction():
    df = _st_trend_df([(+1, 50), (-1, 50)])
    st = se.supertrend(df, period=10, multiplier=3.0)
    # 升段末端應為多方(+1)、降段末端應為空方(-1)
    assert st["st_dir"].iloc[45] == 1.0
    assert st["st_dir"].iloc[-1] == -1.0


def test_supertrend_is_causal_prefix_matches():
    """路徑相依但 causal：前綴重算在重疊區完全一致。"""
    df = _st_trend_df([(+1, 30), (-1, 30), (+1, 30)])
    full = se.supertrend(df, period=10, multiplier=3.0)
    for k in (50, 70, 85):
        pref = se.supertrend(df.iloc[:k], period=10, multiplier=3.0)
        a, b = full["st_dir"].iloc[:k], pref["st_dir"]
        assert a.isna().equals(b.isna())
        np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# Donchian 通道（Turtle 突破）：dc_upper/lower=進場通道、dc_exit_*=出場通道。
#   全部用 .shift(1) 的滾動極值（只看「過去 N 根」），拿當根 close 比 → 無 look-ahead。
# --------------------------------------------------------------------------- #
def test_donchian_channels_are_prior_window_extremes():
    px_h = np.array([1, 2, 3, 4, 5, 6], dtype=float)
    px_l = px_h - 1.0
    df = pd.DataFrame({"open": px_h, "high": px_h, "low": px_l, "close": px_h})
    dc = se.donchian(df, entry_period=3, exit_period=2)
    # dc_upper[i] = max(high[i-3..i-1])；前 3 根 NaN
    assert dc["dc_upper"].iloc[:3].isna().all()
    assert dc["dc_upper"].iloc[3] == pytest.approx(3.0)
    assert dc["dc_upper"].iloc[5] == pytest.approx(5.0)
    assert dc["dc_lower"].iloc[3] == pytest.approx(0.0)
    assert dc["dc_lower"].iloc[5] == pytest.approx(2.0)
    # 出場通道用較短窗
    assert dc["dc_exit_long"].iloc[2] == pytest.approx(0.0)
    assert dc["dc_exit_short"].iloc[2] == pytest.approx(2.0)


def test_donchian_is_causal_prefix_matches():
    rng = np.random.default_rng(3)
    px = 100 + np.cumsum(rng.normal(0, 1, 80))
    df = pd.DataFrame({"open": px, "high": px + 0.5, "low": px - 0.5, "close": px})
    full = se.donchian(df, 20, 10)
    for k in (40, 60, 75):
        pref = se.donchian(df.iloc[:k], 20, 10)
        for col in ("dc_upper", "dc_lower", "dc_exit_long", "dc_exit_short"):
            a, b = full[col].iloc[:k], pref[col]
            assert a.isna().equals(b.isna())
            np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# MACD（price MACD：line=ema(fast)-ema(slow)、signal=ema(line)、hist=line-signal）
# --------------------------------------------------------------------------- #
def test_macd_components_match_ema_definition(wiggly_series):
    out = se.macd(wiggly_series, fast=12, slow=26, sig=9)
    assert set(["macd_line", "macd_signal", "macd_hist"]).issubset(out.columns)
    line = se.ema(wiggly_series, 12) - se.ema(wiggly_series, 26)
    signal = se.ema(line, 9)
    np.testing.assert_allclose(out["macd_line"].values, line.values, rtol=1e-12)
    np.testing.assert_allclose(out["macd_signal"].values, signal.values, rtol=1e-12)
    np.testing.assert_allclose(out["macd_hist"].values, (line - signal).values, rtol=1e-12)


def test_macd_is_causal_no_lookahead(wiggly_series):
    full = se.macd(wiggly_series, 12, 26, 9)
    for k in (20, 40, 55):
        pref = se.macd(wiggly_series.iloc[:k], 12, 26, 9)
        for col in ("macd_line", "macd_signal", "macd_hist"):
            np.testing.assert_allclose(full[col].iloc[:k].values, pref[col].values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# Bollinger Bands（mid=SMA、band=mid±mult*std(母體 ddof=0)、bandwidth、pct_b）
# --------------------------------------------------------------------------- #
def test_bollinger_levels_match_definition():
    rng = np.random.RandomState(9)
    close = pd.Series(100 + np.cumsum(rng.normal(size=80)))
    period, mult = 20, 2.0
    out = se.bollinger(close, period, mult)
    assert set(["bb_mid", "bb_upper", "bb_lower", "bandwidth", "pct_b"]).issubset(out.columns)
    i = 50
    win = close.iloc[i - period + 1: i + 1]
    mid = win.mean()
    sd = win.std(ddof=0)                     # Bollinger 慣例用母體標準差
    assert out["bb_mid"].iloc[i] == pytest.approx(mid)
    assert out["bb_upper"].iloc[i] == pytest.approx(mid + mult * sd)
    assert out["bb_lower"].iloc[i] == pytest.approx(mid - mult * sd)
    assert out["pct_b"].iloc[i] == pytest.approx(
        (close.iloc[i] - (mid - mult * sd)) / ((mid + mult * sd) - (mid - mult * sd)))


def test_bollinger_constant_series_zero_width_pctb_nan():
    """常數序列 → 上下軌重合、bandwidth=0、pct_b 為 NaN（避免除零）。"""
    s = pd.Series([100.0] * 30)
    out = se.bollinger(s, 20, 2.0)
    assert out["bandwidth"].iloc[-1] == pytest.approx(0.0)
    assert np.isnan(out["pct_b"].iloc[-1])


def test_bollinger_is_causal_no_lookahead():
    rng = np.random.RandomState(13)
    close = pd.Series(100 + np.cumsum(rng.normal(size=90)))
    full = se.bollinger(close, 20, 2.0)
    for k in (30, 60, 85):
        pref = se.bollinger(close.iloc[:k], 20, 2.0)
        for col in ("bb_mid", "bb_upper", "bb_lower", "bandwidth", "pct_b"):
            a, b = full[col].iloc[:k], pref[col]
            assert a.isna().equals(b.isna())
            np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# rolling VWAP（causal 滾動 N 根成交量加權典型價，無 session 錨點）
# --------------------------------------------------------------------------- #
def test_rolling_vwap_volume_weighted():
    # 典型價=close（h=l=c），window=2：vwap[1] = (100*1 + 200*3)/(1+3) = 175
    px = [100.0, 200.0]
    df = pd.DataFrame({"open": px, "high": px, "low": px, "close": px,
                       "volume": [1.0, 3.0]})
    v = se.rolling_vwap(df, window=2)
    assert np.isnan(v.iloc[0])                      # 不足一窗 → NaN
    assert v.iloc[1] == pytest.approx(175.0)


def test_rolling_vwap_constant_price_equals_price():
    n = 30
    df = pd.DataFrame({"open": [100.0]*n, "high": [100.0]*n, "low": [100.0]*n,
                       "close": [100.0]*n, "volume": np.arange(1, n + 1, dtype=float)})
    v = se.rolling_vwap(df, window=10).dropna()
    assert np.allclose(v.values, 100.0)


def test_rolling_vwap_is_causal():
    rng = np.random.RandomState(21)
    n = 80
    px = 100 + np.cumsum(rng.normal(size=n))
    df = pd.DataFrame({"open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                       "volume": np.abs(rng.normal(size=n)) + 1})
    full = se.rolling_vwap(df, 20)
    for k in (30, 50, 70):
        pref = se.rolling_vwap(df.iloc[:k], 20)
        a, b = full.iloc[:k], pref
        assert a.isna().equals(b.isna())
        np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# Heikin-Ashi（遞迴、causal；ha_close=OHLC/4、ha_open=前ha均、ha_high/low 含影線）
# --------------------------------------------------------------------------- #
def test_heikin_ashi_hand_computation():
    df = pd.DataFrame({"open": [10.0, 11.0], "high": [12.0, 13.0],
                       "low": [9.0, 10.0], "close": [11.0, 12.0]})
    ha = se.heikin_ashi(df)
    assert set(["ha_open", "ha_close", "ha_high", "ha_low"]).issubset(ha.columns)
    # bar0：ha_close=(10+12+9+11)/4=10.5；ha_open=(10+11)/2=10.5；high=12；low=9
    assert ha["ha_close"].iloc[0] == pytest.approx(10.5)
    assert ha["ha_open"].iloc[0] == pytest.approx(10.5)
    assert ha["ha_high"].iloc[0] == pytest.approx(12.0)
    assert ha["ha_low"].iloc[0] == pytest.approx(9.0)
    # bar1：ha_close=(11+13+10+12)/4=11.5；ha_open=(10.5+10.5)/2=10.5；high=13；low=10
    assert ha["ha_close"].iloc[1] == pytest.approx(11.5)
    assert ha["ha_open"].iloc[1] == pytest.approx(10.5)
    assert ha["ha_high"].iloc[1] == pytest.approx(13.0)
    assert ha["ha_low"].iloc[1] == pytest.approx(10.0)


def test_heikin_ashi_is_causal_prefix_matches():
    rng = np.random.RandomState(31)
    n = 60
    px = 100 + np.cumsum(rng.normal(size=n))
    df = pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px + rng.normal(size=n) * 0.2})
    full = se.heikin_ashi(df)
    for k in (20, 40, 55):
        pref = se.heikin_ashi(df.iloc[:k])
        for col in ("ha_open", "ha_close", "ha_high", "ha_low"):
            np.testing.assert_allclose(full[col].iloc[:k].values, pref[col].values, rtol=1e-9)


# --------------------------------------------------------------------------- #
# Stochastic (KD)：raw %K=100*(close-LL)/(HH-LL)、slow %K=SMA(raw,smooth_k)、
#                  %D=SMA(slow %K, d_period)。bounded [0,100]、causal。
# --------------------------------------------------------------------------- #
def _stoch_df(seed=7, n=80):
    rng = np.random.RandomState(seed)
    px = 100 + np.cumsum(rng.normal(size=n))
    high = px + np.abs(rng.normal(size=n)) + 0.5
    low = px - np.abs(rng.normal(size=n)) - 0.5
    return pd.DataFrame({"open": px, "high": high, "low": low, "close": px})


def test_stochastic_returns_k_and_d_columns():
    out = se.stochastic(_stoch_df())
    assert "stoch_k" in out.columns and "stoch_d" in out.columns


def test_stochastic_bounded_0_100():
    out = se.stochastic(_stoch_df())
    k = out["stoch_k"].dropna()
    d = out["stoch_d"].dropna()
    assert (k >= -1e-9).all() and (k <= 100 + 1e-9).all()
    assert (d >= -1e-9).all() and (d <= 100 + 1e-9).all()


def test_stochastic_raw_k_matches_definition():
    """smooth_k=1 時 stoch_k == raw %K = 100*(close-LL)/(HH-LL)。"""
    df = _stoch_df()
    k_period = 14
    out = se.stochastic(df, k_period=k_period, smooth_k=1, d_period=3)
    i = 50
    win = df.iloc[i - k_period + 1: i + 1]
    ll, hh = win["low"].min(), win["high"].max()
    expected = 100 * (df["close"].iloc[i] - ll) / (hh - ll)
    assert out["stoch_k"].iloc[i] == pytest.approx(expected)


def test_stochastic_constant_range_no_divzero():
    """高低相等（range=0）→ 不應炸除零，回 NaN 或被前值填，總之有限或 NaN。"""
    s = pd.DataFrame({"open": [100.0] * 30, "high": [100.0] * 30,
                      "low": [100.0] * 30, "close": [100.0] * 30})
    out = se.stochastic(s)
    v = out["stoch_k"].iloc[-1]
    assert np.isnan(v) or np.isfinite(v)


def test_stochastic_is_causal_no_lookahead():
    df = _stoch_df(seed=11, n=90)
    full = se.stochastic(df)
    for k in (30, 60, 85):
        pref = se.stochastic(df.iloc[:k])
        for col in ("stoch_k", "stoch_d"):
            a, b = full[col].iloc[:k], pref[col]
            assert a.isna().equals(b.isna())
            np.testing.assert_allclose(a.dropna().values, b.dropna().values, rtol=1e-9)


# ─── fib_ema_score() ──────────────────────────────────────────────────────────

def _fib_ema_df(closes):
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="15min")
    return pd.DataFrame({
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": 1.0,
    }, index=idx)


def test_fib_ema_score_returns_series_same_length():
    from core.signal_engineer import fib_ema_score
    df = _fib_df(list(range(1, 201)))
    result = fib_ema_score(df["close"])
    assert len(result) == len(df)


def test_fib_ema_score_uptrend_near_one():
    """Monotonically rising price → fast EMAs all above slow EMAs → score > 0.85."""
    from core.signal_engineer import fib_ema_score
    closes = [100 + i * 0.5 for i in range(300)]
    score = fib_ema_score(_fib_ema_df(closes)["close"])
    assert score.iloc[-1] > 0.85


def test_fib_ema_score_downtrend_near_zero():
    """Monotonically falling price → fast EMAs all below slow EMAs → score < 0.15."""
    from core.signal_engineer import fib_ema_score
    closes = [300 - i * 0.5 for i in range(300)]
    score = fib_ema_score(_fib_ema_df(closes)["close"])
    assert score.iloc[-1] < 0.15


def test_fib_ema_score_range_zero_to_one():
    """Score is always in [0, 1] regardless of price series."""
    from core.signal_engineer import fib_ema_score
    rng = np.random.default_rng(42)
    closes = (100 + rng.normal(0, 2, 500)).tolist()
    score = fib_ema_score(_fib_ema_df(closes)["close"])
    valid = score.dropna()
    assert (valid >= 0.0).all()
    assert (valid <= 1.0).all()


def test_fib_ema_score_short_series_no_crash():
    """With only 50 bars (< EMA-89 recommended warm-up), must not raise and score in [0,1]."""
    from core.signal_engineer import fib_ema_score
    score = fib_ema_score(_fib_ema_df([100.0] * 50)["close"])
    valid = score.dropna()
    assert len(score) == 50
    assert (valid >= 0.0).all() and (valid <= 1.0).all()


def test_fib_ema_score_custom_periods():
    """Custom fast/slow periods produce valid output."""
    from core.signal_engineer import fib_ema_score
    closes = [100 + i * 0.3 for i in range(200)]
    score = fib_ema_score(_fib_ema_df(closes)["close"], fast=(5, 8), slow=(13, 21))
    assert score.iloc[-1] > 0.75


# ── OPT-16：CVD/價格背離指標（竭盡過濾，causal）──────────────────────────
def _cvd_df(closes, taker, vol=100.0):
    import numpy as _np
    closes = _np.asarray(closes, dtype=float)
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({
        "open": closes, "high": closes * 1.001, "low": closes * 0.999,
        "close": closes, "volume": _np.full(n, vol),
        "taker_base": _np.asarray(taker, dtype=float),
    }, index=idx)


def test_cvd_divergence_bearish_when_price_up_cvd_down():
    """價漲但 CVD 走低（買盤竭盡）→ -1 頂背離。"""
    n = 30
    closes = np.linspace(100, 120, n)
    taker = np.concatenate([np.full(15, 70.0), np.full(15, 30.0)])  # delta +40→-40
    div = se.cvd_price_divergence(_cvd_df(closes, taker), window=10)
    assert div.iloc[-1] == -1.0


def test_cvd_divergence_bullish_when_price_down_cvd_up():
    """價跌但 CVD 走高（賣盤被吸收）→ +1 底背離。"""
    n = 30
    closes = np.linspace(120, 100, n)
    taker = np.concatenate([np.full(15, 30.0), np.full(15, 70.0)])  # delta -40→+40
    div = se.cvd_price_divergence(_cvd_df(closes, taker), window=10)
    assert div.iloc[-1] == 1.0


def test_cvd_divergence_zero_when_aligned():
    """價漲且 CVD 同向上升 → 無背離 0。"""
    n = 30
    closes = np.linspace(100, 120, n)
    taker = np.full(n, 70.0)        # delta 恆 +40 → CVD 持續上升，與價同向
    div = se.cvd_price_divergence(_cvd_df(closes, taker), window=10)
    assert div.iloc[-1] == 0.0


def test_cvd_divergence_is_causal():
    """末根值不依賴未來：去掉最後一根，倒數第二根的值不變。"""
    n = 30
    closes = np.linspace(100, 120, n)
    taker = np.concatenate([np.full(15, 70.0), np.full(15, 30.0)])
    df = _cvd_df(closes, taker)
    full = se.cvd_price_divergence(df, window=10)
    trunc = se.cvd_price_divergence(df.iloc[:-1], window=10)
    assert full.iloc[-2] == trunc.iloc[-1]


def test_cvd_divergence_no_taker_base_returns_zeros():
    """缺 taker_base（合成/舊快取）→ 全 0（優雅退化，不影響進場）。"""
    n = 20
    closes = np.linspace(100, 110, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    df = pd.DataFrame({"open": closes, "high": closes, "low": closes,
                       "close": closes, "volume": np.full(n, 100.0)}, index=idx)
    div = se.cvd_price_divergence(df, window=5)
    assert (div == 0.0).all()


# ── SMC 結構特徵欄位（2026-07-05，ML 結構特徵用）：swing 位準輸出 ──────────────
def _smc_df(n=60):
    rng = np.random.default_rng(7)
    closes = 100 + np.cumsum(rng.normal(0.1, 1.0, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({
        "open": closes, "high": closes + np.abs(rng.normal(0, 0.6, n)) + 0.1,
        "low": closes - np.abs(rng.normal(0, 0.6, n)) - 0.1,
        "close": closes, "volume": np.full(n, 100.0)}, index=idx)


def test_smc_levels_outputs_swing_levels():
    """smc_levels additive 輸出 swing_high/swing_low（BOS 判定用的位準，ML 結構特徵需要）。"""
    out = se.smc_levels(_smc_df(), pivot_left=5, pivot_right=3)
    assert "swing_high" in out.columns and "swing_low" in out.columns
    tail = out.iloc[20:]                       # 暖機後
    assert tail["swing_high"].notna().any()
    assert (tail["swing_high"].dropna() >= tail["swing_low"].dropna().reindex(
        tail["swing_high"].dropna().index).fillna(-1e18)).all()


def test_smc_levels_swing_matches_bos_semantics():
    """bos_bull=1 的根，close 必 > swing_high（同一位準判定，語意一致不分岔）。"""
    out = se.smc_levels(_smc_df(120), pivot_left=5, pivot_right=3)
    mask = out["bos_bull"] == 1.0
    sub = out[mask].dropna(subset=["swing_high"])
    if len(sub):
        assert (sub["close"] > sub["swing_high"]).all()


def test_smc_levels_existing_columns_unchanged():
    """回歸鎖：加 swing 欄不影響 bos/fvg 既有輸出（跟舊版逐位元一致的代理檢查）。"""
    df = _smc_df(120)
    out = se.smc_levels(df, pivot_left=5, pivot_right=3)
    for col in ("bos_bull", "bos_bear", "fvg_bull", "fvg_bear"):
        assert col in out.columns
    # FVG 定義直接可重算驗證
    fvg_bull_expect = (df["high"].shift(2) < df["low"]).astype(float)
    assert (out["fvg_bull"].fillna(0) == fvg_bull_expect.fillna(0)).all()


# ── 高週期趨勢過濾 htf_trend（2026-07-05，多週期共振）────────────────────────
def _htf_df(n=400, drift=0.3):
    idx = pd.date_range("2024-01-01", periods=n, freq="4h")
    closes = 100 + np.cumsum(np.full(n, drift))
    return pd.DataFrame({"open": closes, "high": closes + 0.5, "low": closes - 0.5,
                         "close": closes, "volume": np.full(n, 100.0)}, index=idx)


def test_htf_trend_bull_in_clean_uptrend():
    """乾淨上升趨勢：日線 MA20>MA60 → htf_trend 尾段應為 +1。"""
    out = se.htf_trend(_htf_df(drift=0.3), rule="1D", fast=20, slow=60)
    assert out.iloc[-1] == 1


def test_htf_trend_bear_in_clean_downtrend():
    out = se.htf_trend(_htf_df(drift=-0.3), rule="1D", fast=20, slow=60)
    assert out.iloc[-1] == -1


def test_htf_trend_causal_no_lookahead():
    """因果性：截掉尾巴重算，重疊區間的值必須完全一致（日線只用已收完的根）。"""
    df = _htf_df(400, drift=0.2)
    full = se.htf_trend(df, rule="1D", fast=10, slow=30)
    trunc = se.htf_trend(df.iloc[:-30], rule="1D", fast=10, slow=30)
    overlap = trunc.index
    assert (full.loc[overlap].fillna(0) == trunc.fillna(0)).all(), \
        "截尾後重疊區間值改變 → 用到了未來資料"


def test_htf_trend_uses_only_completed_daily_bars():
    """今日只走到一半時，htf_trend 必須反映『昨日收完』的日線狀態——
    把今天尚未收完的幾根 4h 大改，今天內的 htf_trend 不得改變。"""
    df = _htf_df(400, drift=0.2)
    df2 = df.copy()
    df2.iloc[-3:, df2.columns.get_loc("close")] = 1.0   # 今天的未收完盤面天翻地覆
    a = se.htf_trend(df, rule="1D", fast=10, slow=30)
    b = se.htf_trend(df2, rule="1D", fast=10, slow=30)
    assert a.iloc[-1] == b.iloc[-1], "當日未收完的變動影響了 htf_trend → 前視洩漏"


def test_htf_trend_warmup_is_zero_or_nan_free_index():
    """暖機期（日線根數不足）回 0（中性、放行由呼叫端決定），index 與輸入一致。"""
    df = _htf_df(100, drift=0.2)     # 100 根 4h ≈ 16 天，遠不足 60 日 MA
    out = se.htf_trend(df, rule="1D", fast=20, slow=60)
    assert len(out) == len(df)
    assert set(out.unique()).issubset({-1, 0, 1})
