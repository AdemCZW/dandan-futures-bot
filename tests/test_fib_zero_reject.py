"""FibZeroAxisReject（fib_zero_reject）TDD 測試。

復刻使用者提供的分析師「零軸拒絕」交易法（2026-07-09）：迴歸斐波那契通道的
零軸(下降通道=上緣壓力/上升通道=下緣支撐)有高機率成為反向點，價格觸零軸 +
量能衰減(多頭動能枯竭) → 順通道方向進場(下降做空/上升做多)，價格走到通道
內部目標(靠近一軸)獲利了結，價格反向突破零軸(通道破壞)則結構性停損。

pos 語意（見 se.fib_regression_levels）：0=零軸(進場側)、1=一軸(對側目標)。
進場/獲利在 pos 空間對多空對稱：都是 pos 往 1 走獲利、pos 轉負(破零軸)停損。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import STRATEGIES, build_strategy


def _mk_df(n=200, seed=7):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0, 1.0, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open": close, "high": close + np.abs(rng.normal(0, 0.5, n)) + 0.1,
        "low": close - np.abs(rng.normal(0, 0.5, n)) - 0.1,
        "close": close, "volume": np.abs(rng.normal(1000, 200, n)) + 1,
    }, index=idx)


def _row(fib_rc_pos=0.05, fib_rc_dir=-1, vol_decay=True, atr=2.0,
        zero_reject_dir=None):
    # zero_reject_dir 預設跟隨「觸零軸+量能衰減即進場」的舊行為，讓既有測試不變；
    # 第二根確認開啟時，改由 prepare 算出的欄位決定。
    if zero_reject_dir is None:
        zero_reject_dir = fib_rc_dir if (fib_rc_pos is not None and fib_rc_pos <= 0.15
                                         and vol_decay) else 0
    return {"fib_rc_pos": fib_rc_pos, "fib_rc_dir": fib_rc_dir,
            "vol_decay": vol_decay, "atr": atr, "zero_reject_dir": zero_reject_dir}


def test_registered_and_buildable():
    assert "fib_zero_reject" in STRATEGIES
    s = build_strategy("fib_zero_reject")
    assert s.allow_short is True


def test_prepare_adds_columns():
    s = build_strategy("fib_zero_reject")
    out = s.prepare(_mk_df())
    for col in ("fib_rc_pos", "fib_rc_dir", "vol_decay", "atr", "zero_reject_dir"):
        assert col in out.columns


# ── 第二根四小時確認（2026-07-09，使用者指正：分析師是看第二根 K 棒才決定進場）──
def test_second_candle_confirm_off_by_default_uses_immediate_touch():
    """預設關第二根確認 → 維持「觸零軸+量能衰減即進場」的舊行為（既有測試不變）。"""
    s = build_strategy("fib_zero_reject")
    assert s.params.get("use_second_candle_confirm") is False


def test_second_candle_confirm_enters_when_no_breakout():
    """開啟第二根確認：前一根撞零軸、當根沒有放量突破 → zero_reject_dir=通道方向 → 進場。"""
    s = build_strategy("fib_zero_reject", use_second_candle_confirm=True)
    r = _row(fib_rc_pos=0.05, fib_rc_dir=-1, zero_reject_dir=-1)
    assert s.signal(r, 0) == -1


def test_second_candle_confirm_skips_when_breakout():
    """開啟第二根確認：第二根放量突破零軸（zero_reject_dir=0）→ 不進場（不是拒絕）。"""
    s = build_strategy("fib_zero_reject", use_second_candle_confirm=True)
    r = _row(fib_rc_pos=0.05, fib_rc_dir=-1, zero_reject_dir=0)
    assert s.signal(r, 0) == 0


def test_prepare_second_candle_confirm_detects_rejection():
    """prepare 用震盪資料算 zero_reject_dir：價格反覆觸及通道零軸 → 應在某些根標出
    零軸拒絕訊號（不全 0）。用隨機walk（會自然觸及通道邊界，跟真實幣種一樣）。"""
    df = _mk_df(400, seed=11)
    s = build_strategy("fib_zero_reject", use_second_candle_confirm=True, lookback=60)
    out = s.prepare(df)
    assert (out["zero_reject_dir"] != 0).any(), "第二根確認應在某些根標出零軸拒絕訊號"


def test_short_at_zero_axis_in_down_channel_with_volume_decay():
    """下降通道(dir=-1) + 觸零軸(pos≈0) + 量能衰減 → 做空。"""
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=0.05, fib_rc_dir=-1, vol_decay=True), 0) == -1


def test_long_at_zero_axis_in_up_channel_with_volume_decay():
    """上升通道(dir=+1) + 觸零軸(pos≈0，此時零軸=下緣支撐) + 量能衰減 → 做多。"""
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=0.05, fib_rc_dir=1, vol_decay=True), 0) == 1


def test_no_entry_without_volume_decay():
    """觸零軸但多頭量能沒衰減（放量突破）→ 不進場（分析師：無量縮不算拒絕）。"""
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=0.05, fib_rc_dir=-1, vol_decay=False), 0) == 0


def test_no_entry_when_not_at_zero_axis():
    """價格在通道中段(pos=0.5)不是零軸 → 不進場。"""
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=0.5, fib_rc_dir=-1, vol_decay=True), 0) == 0


def test_volume_gate_off_enters_without_decay():
    """量能閘門關閉時(use_volume_gate=False)，觸零軸即進場（供 A/B 消融）。"""
    s = build_strategy("fib_zero_reject", use_volume_gate=False)
    assert s.signal(_row(fib_rc_pos=0.05, fib_rc_dir=-1, vol_decay=False), 0) == -1


def test_exit_short_at_target():
    """空單：pos 走到目標(≥target_pos，靠近一軸=獲利) → 出場。"""
    s = build_strategy("fib_zero_reject")   # target_pos 預設 0.5
    assert s.signal(_row(fib_rc_pos=0.6, fib_rc_dir=-1), -1) == 0


def test_hold_short_before_target():
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=0.3, fib_rc_dir=-1), -1) == -1


def test_stop_short_when_channel_breaks():
    """空單：pos 轉負(價格反向突破零軸=通道破壞) → 結構性停損。"""
    s = build_strategy("fib_zero_reject")   # break_buffer 預設 0.15
    assert s.signal(_row(fib_rc_pos=-0.3, fib_rc_dir=-1), -1) == 0


def test_exit_long_at_target():
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=0.6, fib_rc_dir=1), 1) == 0


def test_stop_long_when_channel_breaks():
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=-0.3, fib_rc_dir=1), 1) == 0


@pytest.mark.parametrize("pos", [0, 1, -1])
def test_holds_when_pos_nan(pos):
    """暖機期 fib_rc_pos=NaN → 維持現狀。"""
    s = build_strategy("fib_zero_reject")
    assert s.signal(_row(fib_rc_pos=float("nan")), pos) == pos


def test_runs_through_backtester():
    from backtest.backtester import run_backtest
    from core.risk_officer import RiskOfficer
    from config import Config
    cfg = Config(interval="4h", max_daily_loss_pct=10.0)
    res = run_backtest(_mk_df(400), build_strategy("fib_zero_reject"),
                       RiskOfficer(cfg), cfg)
    assert res.equity_curve is not None and len(res.equity_curve) > 0
