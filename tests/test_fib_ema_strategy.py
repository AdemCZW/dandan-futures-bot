"""
TDD tests for FibEmaStrategy — Fibonacci EMA alignment strategy.
RED first: all tests fail until strategy is implemented.
"""
import numpy as np
import pandas as pd
import pytest

# ─── helpers ─────────────────────────────────────────────────────────────────

def _ohlcv(closes, atr_mult=0.01):
    """Build a minimal OHLCV DataFrame with realistic high/low from closes."""
    closes = [float(c) for c in closes]
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="15min")
    spread = [c * atr_mult for c in closes]
    return pd.DataFrame({
        "open":   closes,
        "high":   [c + s for c, s in zip(closes, spread)],
        "low":    [c - s for c, s in zip(closes, spread)],
        "close":  closes,
        "volume": [1000.0] * len(closes),
    }, index=idx)


def _make_strategy(**kwargs):
    from core.quant_researcher import build_strategy
    params = {"rsi_period": 14, "rsi_lo": 35.0, "rsi_hi": 65.0,
              "score_bull": 0.67, "score_bear": 0.33}
    params.update(kwargs)
    return build_strategy("fib_ema", **params)


# ─── strategy registry ────────────────────────────────────────────────────────

def test_fib_ema_in_registry():
    from core.quant_researcher import STRATEGIES
    assert "fib_ema" in STRATEGIES


def test_fib_ema_strategy_count_updated():
    from core.quant_researcher import STRATEGIES
    assert len(STRATEGIES) >= 16   # was 15 before adding fib_ema


# ─── prepare() ───────────────────────────────────────────────────────────────

def test_prepare_adds_fib_score_column():
    strat = _make_strategy()
    df = _ohlcv([100 + i * 0.3 for i in range(300)])
    out = strat.prepare(df)
    assert "fib_score" in out.columns


def test_prepare_adds_rsi_column():
    strat = _make_strategy()
    out = strat.prepare(_ohlcv([100 + i * 0.2 for i in range(200)]))
    assert "rsi" in out.columns


def test_prepare_score_in_range():
    strat = _make_strategy()
    out = strat.prepare(_ohlcv([100 + i * 0.3 for i in range(300)]))
    valid = out["fib_score"].dropna()
    assert (valid >= 0.0).all() and (valid <= 1.0).all()


# ─── signal() — no position ──────────────────────────────────────────────────

def test_signal_long_when_fully_bullish():
    """Full bull alignment + RSI in zone → go long."""
    strat = _make_strategy()
    # monotonically rising → fib_score = 1.0, RSI likely in zone after enough bars
    df = _ohlcv([100 + i * 0.4 for i in range(400)])
    out = strat.prepare(df)
    row = out.iloc[-1]
    sig = strat.signal(row, position=0)
    assert sig == 1


def test_signal_short_when_fully_bearish():
    """Full bear alignment → go short."""
    strat = _make_strategy()
    df = _ohlcv([400 - i * 0.4 for i in range(400)])
    out = strat.prepare(df)
    row = out.iloc[-1]
    sig = strat.signal(row, position=0)
    assert sig == -1


def test_signal_not_all_entries_in_choppy():
    """Strategy should not fire signals on every single bar in choppy conditions."""
    strat = _make_strategy()
    rng = np.random.default_rng(7)
    closes = (100 + rng.normal(0, 0.2, 300)).tolist()
    df = _ohlcv(closes)
    out = strat.prepare(df)
    signals = [strat.signal(out.iloc[i], 0) for i in range(150, 300)]
    # at least some bars should be flat (score in neutral zone 0.33-0.67)
    flat_pct = signals.count(0) / len(signals)
    assert flat_pct > 0.0   # must have at least some flat bars


# ─── signal() — with open position ───────────────────────────────────────────

def test_signal_holds_long_in_uptrend():
    """When long and score still bullish, hold (return 1)."""
    strat = _make_strategy()
    df = _ohlcv([100 + i * 0.4 for i in range(400)])
    out = strat.prepare(df)
    sig = strat.signal(out.iloc[-1], position=1)
    assert sig == 1


