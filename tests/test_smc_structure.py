"""SMCStructureStrategy — EMA 趨勢過濾測試。

問題背景：
  BOS（結構突破）是動能信號，price > swing_high 就觸發多單。
  若 EMA 是空頭排列（EMA20 < EMA50），此類多單是逆趨勢假突破，
  導致 Bot3 在 ETH $1,653 頂部進多單，SL 吃掉 $105。

修法：signal() 進場時加 EMA 方向驗證：
  - bos_bull 進多單：require ema_fast > ema_slow
  - bos_bear 進空單：require ema_fast < ema_slow
  出場邏輯不受影響。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import build_strategy


# ── 輔助函式 ────────────────────────────────────────────────────────────────

def _strategy(params=None):
    p = {"require_fvg": False}
    if params:
        p.update(params)
    return build_strategy("smc_structure", **p)


def _row(bos_bull=0.0, bos_bear=0.0, fvg_bull=0.0, fvg_bear=0.0,
         ema_fast=100.0, ema_slow=100.0, regime="trend"):
    """建立 signal() 所需的最小 row dict。"""
    return {
        "bos_bull": bos_bull, "bos_bear": bos_bear,
        "fvg_bull": fvg_bull, "fvg_bear": fvg_bear,
        "ema_fast": ema_fast, "ema_slow": ema_slow,
        "regime": regime,
    }


def _ohlcv(n=150, trend="up", seed=42) -> pd.DataFrame:
    """產生測試用 OHLCV（趨勢足夠清晰讓 EMA 分叉）。"""
    np.random.seed(seed)
    noise = np.random.randn(n) * 1.5
    if trend == "up":
        close = 100 + np.arange(n) * 0.8 + noise
    elif trend == "down":
        close = 220 - np.arange(n) * 0.8 + noise
    else:
        close = 150 + noise
    high = close + abs(np.random.randn(n)) * 1.5
    low  = close - abs(np.random.randn(n)) * 1.5
    return pd.DataFrame({
        "open": close - 0.2, "high": high, "low": low, "close": close,
        "volume": np.ones(n) * 1000.0,
    })


# ── prepare() — EMA 欄位 ────────────────────────────────────────────────────

class TestSMCPrepare:
    def test_prepare_adds_ema_fast_and_slow(self):
        """prepare() 應輸出 ema_fast 和 ema_slow 欄位。"""
        strat = _strategy()
        df = strat.prepare(_ohlcv())
        assert "ema_fast" in df.columns, "應有 ema_fast 欄位"
        assert "ema_slow" in df.columns, "應有 ema_slow 欄位"

    def test_ema_fast_leads_slow_in_uptrend(self):
        """上升趨勢尾端：ema_fast 應大於 ema_slow。"""
        strat = _strategy()
        df = strat.prepare(_ohlcv(n=150, trend="up"))
        last = df.iloc[-1]
        assert last["ema_fast"] > last["ema_slow"], \
            "上升趨勢 EMA20 應 > EMA50"

    def test_ema_fast_below_slow_in_downtrend(self):
        """下降趨勢尾端：ema_fast 應小於 ema_slow。"""
        strat = _strategy()
        df = strat.prepare(_ohlcv(n=150, trend="down"))
        last = df.iloc[-1]
        assert last["ema_fast"] < last["ema_slow"], \
            "下降趨勢 EMA20 應 < EMA50"


# ── signal() — 進場 EMA 方向驗證 ───────────────────────────────────────────

class TestSMCEntryEMAFilter:
    """進場信號必須與 EMA 趨勢方向一致。"""

    def test_blocks_bull_entry_when_ema_bearish(self):
        """bos_bull=1 但 EMA 空頭 → 不進多單。"""
        strat = _strategy()
        row = _row(bos_bull=1.0, ema_fast=95.0, ema_slow=100.0)
        assert strat.signal(row, 0) == 0, \
            "EMA 空頭排列時不應進多單，即使有 BOS"

    def test_allows_bull_entry_when_ema_bullish(self):
        """bos_bull=1 且 EMA 多頭 → 進多單。"""
        strat = _strategy()
        row = _row(bos_bull=1.0, ema_fast=105.0, ema_slow=100.0)
        assert strat.signal(row, 0) == 1, \
            "EMA 多頭排列 + BOS 多頭 → 應進多單"

    def test_blocks_bear_entry_when_ema_bullish(self):
        """bos_bear=1 但 EMA 多頭 → 不進空單。"""
        strat = _strategy()
        row = _row(bos_bear=1.0, ema_fast=105.0, ema_slow=100.0)
        assert strat.signal(row, 0) == 0, \
            "EMA 多頭排列時不應進空單，即使有 BOS"

    def test_allows_bear_entry_when_ema_bearish(self):
        """bos_bear=1 且 EMA 空頭 → 進空單。"""
        strat = _strategy()
        row = _row(bos_bear=1.0, ema_fast=95.0, ema_slow=100.0)
        assert strat.signal(row, 0) == -1, \
            "EMA 空頭排列 + BOS 空頭 → 應進空單"

    def test_no_entry_without_bos(self):
        """沒有 BOS → 無論 EMA 方向都不進場。"""
        strat = _strategy()
        row_bull_ema = _row(ema_fast=105.0, ema_slow=100.0)
        row_bear_ema = _row(ema_fast=95.0,  ema_slow=100.0)
        assert strat.signal(row_bull_ema, 0) == 0
        assert strat.signal(row_bear_ema, 0) == 0

    def test_regime_range_blocks_entry_regardless_of_ema(self):
        """regime='range' 時（_regime_ok=False）不進場，即使 EMA 對齊。"""
        strat = _strategy()
        row = _row(bos_bull=1.0, ema_fast=105.0, ema_slow=100.0, regime="range")
        assert strat.signal(row, 0) == 0, \
            "regime 不對應時 _regime_ok 應擋下入場"


class TestSMCEmaFilterToggle:
    """use_ema_filter 開關：可關閉 EMA 方向過濾（回到純 BOS 進場），供 A/B 驗證。"""

    def test_filter_on_is_default(self):
        """預設開啟 EMA 過濾：EMA 空頭時擋下 bos_bull 多單。"""
        strat = _strategy()  # 預設 use_ema_filter=True
        row = _row(bos_bull=1.0, ema_fast=95.0, ema_slow=100.0)
        assert strat.signal(row, 0) == 0

    def test_filter_off_allows_counter_trend_entry(self):
        """關閉過濾：EMA 空頭仍可因 bos_bull 進多單（純 BOS 行為）。"""
        strat = _strategy({"use_ema_filter": False})
        row = _row(bos_bull=1.0, ema_fast=95.0, ema_slow=100.0)
        assert strat.signal(row, 0) == 1, \
            "關閉 EMA 過濾後應回到純 BOS 進場"

    def test_filter_off_bear_entry_regardless_of_ema(self):
        """關閉過濾：EMA 多頭仍可因 bos_bear 進空單。"""
        strat = _strategy({"use_ema_filter": False})
        row = _row(bos_bear=1.0, ema_fast=105.0, ema_slow=100.0)
        assert strat.signal(row, 0) == -1

    def test_filter_off_still_respects_regime(self):
        """關閉 EMA 過濾不影響 regime 閘門：range 仍擋下進場。"""
        strat = _strategy({"use_ema_filter": False})
        row = _row(bos_bull=1.0, ema_fast=95.0, ema_slow=100.0, regime="range")
        assert strat.signal(row, 0) == 0


# ── signal() — 出場邏輯不受 EMA 影響 ───────────────────────────────────────

class TestSMCExitUnchanged:
    """持倉中的出場邏輯（BOS 反向平倉）不受 EMA 過濾影響。"""

    def test_long_exits_on_bos_bear(self):
        """多倉遇 bos_bear → 平倉，與 EMA 無關。"""
        strat = _strategy()
        row = _row(bos_bear=1.0, ema_fast=105.0, ema_slow=100.0)
        assert strat.signal(row, 1) == 0

    def test_short_exits_on_bos_bull(self):
        """空倉遇 bos_bull → 平倉，與 EMA 無關。"""
        strat = _strategy()
        row = _row(bos_bull=1.0, ema_fast=95.0, ema_slow=100.0)
        assert strat.signal(row, -1) == 0

    def test_long_holds_without_reversal(self):
        """多倉無 bos_bear → 繼續持有。"""
        strat = _strategy()
        row = _row(ema_fast=105.0, ema_slow=100.0)
        assert strat.signal(row, 1) == 1

    def test_short_holds_without_reversal(self):
        """空倉無 bos_bull → 繼續持有。"""
        strat = _strategy()
        row = _row(ema_fast=95.0, ema_slow=100.0)
        assert strat.signal(row, -1) == -1
