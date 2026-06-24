"""run_paper._apply_trailing_long — Chandelier 追蹤停損（多單）邏輯測試。

確保 paper bot 持倉時停損會隨新高往上移（只升不降），而非固定在進場時的初始停損。
"""
from run_paper import _apply_trailing_long
from core.risk_officer import RiskOfficer
from config import Config


def test_trailing_sl_moves_up_on_new_high():
    """新高出現時，停損應往上移。"""
    cfg = Config()   # chand_mult = 3.0
    risk = RiskOfficer(cfg)
    initial_sl = 95.0
    new_sl, new_highest = _apply_trailing_long(initial_sl, 100.0, 102.0, 2.0, risk)
    assert new_highest == 102.0
    expected_sl = risk.update_trailing_stop(initial_sl, 102.0, 2.0, 1)
    assert new_sl == expected_sl
    assert new_sl > initial_sl


def test_trailing_sl_never_decreases_on_pullback():
    """回撤時（bar_high < 歷史最高），停損保持不動（只升不降）。"""
    cfg = Config()
    risk = RiskOfficer(cfg)
    initial_sl = 95.0
    sl1, h1 = _apply_trailing_long(initial_sl, 100.0, 106.0, 2.0, risk)
    # 第二根回撤到 103 — sl 不能降
    sl2, h2 = _apply_trailing_long(sl1, h1, 103.0, 2.0, risk)
    assert h2 == h1    # 歷史最高維持不變
    assert sl2 == sl1  # 停損只升不降


def test_trailing_highest_always_updated():
    """就算停損沒動，歷史最高仍需更新。"""
    cfg = Config()
    risk = RiskOfficer(cfg)
    sl, h = _apply_trailing_long(95.0, 100.0, 108.0, 2.0, risk)
    assert h == 108.0


def test_trailing_sl_unchanged_when_atr_none():
    """atr=None 時停損維持不變（ATR 不可用的 fallback）。"""
    cfg = Config()
    risk = RiskOfficer(cfg)
    new_sl, new_highest = _apply_trailing_long(95.0, 100.0, 110.0, None, risk)
    assert new_sl == 95.0       # 無 ATR → 停損不更新
    assert new_highest == 110.0  # 歷史最高仍更新


def test_trailing_sl_unchanged_when_atr_zero():
    """atr=0 時停損維持不變（避免除零）。"""
    cfg = Config()
    risk = RiskOfficer(cfg)
    new_sl, _ = _apply_trailing_long(95.0, 100.0, 110.0, 0.0, risk)
    assert new_sl == 95.0
