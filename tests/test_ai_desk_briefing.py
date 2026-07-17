"""ai_desk.briefing 測試 — 合成資料驗證簡報欄位與 NaN 防護，不碰網路。"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.briefing import MarketBriefing, build_market_briefing, format_briefing


def make_df(n=300, seed=7):
    """合成 4h OHLCV 隨機漫步，DatetimeIndex。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = rng.uniform(100, 200, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_build_briefing_fields():
    df = make_df()
    b = build_market_briefing(df, "BTCUSDT", "4h")
    assert isinstance(b, MarketBriefing)
    assert b.symbol == "BTCUSDT" and b.interval == "4h"
    assert b.close == pytest.approx(float(df["close"].iloc[-1]))
    assert b.as_of == str(df.index[-1])
    # 指標皆為有限 float
    for v in (b.ema_fast, b.ema_slow, b.rsi, b.atr, b.zscore, b.fib_pos):
        assert np.isfinite(v)
    assert 0.0 <= b.rsi <= 100.0
    assert b.htf_trend in (-1, 0, 1)
    assert len(b.recent_closes) == 6


def test_build_briefing_rejects_short_data():
    df = make_df(n=100)
    with pytest.raises(ValueError, match="不足"):
        build_market_briefing(df, "BTCUSDT", "4h")


def test_format_briefing_contains_facts():
    df = make_df()
    b = build_market_briefing(df, "BTCUSDT", "4h")
    text = format_briefing(b)
    assert "BTCUSDT" in text and "4h" in text
    assert f"{b.close:.2f}" in text
    assert "RSI" in text