def test_signal_exits_long_when_score_drops():
    """When long and score turns bearish (< 0.33), exit."""
    strat = _make_strategy()
    # Build a row with manually crafted fib_score below bear threshold
    df = _ohlcv([100 + i * 0.4 for i in range(400)])
    out = strat.prepare(df)
    row = out.iloc[-1].copy()
    row["fib_score"] = 0.10   # below score_bear threshold
    sig = strat.signal(row, position=1)
    assert sig == 0


def test_signal_holds_short_in_downtrend():
    """When short and score still bearish, hold (return -1)."""
    strat = _make_strategy()
    df = _ohlcv([400 - i * 0.4 for i in range(400)])
    out = strat.prepare(df)
    sig = strat.signal(out.iloc[-1], position=-1)
    assert sig == -1


def test_signal_exits_short_when_score_rises():
    """When short and score turns bullish (> 0.67), exit."""
    strat = _make_strategy()
    df = _ohlcv([400 - i * 0.4 for i in range(400)])
    out = strat.prepare(df)
    row = out.iloc[-1].copy()
    row["fib_score"] = 0.90   # above score_bull threshold
    sig = strat.signal(row, position=-1)
    assert sig == 0


# ─── allow_short / regime_pref ───────────────────────────────────────────────

def test_allow_short_is_true():
    strat = _make_strategy()
    assert strat.allow_short is True


def test_regime_pref_is_trend():
    strat = _make_strategy()
    assert strat.regime_pref == "trend"


# ─── regime 閘門（新增）────────────────────────────────────────────────────────

def _row_with_regime(regime_val, score=0.90, rsi=50.0):
    """Build a minimal row dict with fib_score, rsi, and regime."""
    return {"fib_score": score, "rsi": rsi, "regime": regime_val}


def test_entry_blocked_when_regime_is_range():
    """regime='range' 時不論 fib_score 多強，空手不進場（whipsaw 防護）。"""
    strat = _make_strategy()
    row = _row_with_regime("range", score=1.0)
    assert strat.signal(row, position=0) == 0


def test_entry_allowed_when_regime_is_trend():
    """regime='trend' 時 fib_score >= score_bull → 正常開多。"""
    strat = _make_strategy()
    row = _row_with_regime("trend", score=1.0)
    assert strat.signal(row, position=0) == 1


def test_short_entry_blocked_when_regime_is_range():
    """regime='range' 時空單訊號也被擋下。"""
    strat = _make_strategy()
    row = _row_with_regime("range", score=0.0)
    assert strat.signal(row, position=0) == 0


def test_exit_still_fires_in_range_regime():
    """持多中即使 regime='range'，score 跌破 bear 仍須出場（出場不受 regime 限制）。"""
    strat = _make_strategy()
    row = _row_with_regime("range", score=0.10)
    assert strat.signal(row, position=1) == 0


def test_exit_short_still_fires_in_range_regime():
    """持空中即使 regime='range'，score 升破 bull 仍須出場。"""
    strat = _make_strategy()
    row = _row_with_regime("range", score=0.95)
    assert strat.signal(row, position=-1) == 0


# ── OPT-17：出場死區可調（exit_mid，預設 None=現行 0.33/0.67 死區，default-off）──
def test_exit_mid_none_keeps_current_deadzone():
    strat = _make_strategy()                         # 未設 exit_mid
    # 持多、score=0.5（在 0.33–0.67 死區內）→ 維持多單（現行行為不變）
    assert strat.signal(pd.Series({"fib_score": 0.5, "rsi": 50.0}), 1) == 1


def test_exit_mid_tightens_long_exit():
    strat = _make_strategy(exit_mid=0.5)
    assert strat.signal(pd.Series({"fib_score": 0.45, "rsi": 50.0}), 1) == 0   # <0.5 → 出
    assert strat.signal(pd.Series({"fib_score": 0.60, "rsi": 50.0}), 1) == 1   # ≥0.5 → 抱


def test_exit_mid_tightens_short_exit():
    strat = _make_strategy(exit_mid=0.5)             # 空單對稱門檻 = 1-0.5 = 0.5
    assert strat.signal(pd.Series({"fib_score": 0.55, "rsi": 50.0}), -1) == 0  # >0.5 → 出
    assert strat.signal(pd.Series({"fib_score": 0.40, "rsi": 50.0}), -1) == -1 # ≤0.5 → 抱
