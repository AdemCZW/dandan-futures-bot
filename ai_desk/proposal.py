"""交易提案 — AI 裁判輸出的結構化提案 + 既有風控官夾限。

不重新實作任何風控規則：熔斷/清算守衛/倉位上限全部走既有
core.risk_officer.RiskOfficer（與規則策略同一套、同一實例邏輯）。
AI 的信心分數對風控沒有任何影響力。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.risk_officer import RiskDecision, RiskOfficer


@dataclass
class TradeProposal:
    symbol: str
    ts: str
    direction: int        # 1 多 / -1 空 / 0 觀望
    confidence: float     # 0~1（僅供人工核准參考，不影響風控）
    entry: float
    stop: float
    take_profit: float
    rationale: str


def proposal_from_judge(data: dict, symbol: str, ts: str) -> TradeProposal:
    """裁判 JSON → TradeProposal。驗證失敗拋 ValueError（該輪提案作廢）。"""
    direction = int(data["direction"])
    if direction not in (-1, 0, 1):
        raise ValueError(f"direction 必須是 -1/0/1，收到 {direction}")
    confidence = float(data["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence 必須在 0~1，收到 {confidence}")
    entry = float(data["entry"])
    stop = float(data["stop"])
    take_profit = float(data["take_profit"])
    if direction == 1 and not stop < entry:
        raise ValueError(f"多單停損({stop})必須低於進場價({entry})")
    if direction == -1 and not stop > entry:
        raise ValueError(f"空單停損({stop})必須高於進場價({entry})")
    return TradeProposal(symbol=symbol, ts=ts, direction=direction,
                         confidence=confidence, entry=entry, stop=stop,
                         take_profit=take_profit,
                         rationale=str(data["rationale"]))


def clamp_with_risk_officer(proposal: TradeProposal, officer: RiskOfficer,
                            equity: float, atr=None) -> RiskDecision:
    """既有風控官夾限：熔斷/清算守衛照走，倉位取「AI 停損」與「風控停損」
    兩種算法中較保守（較小）者。AI 信心分數不參與任何計算。"""
    if proposal.direction == 0:
        return RiskDecision(False, 0.0, "AI 建議觀望，不進場")
    gate = officer.check_entry(equity, proposal.entry, proposal.ts,
                               direction=proposal.direction, atr=atr)
    if not gate.allow:
        return gate
    qty_ai_stop = officer.position_size(equity, proposal.entry, proposal.stop)
    qty = min(gate.quantity, qty_ai_stop)
    if qty <= 0:
        return RiskDecision(False, 0.0, "風控算出倉位為 0")
    return RiskDecision(True, qty, "ok（倉位取 AI 停損與風控停損較保守者）")
