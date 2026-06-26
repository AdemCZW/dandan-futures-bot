"""TrendPullbackStrategy — 趨勢過濾 + 回踩進場 + KD 觸發。

設計（使用者指定）：
  主方向：200EMA 之上只做多、之下只做空（過濾逆勢假突破）。
  進場（順勢回踩，不追極端）：
    多 = 價 > 200EMA、EMA20 > EMA50、RSI 在回踩區 [rsi_lo, rsi_hi]、KD 黃金交叉。
    空 = 鏡像（價 < 200EMA、EMA20 < EMA50、RSI 在區間、KD 死叉）。
  出場：趨勢翻轉（價跨越 200EMA）或動能翻轉（EMA20/50 反向）。ATR 停損由 risk 層處理。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import build_strategy


def _strat(params=None):
    p = {}
    if params:
        p.update(params)
    return build_strategy("trend_pullback", **p)


def _row(close=100.0, ema_t=90.0, ema_f=101.0, ema_s=100.0, rsi=50.0,
         kd_gold=0.0, kd_dead=0.0, regime="trend"):
    """signal() 所需最小 row（多頭預設：價在 200EMA 上、EMA20>50）。"""
    return {
        "close": close, "ema_t": ema_t, "ema_f": ema_f, "ema_s": ema_s,
        "rsi": rsi, "kd_gold": kd_gold, "kd_dead": kd_dead,
        "atr": 1.0, "regime": regime,
    }


def _ohlcv(n=300, trend="up", seed=1):
    rng = np.random.RandomState(seed)
    if trend == "up":
        base = 100 + np.arange(n) * 0.4
    elif trend == "down":
        base = 220 - np.arange(n) * 0.4
    else:
        base = 150 + np.zeros(n)
    close = base + np.cumsum(rng.normal(0, 0.3, n))
    high = close + np.abs(rng.normal(0, 0.6, n)) + 0.3
    low = close - np.abs(rng.normal(0, 0.6, n)) - 0.3
    return pd.DataFrame({"open": close, "high": high, "low": low,
                         "close": close, "volume": np.ones(n) * 1000})


# ── prepare：欄位齊備 ────────────────────────────────────────────────────────

class TestPrepare:
    def test_prepare_adds_all_columns(self):
        df = _strat().prepare(_ohlcv())
        for col in ("ema_t", "ema_f", "ema_s", "rsi", "stoch_k", "stoch_d",
                    "kd_gold", "kd_dead", "atr"):
            assert col in df.columns, f"缺欄位 {col}"

    def test_kd_gold_dead_are_causal_cross_flags(self):
        """kd_gold/kd_dead 為 0/1，且不可同一根同時為 1。"""
        df = _strat().prepare(_ohlcv(trend="flat", seed=3)).dropna()
        assert set(np.unique(df["kd_gold"].values)).issubset({0.0, 1.0})
        assert ((df["kd_gold"] > 0.5) & (df["kd_dead"] > 0.5)).sum() == 0


# ── 進場：四條件 AND ─────────────────────────────────────────────────────────

class TestEntry:
    def test_long_when_all_long_conditions_met(self):
        row = _row(close=110, ema_t=100, ema_f=106, ema_s=104, rsi=50, kd_gold=1.0)
        assert _strat().signal(row, 0) == 1

    def test_no_long_when_below_200ema(self):
        """價在 200EMA 之下，即使其餘條件滿足也不做多。"""
        row = _row(close=95, ema_t=100, ema_f=106, ema_s=104, rsi=50, kd_gold=1.0)
        assert _strat().signal(row, 0) == 0

    def test_no_long_without_kd_gold(self):
        """沒有 KD 黃金交叉（觸發鍵）→ 不進場。"""
        row = _row(close=110, ema_t=100, ema_f=106, ema_s=104, rsi=50, kd_gold=0.0)
        assert _strat().signal(row, 0) == 0

    def test_no_long_when_rsi_overbought(self):
        """RSI 過熱（>hi）→ 不追高。"""
        row = _row(close=110, ema_t=100, ema_f=106, ema_s=104, rsi=75, kd_gold=1.0)
        assert _strat().signal(row, 0) == 0

    def test_no_long_when_ema_fast_below_slow(self):
        """EMA20 < EMA50（短線動能不向上）→ 不做多。"""
        row = _row(close=110, ema_t=100, ema_f=103, ema_s=105, rsi=50, kd_gold=1.0)
        assert _strat().signal(row, 0) == 0

    def test_short_when_all_short_conditions_met(self):
        row = _row(close=90, ema_t=100, ema_f=94, ema_s=96, rsi=50, kd_dead=1.0)
        assert _strat().signal(row, 0) == -1

    def test_no_short_above_200ema(self):
        row = _row(close=110, ema_t=100, ema_f=94, ema_s=96, rsi=50, kd_dead=1.0)
        assert _strat().signal(row, 0) == 0

    def test_no_short_without_kd_dead(self):
        row = _row(close=90, ema_t=100, ema_f=94, ema_s=96, rsi=50, kd_dead=0.0)
        assert _strat().signal(row, 0) == 0


# ── 出場：趨勢/動能翻轉 ──────────────────────────────────────────────────────

class TestExit:
    def test_long_exits_when_price_below_200ema(self):
        row = _row(close=95, ema_t=100, ema_f=106, ema_s=104)
        assert _strat().signal(row, 1) == 0

    def test_long_exits_when_ema_fast_below_slow(self):
        row = _row(close=110, ema_t=100, ema_f=103, ema_s=105)
        assert _strat().signal(row, 1) == 0

    def test_long_holds_while_trend_intact(self):
        row = _row(close=110, ema_t=100, ema_f=106, ema_s=104)
        assert _strat().signal(row, 1) == 1

    def test_short_exits_when_price_above_200ema(self):
        row = _row(close=110, ema_t=100, ema_f=94, ema_s=96)
        assert _strat().signal(row, -1) == 0

    def test_short_holds_while_downtrend_intact(self):
        row = _row(close=90, ema_t=100, ema_f=94, ema_s=96)
        assert _strat().signal(row, -1) == -1


# ── 整合：prepare→signal 全程跑得動、允許做空 ────────────────────────────────

class TestIntegration:
    def test_allow_short_flag(self):
        assert _strat().allow_short is True

    def test_full_pipeline_runs(self):
        strat = _strat()
        df = strat.prepare(_ohlcv(n=300, trend="up")).dropna()
        assert len(df) > 50
        pos = 0
        for _, r in df.iterrows():
            pos = strat.signal(r, pos)
            assert pos in (-1, 0, 1)
