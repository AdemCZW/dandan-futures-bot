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
