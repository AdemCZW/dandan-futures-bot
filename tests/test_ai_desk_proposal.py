"""ai_desk.proposal 測試 — 提案驗證 + 與既有 RiskOfficer 的夾限整合。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from core.risk_officer import RiskOfficer

from ai_desk.proposal import TradeProposal, clamp_with_risk_officer, proposal_from_judge


@pytest.fixture
def officer():
    return RiskOfficer(Config())


def judge_data(**over):
    d = {"direction": 1, "confidence": 0.6, "entry": 100.0, "stop": 95.0,
         "take_profit": 110.0, "rationale": "測試"}
    d.update(over)
    return d


def test_valid_long_proposal():
    p = proposal_from_judge(judge_data(), "BTCUSDT", "2026-07-17T08:00:00Z")
    assert p.direction == 1 and p.entry == 100.0 and p.symbol == "BTCUSDT"


def test_long_stop_must_be_below_entry():
    with pytest.raises(ValueError, match="停損"):
        proposal_from_judge(judge_data(stop=105.0), "BTCUSDT", "t")


def test_short_stop_must_be_above_entry():
    with pytest.raises(ValueError, match="停損"):
        proposal_from_judge(judge_data(direction=-1, stop=95.0, take_profit=90.0),
                            "BTCUSDT", "t")


def test_bad_direction_and_confidence_rejected():
    with pytest.raises(ValueError):
        proposal_from_judge(judge_data(direction=2), "BTCUSDT", "t")
    with pytest.raises(ValueError):
        proposal_from_judge(judge_data(confidence=1.5), "BTCUSDT", "t")


def test_direction_zero_skips_price_validation():
    p = proposal_from_judge(judge_data(direction=0, entry=0, stop=0, take_profit=0),
                            "BTCUSDT", "t")
    assert p.direction == 0


def test_direction_zero_with_null_prices_defaults_to_zero():
    """裁判判觀望時常把 entry/stop/take_profit 給 null；不可 float(None) 崩潰。"""
    p = proposal_from_judge(
        judge_data(direction=0, entry=None, stop=None, take_profit=None),
        "BTCUSDT", "t")
    assert p.direction == 0
    assert p.entry == 0.0 and p.stop == 0.0 and p.take_profit == 0.0


def test_directional_with_null_price_raises_valueerror_not_typeerror():
    """有方向卻缺價格 → 明確 ValueError（不可用的提案），而非 float(None) 的 TypeError。"""
    with pytest.raises(ValueError):
        proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=None,
                                       take_profit=90.0), "BTCUSDT", "t")


def test_clamp_flat_proposal_not_allowed(officer):
    p = proposal_from_judge(judge_data(direction=0, entry=0, stop=0, take_profit=0),
                            "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0)
    assert d.allow is False and "觀望" in d.reason


def test_clamp_uses_conservative_qty(officer):
    """AI 停損距離(5%)比預設固定停損(2%)寬 → AI 停損算出的 qty 較小 → 取較小者。"""
    p = proposal_from_judge(judge_data(), "BTCUSDT", "2026-07-17T08:00:00Z")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0)
    assert d.allow is True
    qty_ai = officer.position_size(10_000.0, 100.0, 95.0)
    assert d.quantity == pytest.approx(qty_ai)


def test_clamp_respects_circuit_breaker(officer):
    """單日虧損熔斷觸發 → 不管 AI 信心多高一律拒絕。"""
    officer.check_entry(10_000.0, 100.0, "2026-07-17T00:00:00Z")   # 建立當日基準
    p = proposal_from_judge(judge_data(confidence=0.99), "BTCUSDT",
                            "2026-07-17T08:00:00Z")
    d = clamp_with_risk_officer(p, officer, equity=9_000.0)        # 當日 -10%
    assert d.allow is False and "熔斷" in d.reason


# ── 進場區位閘門（Fib zone gate）────────────────────────────
#
# 證據（2026-08-06，56 筆已結算前瞻樣本）：做空依進場時的 Fib 區間位置分組，
# 勝率呈單調遞增——低位(fib<0.30) 6.2%／中位(0.30~0.45) 17.6%／
# 高位(fib>=0.45) 45.5%，相關係數 +0.347。反事實：只留 fib>=0.45 的做空，
# 被砍掉的 33 筆合計 -509.46，保留的 11 筆合計 +145.45。
# 做多側是同一個病的鏡像（#4 fib=1.095、#8 fib=0.857 都是貼著區間頂做多後停損）。
#
# ⚠️ 界定：這是「唯一一組方向與整體相反、值得優先測試的線索」，不是已證明的 edge。
# 保留組 bootstrap 信賴下界仍為 -1.24（n=11），未過本專案的正式晉升閘門；
# 做多側證據更弱（僅 2-3 筆可量測），屬鏡像對稱推論。故程式碼預設關閉。

from ai_desk.proposal import (DEFAULT_LONG_MAX_FIB, DEFAULT_SHORT_MIN_FIB,  # noqa: E402
                              entry_zone_allows, zone_gate_enabled)


def test_zone_gate_disabled_by_default(monkeypatch):
    """預設關閉——比照專案既有新過濾器慣例，不默默改變線上行為。"""
    monkeypatch.delenv("AI_DESK_ZONE_GATE", raising=False)
    assert zone_gate_enabled() is False


def test_zone_gate_requires_exact_true(monkeypatch):
    monkeypatch.setenv("AI_DESK_ZONE_GATE", "1")
    assert zone_gate_enabled() is False
    monkeypatch.setenv("AI_DESK_ZONE_GATE", "true")
    assert zone_gate_enabled() is True
    monkeypatch.setenv("AI_DESK_ZONE_GATE", "TRUE")
    assert zone_gate_enabled() is True


def test_short_blocked_at_range_low():
    """貼近區間低點追空 = 實測勝率 6.2% 的區域，擋掉。"""
    assert entry_zone_allows(-1, 0.21) is False
    assert entry_zone_allows(-1, 0.0) is False


def test_short_allowed_at_range_high():
    """反彈到區間中上緣才做空 = 實測勝率 45.5% 的區域，放行。"""
    assert entry_zone_allows(-1, 0.50) is True
    assert entry_zone_allows(-1, 0.76) is True


def test_short_boundary_is_inclusive():
    """門檻本身算通過（fib>=0.45），與統計分組的定義一致。"""
    assert entry_zone_allows(-1, DEFAULT_SHORT_MIN_FIB) is True


def test_long_blocked_at_range_high():
    """貼區間頂做多 —— #4(fib=1.095)、#8(fib=0.857) 都是這樣停損的。"""
    assert entry_zone_allows(1, 0.857) is False
    assert entry_zone_allows(1, 1.095) is False


