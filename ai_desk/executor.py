"""Phase 2 執行層 — 把「已核准」的提案掛到 Binance Futures **Testnet**。

設計見 docs/superpowers/specs/2026-07-20-ai-desk-phase2-execution-design.md

本檔的原則是**用硬規則擋死**，不依賴呼叫端自律：這是整套系統唯一會真的送出
委託的地方，前面各層出錯頂多是「建議很爛」，這裡出錯是「掛了殭屍單/裸倉」。

目前已實作：安全護欄（幣種白名單、testnet 強制驗證）。
掛單/對帳等後續分片依設計文件逐步加入。
"""
from __future__ import annotations

import os

DEFAULT_SYMBOLS = frozenset({"ETHUSDT"})
"""白名單預設值。

刻意不含 BTCUSDT/SOLUSDT/DOTUSDT —— 既有 bot 歷史上跑過這些幣，而 bot 有
「孤兒倉接管」機制（core/trade_journal.py 的 RECONCILED_EXIT_SIDES）：bot 重啟時
會把交易所上它沒有本地紀錄的部位當成自己的孤兒倉接管甚至平掉。若 ai_desk 與
bot 用到同一幣種，會互相破壞部位、並污染 bot 的乾淨勝率統計。
"""


def allowed_symbols() -> frozenset:
    """從 AI_DESK_SYMBOLS 讀白名單（逗號分隔，大小寫不敏感）。未設或空 → 預設值。"""
    raw = os.getenv("AI_DESK_SYMBOLS", "")
    syms = {s.strip().upper() for s in raw.split(",") if s.strip()}
    return frozenset(syms) if syms else DEFAULT_SYMBOLS


def assert_symbol_allowed(symbol: str) -> None:
    """幣種不在白名單 → 拒絕。防止與既有 bot 搶同一帳戶的部位。"""
    allowed = allowed_symbols()
    if symbol.upper() not in allowed:
        raise ValueError(
            f"{symbol} 不在 ai_desk 白名單 {sorted(allowed)}；"
            "拒絕執行（避免與既有 bot 搶同一測試網帳戶的部位）。"
            "要放行請設定環境變數 AI_DESK_SYMBOLS。"
        )


def assert_testnet(client) -> None:
    """驗證 client 實際會打到 testnet，否則拒絕。

    不可改用 client.FUTURES_URL 判斷：那是類別層級常數，即使 testnet=True 也可能
    顯示主網位址（實測如此），具誤導性。唯一可信的是實際組出的請求網址。
    """
    build = getattr(client, "_create_futures_api_uri", None)
    if not callable(build):
        raise RuntimeError(
            "無法驗證此 client 是否為 testnet（缺 _create_futures_api_uri）；拒絕執行。")
    uri = str(build("order"))
    if "testnet" not in uri:
        raise RuntimeError(
            f"client 實際請求網址不是 testnet：{uri}；拒絕執行。"
            "ai_desk 僅允許在 Binance Futures testnet 上運作。")
