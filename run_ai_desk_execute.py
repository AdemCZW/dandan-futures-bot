"""ai_desk Phase 2 執行進入點 — 把已核准的提案真的掛到 Binance Futures Testnet。

總開關預設關閉：即使這支程式寫好了、被呼叫了，AI_DESK_EXEC_ENABLED 沒設成
"true" 就什麼都不會做（連交易所連線都不建立）。要真的送出委託必須明確開啟。

用法：
    AI_DESK_EXEC_ENABLED=true python run_ai_desk_execute.py
    （幣種取自 ai_desk.executor.allowed_symbols()，即 AI_DESK_SYMBOLS 環境變數）

單輪跑完即退出（仿 run_once.py 慣例），適合排程重複呼叫。
"""
import os
import sys

from binance.client import Client
from config import Config
from core.futures_execution_engineer import FuturesExecutionEngineer

from ai_desk.approval import ApprovalStore
from ai_desk.executor import allowed_symbols, run_execution_pass


def build_engines(client, symbols) -> dict:
    """每個白名單幣種各建一個 FuturesExecutionEngineer。"""
    return {s: FuturesExecutionEngineer(client, s, set_leverage=False) for s in symbols}


def report_result(result: dict) -> int:
    """印出一輪的結果，回傳行程退出碼（有錯誤 → 非零）。

    run_execution_pass 現在會隔離單筆失敗、把原因收進 result["errors"] 後繼續跑。
    好處是一筆失敗不再讓整輪停擺，代價是失敗不會再自己中止程式——所以這裡必須
    主動、完整地把原因印出來，否則就從「大聲中止」退化成「靜默失敗」，
    排程 log 只剩一句「本輪結束」，比原本更難察覺。
    """
    for w in result["warnings"]:
        print(w)
    print(f"新掛單：{result['placed']}")
    print(f"已有部位而跳過：{result['skipped']}")
    print(f"本輪成交：{result['filled']}")
    print(f"逾時撤單：{result['expired']}")

    errors = result.get("errors") or []
    if not errors:
        return 0
    print(f"\n❌ 本輪有 {len(errors)} 筆失敗（已隔離，其餘提案照常處理）：")
    for e in errors:
        print(f"  · {e}")
    return 1


def main() -> None:
    if os.getenv("AI_DESK_EXEC_ENABLED", "false").lower() != "true":
        print("AI_DESK_EXEC_ENABLED 未開啟，不執行任何動作。"
              "（這是刻意的安全預設：程式寫好了也不會真的送出委託）")
        return

    cfg = Config()
    client = Client(cfg.futures_api_key, cfg.futures_api_secret,
                    testnet=True, ping=False)
    symbols = sorted(allowed_symbols())
    print(f"白名單幣種：{symbols}")

    store = ApprovalStore()
    engines = build_engines(client, symbols)
    result = run_execution_pass(store, engines)
    code = report_result(result)
    if code:
        sys.exit(code)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"執行中止：{type(e).__name__}: {e}", file=sys.stderr)
        raise