def test_long_allowed_at_range_low():
    assert entry_zone_allows(1, 0.345) is True
    assert entry_zone_allows(1, 0.10) is True


def test_long_boundary_is_inclusive():
    assert entry_zone_allows(1, DEFAULT_LONG_MAX_FIB) is True


def test_missing_fib_blocks_entry():
    """缺值視為不通過——比照 smc_structure 的 vol/corr 過濾器慣例，
    寧可少做一筆，也不要在不知道自己站在區間哪裡的情況下進場。"""
    assert entry_zone_allows(-1, None) is False
    assert entry_zone_allows(1, None) is False


def test_thresholds_are_symmetric_around_midpoint():
    """雙向對稱：做空下限與做多上限應對稱於區間中點 0.5。"""
    assert DEFAULT_SHORT_MIN_FIB + DEFAULT_LONG_MAX_FIB == pytest.approx(1.0)


def test_clamp_blocks_short_in_bad_zone_when_gate_on(officer, monkeypatch):
    """閘門開啟時，風控層真的擋下低位做空。"""
    monkeypatch.setenv("AI_DESK_ZONE_GATE", "true")
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, fib_pos=0.21)
    assert d.allow is False
    assert "區間" in d.reason and "0.21" in d.reason   # 原因要說清楚，方便事後稽核


def test_clamp_allows_short_in_good_zone_when_gate_on(officer, monkeypatch):
    monkeypatch.setenv("AI_DESK_ZONE_GATE", "true")
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, fib_pos=0.60)
    assert d.allow is True


def test_clamp_ignores_zone_when_gate_off(officer, monkeypatch):
    """閘門關閉時行為與現況逐位元相同——低位做空照樣放行。"""
    monkeypatch.delenv("AI_DESK_ZONE_GATE", raising=False)
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, fib_pos=0.21)
    assert d.allow is True


def test_zone_gate_never_affects_sizing(officer, monkeypatch):
    """硬規則不變：閘門只決定「進不進場」，絕不改變倉位大小。"""
    monkeypatch.setenv("AI_DESK_ZONE_GATE", "true")
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    gated = clamp_with_risk_officer(p, officer, equity=10_000.0, fib_pos=0.60)
    monkeypatch.delenv("AI_DESK_ZONE_GATE", raising=False)
    plain = clamp_with_risk_officer(p, officer, equity=10_000.0, fib_pos=0.60)
    assert gated.quantity == plain.quantity


