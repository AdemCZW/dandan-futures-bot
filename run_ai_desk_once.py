"""ai_desk 單輪進入點 — 抓真實合約 K 線，跑一輪四角色辯論，提案進 pending。

Phase 1：本機手動執行、人工肉眼檢視辯論品質。不排程、不執行任何下單。
核准後的提案執行接線屬 Phase 2（本腳本只列印 approved 未執行清單提醒）。

用法（Phase 1 預設走訂閱 CLI，不需 API key、不需 pip install）：
    python run_ai_desk_once.py [SYMBOL] [INTERVAL]
    （預設 BTCUSDT 4h；需本機已登入 claude CLI；K 線用幣安公開端點，不需交易金鑰）

切換到 API 版（Phase 2 排程用，按量計費）：
    AI_DESK_LLM=api ANTHROPIC_API_KEY=sk-ant-... python run_ai_desk_once.py

費用注意：預設 CLI 版走 Max 訂閱額度（免額外計費，但吃訂閱用量上限）；每輪 4 次呼叫。
"""
import os
import sys

import pandas as pd

from binance.client import Client
from config import Config
from core.market_analyst import fetch_klines
from core.risk_officer import RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.desk import run_one_cycle
from ai_desk.llm_client import AnthropicLLMClient, ClaudeCliClient
from ai_desk.memory import ThesisMemory

MEMORY_DIR = os.path.join("ai_desk", "memory")
EQUITY_FOR_SIZING = 10_000.0   # Phase 1 名目資金（測試網虛擬資金基準）


def prepare_df(raw: pd.DataFrame) -> pd.DataFrame:
    """fetch_klines 已以 open_time 為 DatetimeIndex；這裡只丟掉最後一根未收盤 K 棒。"""
    return raw.iloc[:-1]


def build_llm():
    """依 AI_DESK_LLM 選後端：預設 cli（走訂閱、免額外計費）；api（按量計費，Phase 2 排程）。"""
    if os.getenv("AI_DESK_LLM", "cli").lower() == "api":
        return AnthropicLLMClient()                    # 缺 key 在這裡就報錯
    return ClaudeCliClient()                           # 走本機登入的訂閱額度


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "4h"

    llm = build_llm()
    client = Client()                                 # 公開 K 線端點不需金鑰
    raw = fetch_klines(client, symbol, interval, limit=400, futures=True)
    df = prepare_df(raw)

    result = run_one_cycle(
        df, symbol, interval,
        llm_call=llm,
        risk_officer=RiskOfficer(Config()),
        equity=EQUITY_FOR_SIZING,
        memory=ThesisMemory(MEMORY_DIR, symbol, interval),
        approval_store=ApprovalStore(),
    )

    print("=" * 60)
    for role, text in result.debate.items():
        print(f"\n【{role}】\n{text}")
    print("=" * 60)
    p, r = result.proposal, result.risk
    dir_txt = {1: "做多", -1: "做空", 0: "觀望"}[p.direction]
    print(f"提案：{dir_txt}  信心 {p.confidence:.2f}")
    if p.direction != 0:
        print(f"進場 {p.entry}  停損 {p.stop}  停利 {p.take_profit}")
    print(f"風控：{'放行' if r.allow else '拒絕'}（{r.reason}）"
          + (f"  數量 {r.quantity:.6f}" if r.allow else ""))
    if result.proposal_id is not None:
        print(f"→ 已進入待核准佇列 #{result.proposal_id}；"
              f"檢視/核准：python -m ai_desk.approval")

    store = ApprovalStore()
    approved = store.approved_unexecuted()
    if approved:
        print(f"⚠ 有 {len(approved)} 筆已核准未執行的提案"
              f"（執行接線屬 Phase 2，目前需人工處理）")


if __name__ == "__main__":
    main()
