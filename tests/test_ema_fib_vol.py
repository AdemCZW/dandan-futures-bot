"""EmaFibVolStrategy（ema_fib_vol）的 TDD 測試 — 先寫測試再實作。

複合策略：雙均線（趨勢）× 斐波那契通道回調（時機）× 量能放大（參與度）三重確認。
文獻依據：EMA 趨勢 + fib 回調區（38.2–61.8%）+ 量能確認是 pullback trading 標準做法；
本策略把三個閘門全部要求同時成立才進場，出場用死叉或通道目標（hysteresis）。

全部用直接構造的 row 測 signal() 純函式行為（無 regime 欄 → _regime_ok 放行，
與其他策略單元測試同一慣例）。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import STRATEGIES, build_strategy


def _mk_df(n=300, seed=3):
    """帶趨勢的確定性 OHLCV，足夠讓 prepare 產生所有欄位。"""
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0.08, 1.0, n))
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    vol = np.abs(rng.normal(1000, 200, n)) + 1
    return pd.DataFrame({
        "open": close,
        "high": close + np.abs(rng.normal(0, 0.5, n)) + 0.1,
        "low": close - np.abs(rng.normal(0, 0.5, n)) - 0.1,
        "close": close, "volume": vol,
    }, index=idx)


def _row(ema_fast=110.0, ema_slow=100.0, atr=2.0, fib_ch_pos=0.2, fib_ch_dir=1,
         vol_ratio=1.5, close=105.0):
    """可控的單列：預設值 = 「多方三閘門全過」（金叉分離足、回調到原點區、量能放大）。"""
    return {"ema_fast": ema_fast, "ema_slow": ema_slow, "atr": atr,
            "fib_ch_pos": fib_ch_pos, "fib_ch_dir": fib_ch_dir,
            "vol_ratio": vol_ratio, "close": close}


# ── 註冊表 ──────────────────────────────────────────────────────────────────
def test_registered_and_buildable():
    assert "ema_fib_vol" in STRATEGIES
    s = build_strategy("ema_fib_vol")
    assert s.allow_short is True


def test_registry_now_has_eighteen_keys():
    assert len(STRATEGIES) == 18


# ── prepare()：欄位齊全 ─────────────────────────────────────────────────────
def test_prepare_adds_all_gate_columns():
    s = build_strategy("ema_fib_vol")
    out = s.prepare(_mk_df())
    for col in ("ema_fast", "ema_slow", "fib_ch_pos", "fib_ch_dir",
                "vol_ratio", "atr"):
        assert col in out.columns, f"缺欄位 {col}"


# ── 進場：三閘門全過才進 ────────────────────────────────────────────────────
def test_long_entry_when_all_three_gates_pass():
    s = build_strategy("ema_fib_vol")
    assert s.signal(_row(), 0) == 1


def test_short_entry_symmetric():
    s = build_strategy("ema_fib_vol")
    r = _row(ema_fast=90.0, ema_slow=100.0, fib_ch_dir=-1)   # 死叉分離足 + 下降通道回調 + 量能
    assert s.signal(r, 0) == -1


def test_no_entry_when_volume_gate_fails():
    """量能不足（vol_ratio < vol_min）→ 其他條件再好也不進場。"""
    s = build_strategy("ema_fib_vol")
    assert s.signal(_row(vol_ratio=0.8), 0) == 0


def test_no_entry_when_no_pullback():
    """價格不在回調區（fib_ch_pos > entry_zone）→ 不追價。"""
    s = build_strategy("ema_fib_vol")
    assert s.signal(_row(fib_ch_pos=0.7), 0) == 0


def test_no_entry_when_ema_disagrees_with_channel():
    """通道上升但均線死叉（趨勢矛盾）→ 不進場。"""
    s = build_strategy("ema_fib_vol")
    r = _row(ema_fast=95.0, ema_slow=100.0, fib_ch_dir=1)
    assert s.signal(r, 0) == 0


def test_no_entry_when_separation_below_atr_buffer():
    """金叉但兩線分離 < sep_atr_k×ATR（假交叉抖動）→ 不進場。"""
    s = build_strategy("ema_fib_vol", sep_atr_k=0.5)
    r = _row(ema_fast=100.5, ema_slow=100.0, atr=2.0)   # 分離 0.5 < 0.5×2.0
    assert s.signal(r, 0) == 0


def test_no_entry_when_channel_flat():
    """無通道方向（fib_ch_dir=0 / NaN）→ 不進場。"""
    s = build_strategy("ema_fib_vol")
    assert s.signal(_row(fib_ch_dir=0), 0) == 0
    assert s.signal(_row(fib_ch_dir=float("nan")), 0) == 0


# ── 出場：死叉或通道目標（多空對稱）────────────────────────────────────────
def test_long_exit_on_dead_cross():
    """持多 + 均線翻空 → 平倉（即使通道位置仍好）。"""
    s = build_strategy("ema_fib_vol")
    r = _row(ema_fast=95.0, ema_slow=100.0, fib_ch_pos=0.4)
    assert s.signal(r, 1) == 0


def test_long_exit_at_channel_target():
    """持多 + 到達通道目標側（pos > exit_zone）→ 停利平倉。"""
    s = build_strategy("ema_fib_vol")
    r = _row(fib_ch_pos=0.9)                     # 均線仍多頭，但已到目標
    assert s.signal(r, 1) == 0


def test_long_exit_on_channel_break():
    """持多 + 跌破通道原點（pos < -break_buffer）→ 停損平倉。"""
    s = build_strategy("ema_fib_vol")
    r = _row(fib_ch_pos=-0.2)
    assert s.signal(r, 1) == 0


def test_long_holds_mid_channel():
    """持多 + 均線仍多 + 通道中段 → 續抱。"""
    s = build_strategy("ema_fib_vol")
    r = _row(fib_ch_pos=0.5)
    assert s.signal(r, 1) == 1


def test_short_exit_on_golden_cross():
    s = build_strategy("ema_fib_vol")
    r = _row(ema_fast=110.0, ema_slow=100.0, fib_ch_dir=-1, fib_ch_pos=0.4)
    assert s.signal(r, -1) == 0


# ── 暖機：NaN 維持現狀（與全庫策略契約一致）────────────────────────────────
@pytest.mark.parametrize("pos", [0, 1, -1])
def test_warmup_nan_holds_position(pos):
    s = build_strategy("ema_fib_vol")
    r = _row(ema_fast=float("nan"))
    assert s.signal(r, pos) == pos


# ── 回測引擎整合：跑得動、能產生交易 ────────────────────────────────────────
def test_runs_through_backtester():
    from backtest.backtester import run_backtest
    from core.risk_officer import RiskOfficer
    from config import Config
    cfg = Config(interval="4h", max_daily_loss_pct=10.0)
    res = run_backtest(_mk_df(400), build_strategy("ema_fib_vol"),
                       RiskOfficer(cfg), cfg)
    assert res.equity_curve is not None and len(res.equity_curve) > 0