# ── 停利可達性閘門（target reachability gate）─────────────────
#
# 證據（2026-08-13，68 筆已結算前瞻樣本）：依停利距離（ATR 單位）分組，
# 勝率單調遞減——
#     <2 ATR   n=48  勝率 27.1%  合計 -158.66
#     2~3 ATR  n= 9  勝率 11.1%  合計 -222.95
#     >=3 ATR  n=11  勝率  0.0%  合計 -278.89   ← 11 戰全敗
#
# 關鍵對照：規劃 R/R 中位數 2.23，純隨機進場的理論勝率是 31%，實際只有 20.6%
# ——比隨機還差，代表有系統性錯誤，不只是猜不準方向。機制：震盪盤裡 3+ ATR 的
# 停利，在觸及 1.5 ATR 停損之前物理上到不了，這是規格上就達不成的單。
#
# ⚠️ 這是同一批樣本上測的第 6 個假設（p-hacking 風險高），且未經樣本外驗證。
# 故預設關閉，須與區位閘門一起用新樣本前瞻對照。

from ai_desk.proposal import (DEFAULT_MAX_TP_ATR, target_gate_enabled,  # noqa: E402
                              target_is_reachable)


def test_target_gate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AI_DESK_TARGET_GATE", raising=False)
    assert target_gate_enabled() is False


def test_target_gate_requires_exact_true(monkeypatch):
    monkeypatch.setenv("AI_DESK_TARGET_GATE", "1")
    assert target_gate_enabled() is False
    monkeypatch.setenv("AI_DESK_TARGET_GATE", "true")
    assert target_gate_enabled() is True


def test_near_target_is_reachable():
    """1 ATR 的停利，震盪盤裡也走得到。"""
    assert target_is_reachable(entry=100.0, take_profit=110.0, atr=10.0) is True


def test_far_target_is_not_reachable():
    """實測 >=3 ATR 那組 11 戰全敗。"""
    assert target_is_reachable(entry=100.0, take_profit=140.0, atr=10.0) is False


def test_threshold_boundary_is_exclusive():
    """門檻本身算不通過（>=3 才是實測全敗那組的定義）。"""
    atr = 10.0
    tp_at_threshold = 100.0 + DEFAULT_MAX_TP_ATR * atr
    assert target_is_reachable(entry=100.0, take_profit=tp_at_threshold, atr=atr) is False
    assert target_is_reachable(entry=100.0, take_profit=tp_at_threshold - 0.01,
                               atr=atr) is True


def test_works_for_short_direction_by_absolute_distance():
    """做空的停利在進場價下方；只看絕對距離，方向無關。"""
    assert target_is_reachable(entry=100.0, take_profit=90.0, atr=10.0) is True
    assert target_is_reachable(entry=100.0, take_profit=60.0, atr=10.0) is False


def test_missing_atr_blocks_entry():
    """缺 ATR 一律不通過——比照區位閘門與 smc_structure 過濾器的既有慣例。"""
    assert target_is_reachable(entry=100.0, take_profit=110.0, atr=None) is False


def test_non_positive_atr_blocks_entry_instead_of_dividing_by_zero():
    assert target_is_reachable(entry=100.0, take_profit=110.0, atr=0.0) is False
    assert target_is_reachable(entry=100.0, take_profit=110.0, atr=-5.0) is False


def test_clamp_blocks_unreachable_target_when_gate_on(officer, monkeypatch):
    monkeypatch.setenv("AI_DESK_TARGET_GATE", "true")
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=60.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, atr=10.0)
    assert d.allow is False
    assert "停利" in d.reason and "ATR" in d.reason      # 原因要可稽核


def test_clamp_allows_reachable_target_when_gate_on(officer, monkeypatch):
    monkeypatch.setenv("AI_DESK_TARGET_GATE", "true")
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, atr=10.0)
    assert d.allow is True


def test_clamp_ignores_target_distance_when_gate_off(officer, monkeypatch):
    """閘門關閉時行為與現況逐位元相同。"""
    monkeypatch.delenv("AI_DESK_TARGET_GATE", raising=False)
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=60.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, atr=10.0)
    assert d.allow is True


def test_two_gates_are_independent(officer, monkeypatch):
    """只開停利閘門時，不利區位的提案照樣放行（兩道閘門互不牽連）。"""
    monkeypatch.setenv("AI_DESK_TARGET_GATE", "true")
    monkeypatch.delenv("AI_DESK_ZONE_GATE", raising=False)
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0, atr=10.0, fib_pos=0.10)
    assert d.allow is True


def test_target_gate_never_affects_sizing(officer, monkeypatch):
    """硬規則不變：閘門只決定進不進場，絕不改變倉位大小。"""
    p = proposal_from_judge(judge_data(direction=-1, entry=100.0, stop=105.0,
                                       take_profit=90.0), "BTCUSDT", "t")
    monkeypatch.setenv("AI_DESK_TARGET_GATE", "true")
    gated = clamp_with_risk_officer(p, officer, equity=10_000.0, atr=10.0)
    monkeypatch.delenv("AI_DESK_TARGET_GATE", raising=False)
    plain = clamp_with_risk_officer(p, officer, equity=10_000.0, atr=10.0)
    assert gated.quantity == plain.quantity
