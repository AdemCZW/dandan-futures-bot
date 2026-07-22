"""ai_desk 全自動核准+下單 — 取代「人工核准」這一步，其餘防線全部維持。

修訂記錄見 docs/superpowers/specs/2026-07-20-ai-desk-phase2-execution-design.md
（2026-07-22 全自動核准+執行）：使用者在知悉現況（樣本數少、尚無統計驗證、
Testnet 虛擬資金）的情況下明確要求全自動。

狀態機本身不變——approve() 仍是進入 placed 的唯一入口，只是呼叫者從人變成本模組，
核准這個轉移本身的紀錄/稽核性質不變。`AI_DESK_AUTO_APPROVE` 開關（見進入點腳本）
預設關閉：本模組本身永遠自動核准，是否被使用是呼叫端的選擇。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.risk_officer import RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.desk import CycleResult, run_one_cycle
from ai_desk.executor import place_entry
from ai_desk.memory import ThesisMemory


@dataclass
class AutoCycleResult:
    cycle: CycleResult
    placed: bool
    error: str | None


def run_auto_cycle(df, symbol: str, interval: str, *,
                   llm_call, risk_officer: RiskOfficer, equity: float,
                   memory: ThesisMemory, approval_store: ApprovalStore,
                   engine, on_progress=None) -> AutoCycleResult:
    """跑一輪辯論；風控放行的方向性提案立刻自動核准+掛單，不等人工核准。

    觀望或風控拒絕的提案（proposal_id 為 None）不會被核准，也不會嘗試掛單。
    白名單/testnet 驗證失敗時：提案仍停在 approved（已核准但未掛出），
    錯誤原因回傳在 error 欄位，不靜默吞掉。
    """
    cycle = run_one_cycle(
        df, symbol, interval,
        llm_call=llm_call, risk_officer=risk_officer, equity=equity,
        memory=memory, approval_store=approval_store, on_progress=on_progress,
    )
    if cycle.proposal_id is None:
        return AutoCycleResult(cycle=cycle, placed=False, error=None)

    approval_store.approve(cycle.proposal_id)
    try:
        place_entry(approval_store.get(cycle.proposal_id), approval_store, engine)
        return AutoCycleResult(cycle=cycle, placed=True, error=None)
    except Exception as e:
        return AutoCycleResult(cycle=cycle, placed=False, error=str(e))
