"""Phase 2 執行層 — 把「已核准」的提案掛到 Binance Futures **Testnet**。

設計見 docs/superpowers/specs/2026-07-20-ai-desk-phase2-execution-design.md

本檔的原則是**用硬規則擋死**，不依賴呼叫端自律：這是整套系統唯一會真的送出
委託的地方，前面各層出錯頂多是「建議很爛」，這裡出錯是「掛了殭屍單/裸倉」。

目前已實作：安全護欄（幣種白名單、testnet 強制驗證）。
掛單/對帳等後續分片依設計文件逐步加入。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

DEFAULT_TTL_HOURS = 24
"""限價單未成交的存活時間。

提案是基於某一根 4h 收盤的市況做的判斷；隔了 6 根 K 棒（24h）之後，那個判斷
已經過期，不該還掛在市場上等成交。逾時即撤單。
"""

# 決策動作
ACTION_PLACE = "place"                    # 掛出限價進場單
ACTION_SKIP_POSITION = "skip_position_open"  # 該幣已有部位 → 不加碼不反手
ACTION_FILL = "fill"                      # 限價單已成交 → 標記並掛停損停利
ACTION_EXPIRE = "expire"                  # 逾時或交易所端已消失 → 撤單/標記
ACTION_WAIT = "wait"                      # 無事可做

_ORDER_GONE = frozenset({"CANCELED", "EXPIRED", "REJECTED"})

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


# ── 限價進場單 ──────────────────────────────────────────────
def limit_order_params(symbol: str, direction: int, qty: float, price: float,
                       *, round_qty, round_price) -> dict:
    """組出限價進場單參數。

    為什麼一定要限價：提案的核心是「等回抽到 entry 再進」（AI 明確表示不宜追價）。
    若改用市價立即成交，等於把它的判斷做反。

    round_qty / round_price 由呼叫端傳入（實務上用 FuturesExecutionEngineer 的
    交易所精度處理），本函式因此可離線測試。
    """
    if direction not in (1, -1):
        raise ValueError(f"限價進場單方向必須是 1（多）或 -1（空），收到 {direction}")
    return {
        "symbol": symbol,
        "side": "BUY" if direction == 1 else "SELL",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": round_qty(qty),
        "price": round_price(price),
    }


# ── 逾時 ────────────────────────────────────────────────────
def is_expired(placed_at: str, now: datetime | None = None,
               ttl_hours: int = DEFAULT_TTL_HOURS) -> bool:
    """掛單是否已超過存活時間。placed_at 為 ISO 字串。"""
    now = now or datetime.now(timezone.utc)
    t0 = datetime.fromisoformat(placed_at)
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    return (now - t0) > timedelta(hours=ttl_hours)


# ── 動作決策 ────────────────────────────────────────────────
def decide_action(*, status: str, has_position: bool, order_status: str | None = None,
                  placed_at: str | None = None, now: datetime | None = None,
                  ttl_hours: int = DEFAULT_TTL_HOURS) -> str:
    """依提案狀態 + 交易所實況決定下一步。純函式，不碰網路。

    交易所是持倉/訂單的唯一真相來源：order_status 說單子沒了就以它為準，
    不管本地怎麼記。
    """
    if status == "approved":
        # 已有部位就不再進場——不加碼、不反手（硬規則）
        return ACTION_SKIP_POSITION if has_position else ACTION_PLACE

    if status == "placed":
        if order_status == "FILLED":
            return ACTION_FILL
        if order_status in _ORDER_GONE:
            return ACTION_EXPIRE
        if placed_at and is_expired(placed_at, now=now, ttl_hours=ttl_hours):
            return ACTION_EXPIRE
        return ACTION_WAIT

    return ACTION_WAIT
