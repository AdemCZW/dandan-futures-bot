"""單輪編排 — 簡報 → 四角色辯論 → 提案 → 風控夾限 → pending → 記憶。

所有外部依賴（llm_call / risk_officer / memory / approval_store）注入，
本模組不 import anthropic、不碰網路。AI 永遠只提案：這裡的產出最遠只到
approval_store 的 pending 狀態，執行接線屬 Phase 2。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.risk_officer import RiskDecision, RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.briefing import build_market_briefing, format_briefing
from ai_desk.hedge_signal import count_judge_hedges
from ai_desk.memory import ThesisMemory
from ai_desk.proposal import TradeProposal, clamp_with_risk_officer, proposal_from_judge
from ai_desk.roles import (
    run_bear_researcher,
    run_bull_researcher,
    run_technical_analyst,
    run_trader_judge,
)


@dataclass
class CycleResult:
    proposal: TradeProposal
    risk: RiskDecision
    proposal_id: int | None   # 進了 pending 才有值
    debate: dict              # {"analyst"/"bull"/"bear"/"judge": 全文}


def run_one_cycle(df, symbol: str, interval: str, *,
                  llm_call, risk_officer: RiskOfficer, equity: float,
                  memory: ThesisMemory,
                  approval_store: ApprovalStore,
                  on_progress=None, live_price=None) -> CycleResult:
    """on_progress(role_name, full_text)（可選）：每個角色跑完就回報一次，
    role_name 依序為 analyst/bull/bear/judge，供即時介面逐步顯示辯論。"""
    def _report(role: str, text: str) -> None:
        if on_progress is not None:
            on_progress(role, text)

    briefing = build_market_briefing(df, symbol, interval, live_price=live_price)
    briefing_text = format_briefing(briefing)
    # 附上真實結算結果（觸及停損/停利/未成交），不讓模型自己拿「當時價 vs 現價」
    # 的價差瞎猜上次判斷對不對——那個推論方式是錯的，見 ai_desk.memory.attach_outcomes。
    memory_text = memory.format_for_prompt(
        approval_rows=[r for r in approval_store.all() if r["symbol"] == symbol],
        klines=df)

    analyst = run_technical_analyst(briefing_text, llm_call)
    _report("analyst", analyst.full_text)
    bull = run_bull_researcher(analyst.full_text, memory_text, llm_call)
    _report("bull", bull.full_text)
    bear = run_bear_researcher(analyst.full_text, bull.full_text,
                               memory_text, llm_call)
    _report("bear", bear.full_text)
    judge = run_trader_judge(analyst.full_text, bull.full_text,
                             bear.full_text, llm_call)
    _report("judge", judge.full_text)

    proposal = proposal_from_judge(judge.data, symbol, briefing.as_of)
    risk = clamp_with_risk_officer(proposal, risk_officer, equity,
                                   atr=briefing.atr, fib_pos=briefing.fib_pos)

    debate = {"analyst": analyst.full_text, "bull": bull.full_text,
              "bear": bear.full_text, "judge": judge.full_text}

    proposal_id = None
    if risk.allow:
        full_text = "\n\n".join(
            f"【{k}】\n{v}" for k, v in debate.items())
        # 記下產生這筆提案的模型（llm_call 有 .model 就取），供樣本分組比較；
        # judge_hedges 是純觀察指標（裁判提了幾種顧慮），不影響任何執行決策。
        proposal_id = approval_store.add(
            proposal, risk.quantity, full_text,
            model=getattr(llm_call, "model", None),
            judge_hedges=count_judge_hedges(full_text))

    memory.append({
        "ts": proposal.ts,
        "direction": proposal.direction,
        "confidence": proposal.confidence,
        "rationale_summary": proposal.rationale,
        "price_at_decision": briefing.close,
    })
    return CycleResult(proposal=proposal, risk=risk,
                       proposal_id=proposal_id, debate=debate)
