"""ai_desk 單輪進入點 — 抓真實合約 K 線，跑一輪四角色辯論。

預設（AI_DESK_AUTO_APPROVE 未設或非 "true"）：提案進 pending，等人工核准；
核准後執行需另外手動跑 run_ai_desk_execute.py（本腳本只列印提醒）。

全自動模式（AI_DESK_AUTO_APPROVE=true，2026-07-22 使用者明確要求開啟）：
風控放行的方向性提案立刻自動核准+掛單，不等人工核准。見設計文件修訂記錄
docs/superpowers/specs/2026-07-20-ai-desk-phase2-execution-design.md。

用法（Phase 1 預設走訂閱 CLI，不需 API key、不需 pip install）：
    python run_ai_desk_once.py [SYMBOL] [INTERVAL]
    （預設 BTCUSDT 4h；需本機已登入 claude CLI；K 線用幣安公開端點，不需交易金鑰）

全自動＋真實掛單（僅限 Testnet；SYMBOL 必須在 AI_DESK_SYMBOLS 白名單內）：
    AI_DESK_AUTO_APPROVE=true python run_ai_desk_once.py ETHUSDT 4h

切換到 API 版（Phase 2 排程用，按量計費）：
    AI_DESK_LLM=api ANTHROPIC_API_KEY=sk-ant-... python run_ai_desk_once.py

費用注意：預設 CLI 版走 Max 訂閱額度（免額外計費，但吃訂閱用量上限）；每輪 4 次呼叫。
"""
import os
import sys

import pandas as pd

from binance.client import Client
from config import Config
from core.futures_execution_engineer import FuturesExecutionEngineer
from core.market_analyst import fetch_klines
from core.risk_officer import RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.auto import run_auto_cycle
from ai_desk.desk import run_one_cycle
from ai_desk.llm_client import AnthropicLLMClient, ClaudeCliClient
from ai_desk.memory import ThesisMemory

MEMORY_DIR = os.path.join("ai_desk", "memory")
EQUITY_FOR_SIZING = 10_000.0   # Phase 1 名目資金（測試網虛擬資金基準）

KLINE_LIMIT = 1500
"""每次抓的 4h K 棒數（幣安單次上限）＝ 250 天。

為什麼要這麼多：日均線 MA200 需要 200 天（＝1200 根 4h）才有第一個有效值。
原本只抓 400 根（≈66 天），導致 MA120/MA200 永遠算不出來、連 MA60 都只有
7 個有效值（暖機不足卻仍輸出方向給 AI 當證據）。1500 根讓 MA200 有 51 個
有效值——偏少但可用；再多就得分批抓。
"""


def auto_approve_enabled() -> bool:
    return os.getenv("AI_DESK_AUTO_APPROVE", "false").lower() == "true"


def prepare_df(raw: pd.DataFrame) -> pd.DataFrame:
    """fetch_klines 已以 open_time 為 DatetimeIndex；這裡只丟掉最後一根未收盤 K 棒。"""
    return raw.iloc[:-1]


def fetch_live_price(client, symbol: str) -> float | None:
    """抓此刻市場成交價（僅供簡報附註，不參與指標計算）。

    失敗一律回 None——少了這個附註只是 AI 少一項參考，不該讓整輪分析中斷
    （排程無人值守時尤其重要）。
    """
    try:
        return float(client.futures_symbol_ticker(symbol=symbol)["price"])
    except Exception:
        return None


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
    raw = fetch_klines(client, symbol, interval, limit=KLINE_LIMIT, futures=True)
    df = prepare_df(raw)
    live_price = fetch_live_price(client, symbol)

    store = ApprovalStore()
    memory = ThesisMemory(MEMORY_DIR, symbol, interval)
    risk_officer = RiskOfficer(Config())

    if auto_approve_enabled():
        cfg = Config()
        trade_client = Client(cfg.futures_api_key, cfg.futures_api_secret, testnet=True)
        engine = FuturesExecutionEngineer(trade_client, symbol, set_leverage=False)
        auto = run_auto_cycle(
            df, symbol, interval, llm_call=llm, risk_officer=risk_officer,
            equity=EQUITY_FOR_SIZING, memory=memory, approval_store=store,
            engine=engine, live_price=live_price,
        )
        result = auto.cycle
    else:
        result = run_one_cycle(
            df, symbol, interval, llm_call=llm, risk_officer=risk_officer,
            equity=EQUITY_FOR_SIZING, memory=memory, approval_store=store,
            live_price=live_price,
        )
        auto = None

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

    if auto is not None:
        if auto.placed:
            print(f"→ 全自動：已核准並掛單 #{result.proposal_id}")
        elif result.proposal_id is not None:
            print(f"→ 全自動：#{result.proposal_id} 已核准但掛單失敗（{auto.error}）")
    elif result.proposal_id is not None:
        print(f"→ 已進入待核准佇列 #{result.proposal_id}；"
              f"檢視/核准：python -m ai_desk.approval")

    approved = store.approved_unexecuted()
    if approved:
        print(f"⚠ 有 {len(approved)} 筆已核准未執行的提案"
              f"（執行 python run_ai_desk_execute.py，或設 AI_DESK_AUTO_APPROVE=true）")


if __name__ == "__main__":
    main()
