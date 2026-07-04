"""VolMomentumStrategy — 成交量計時時序動量策略測試。"""
import pandas as pd
import pytest
from core.quant_researcher import build_strategy, STRATEGIES


def test_registered():
    assert "vol_momentum" in STRATEGIES
    assert build_strategy("vol_momentum") is not None


def _row(mom=0.0, vol_ratio=2.0, close=100.0, ema_trend=90.0):
    return {"mom": mom, "vol_ratio": vol_ratio, "close": close, "ema_trend": ema_trend}


def test_long_entry_strong_mom_high_vol_uptrend():
    s = build_strategy("vol_momentum", entry_thresh=0.01, vol_min=1.2)
    # mom +2% > 1%，量 2x ≥1.2，close>ema → 做多
    assert s.signal(_row(mom=0.02, vol_ratio=2.0, close=100, ema_trend=90), 0) == 1


def test_short_entry_strong_negmom_downtrend():
    s = build_strategy("vol_momentum", entry_thresh=0.01, vol_min=1.2)
    assert s.signal(_row(mom=-0.02, vol_ratio=2.0, close=90, ema_trend=100), 0) == -1


def test_no_entry_when_volume_thin():
    """量能不足（vol_ratio < vol_min）→ 不進場（核心差異化）。"""
    s = build_strategy("vol_momentum", entry_thresh=0.01, vol_min=1.5)
    assert s.signal(_row(mom=0.03, vol_ratio=1.0, close=100, ema_trend=90), 0) == 0


def test_no_entry_when_mom_weak():
    s = build_strategy("vol_momentum", entry_thresh=0.02, vol_min=1.2)
    assert s.signal(_row(mom=0.005, vol_ratio=2.0, close=100, ema_trend=90), 0) == 0


def test_trend_filter_blocks_counter_trend_long():
    """強動量但逆大趨勢（close<ema）→ 做多被擋。"""
    s = build_strategy("vol_momentum", entry_thresh=0.01, vol_min=1.2, use_trend_filter=True)
    assert s.signal(_row(mom=0.03, vol_ratio=2.0, close=90, ema_trend=100), 0) == 0


def test_trend_filter_off_allows_counter_trend():
    s = build_strategy("vol_momentum", entry_thresh=0.01, vol_min=1.2, use_trend_filter=False)
    assert s.signal(_row(mom=0.03, vol_ratio=2.0, close=90, ema_trend=100), 0) == 1


def test_exit_long_on_momentum_exhaustion():
    """持多且動量穿回負 → 平倉。"""
    s = build_strategy("vol_momentum")
    assert s.signal(_row(mom=-0.001), 1) == 0


def test_hold_long_while_mom_positive():
    s = build_strategy("vol_momentum")
    assert s.signal(_row(mom=0.005), 1) == 1


def test_exit_short_on_momentum_flip():
    s = build_strategy("vol_momentum")
    assert s.signal(_row(mom=0.001), -1) == 0


def test_warmup_holds_position():
    s = build_strategy("vol_momentum")
    assert s.signal({"mom": float("nan")}, 1) == 1
    assert s.signal({"mom": float("nan")}, 0) == 0


def test_prepare_adds_columns():
    idx = pd.date_range("2026-01-01", periods=250, freq="1h")
    import numpy as np
    close = pd.Series(100 + np.cumsum(np.random.default_rng(0).normal(0, 1, 250)), index=idx)
    df = pd.DataFrame({"open": close, "high": close*1.01, "low": close*0.99,
                       "close": close, "volume": 1000.0}, index=idx)
    out = build_strategy("vol_momentum").prepare(df)
    for c in ("mom", "vol_ratio", "ema_trend"):
        assert c in out.columns
