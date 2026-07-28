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


# ── 現價附註（防前視前提下補上即時性）─────────────────────────
def test_live_price_defaults_to_none_when_not_provided():
    """不傳現價時仍可正常產生簡報——排程抓現價失敗不該讓整輪掛掉。"""
    b = build_market_briefing(make_df(), "BTCUSDT", "4h")
    assert b.live_price is None


def test_live_price_is_recorded_when_provided():
    b = build_market_briefing(make_df(), "BTCUSDT", "4h", live_price=63205.7)
    assert b.live_price == 63205.7


def test_live_price_does_not_alter_any_indicator():
    """現價只是附註，絕不可污染指標（否則等於用未收盤資料算，破壞防前視）。"""
    df = make_df()
    without = build_market_briefing(df, "BTCUSDT", "4h")
    with_live = build_market_briefing(df, "BTCUSDT", "4h", live_price=999999.0)
    for name in ("close", "ema_fast", "ema_slow", "rsi", "atr", "zscore",
                 "fib_pos", "fib_382", "fib_618", "htf_trend", "recent_closes"):
        assert getattr(without, name) == getattr(with_live, name), f"{name} 被現價污染"


def test_format_shows_drift_from_closed_bar():
    """簡報要讓 AI 看到「收盤後價格已經走掉多少」。"""
    df = make_df()
    closed = float(df["close"].iloc[-1])
    text = format_briefing(build_market_briefing(
        df, "BTCUSDT", "4h", live_price=closed * 1.01))
    assert "現價" in text
    assert "+1.0" in text or "1.00%" in text or "+1.00" in text


def test_format_omits_live_price_section_when_absent():
    text = format_briefing(build_market_briefing(make_df(), "BTCUSDT", "4h"))
    assert "現價" not in text
