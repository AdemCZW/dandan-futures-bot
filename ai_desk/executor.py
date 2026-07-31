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


# ── 執行編排 ────────────────────────────────────────────────
def place_entry(row: dict, store, engine) -> None:
    """把 approved 的提案掛成限價進場單。

    row：store.get(pid) 的字典。engine：FuturesExecutionEngineer 相容物件
    （round_qty/round_price/client）。安全檢查一律在送出委託前做，
    任何一項失敗都不會建立委託、不會改動提案狀態。
    """
    assert_symbol_allowed(row["symbol"])
    assert_testnet(engine.client)
    params = limit_order_params(
        row["symbol"], row["direction"], row["qty"], row["entry"],
        round_qty=engine.round_qty, round_price=engine.round_price)
    resp = engine.client.futures_create_order(**params)
    store.mark_placed(row["id"], resp["orderId"])


def handle_placed(row: dict, store, engine, *, ttl_hours: int = DEFAULT_TTL_HOURS,
                  now=None) -> str:
    """處理一筆 placed 狀態的提案：查交易所實況、決定動作、執行。

    回傳實際採取的動作字串（"fill" / "expire" / "wait"），方便呼叫端記錄。
    交易所查無此單（例如 closePosition 單成交後被自動撤銷、查不到）視同「已消失」，
    以 expire 處理——交易所是唯一真相來源，查不到就不該再等它成交。
    """
    try:
        order = engine.get_order(row["exchange_order_id"])
    except Exception:
        order = {"status": "CANCELED"}
    action = decide_action(
        status="placed",
        has_position=(engine.position_amt() != 0),
        order_status=order.get("status"),
        placed_at=row["decided_at"],
        now=now, ttl_hours=ttl_hours,
    )
    if action == ACTION_FILL:
        store.mark_filled(row["id"])
        _protect_with_stop(row, store, engine)
    elif action == ACTION_EXPIRE:
        try:
            engine.cancel_order(row["exchange_order_id"])
        except Exception:
            pass   # 單子可能已經不存在（交易所端已消失才會判定 expire），容忍
        store.mark_expired(row["id"])
    return action


def _protect_with_stop(row: dict, store, engine) -> None:
    """成交後立刻掛停損停利；掛停損失敗 → 絕不留裸倉，立刻市價平倉。

    必須帶 qty（帶量 + reduceOnly），不可用 closePosition（qty=None）：實測這個
    帳戶的 closePosition 條件單會撞 -4045（Reach max stop order limit）——
    root cause 是舊 bot 艦隊留下的 98 筆孤兒 algo 委託（見
    core/futures_execution_engineer.py 的 stop_order_params 註解）佔滿了
    closePosition 專用名額，futures_get_open_orders() 完全看不到、
    futures_cancel_all_open_orders() 也撤不掉（它們活在另一套 algo order
    系統）。帶量單走一般委託額度，不受此限制。

    真實損益需要對帳補正（此處無法可靠算出平倉均價），先記 None，交由
    Phase 2 對帳補正——不可用假數字冒充。
    """
    qty = abs(engine.position_amt())
    try:
        engine.place_stop(row["direction"], row["stop"], qty=qty)
        engine.place_take_profit(row["direction"], row["take_profit"], qty=qty)
    except Exception as e:
        qty = abs(engine.position_amt())
        engine.close(qty, row["direction"])
        store.mark_closed(row["id"], "closed_manual", realized_pnl=None)
        raise RuntimeError(
            f"掛停損失敗（{e}），為避免裸倉已立刻市價平倉 #{row['id']}；"
            "真實損益待對帳補正。") from e


# ── 對帳 ────────────────────────────────────────────────────
def reconcile_orphan_positions(engines: dict, store) -> list:
    """檢查每個白名單幣種是否有「交易所有部位、但本地無 filled 中提案追蹤」的孤兒倉。

    只告警、絕不接管或平倉——這不是 ai_desk 自己的部位就不該碰，
    避免誤動到既有 bot 或人工操作留下的倉位。

    engines：{symbol: engine}，僅檢查傳入的幣種。
    """
    tracked = {r["symbol"] for r in store.by_status("filled")}
    warnings = []
    for symbol, engine in engines.items():
        amt = engine.position_amt()
        if amt != 0 and symbol not in tracked:
            warnings.append(
                f"⚠️ {symbol} 交易所有部位（amt={amt}）但本地無對應追蹤中的提案"
                "——不接管、不平倉，僅告警，請人工確認來源。")
    return warnings


# ── 一輪執行 ────────────────────────────────────────────────
def run_execution_pass(store, engines: dict, *, ttl_hours: int = DEFAULT_TTL_HOURS,
                       now=None) -> dict:
    """跑一輪：對帳孤兒倉 → 該掛的掛（approved）→ 該處理的處理（placed）。

    engines：{symbol: engine}。只處理有傳入 engine 的幣種，其餘一律不碰
    （即使資料庫裡有其他幣種的提案）。回傳各動作影響到的提案 id，方便記錄/測試。

    單筆失敗一律隔離：錯誤收進 result["errors"] 後繼續處理下一筆，絕不讓一筆
    炸掉整輪。實際事故（2026-07-24~30）是 ETHUSDT 掛停損收到 -4045，例外穿出
    本函式，導致其後所有提案連續多日完全不被處理。理由與
    scheduling/run_ai_desk_scheduled.sh 刻意不用 set -e 相同。
    注意這裡只縮小炸開範圍：place_entry 的安全檢查與 _protect_with_stop 的
    「絕不裸倉 + 大聲拋錯」行為都不變，錯誤也不靜默吞掉。
    """
    result = {"warnings": [], "placed": [], "skipped": [], "filled": [],
              "expired": [], "errors": []}
    result["warnings"] = reconcile_orphan_positions(engines, store)

    for symbol, engine in engines.items():
        has_position = engine.position_amt() != 0
        for row in store.by_status("approved"):
            if row["symbol"] != symbol:
                continue
            if has_position:
                result["skipped"].append(row["id"])
                continue
            try:
                place_entry(row, store, engine)
            except Exception as e:
                result["errors"].append(f"#{row['id']} {symbol} 掛進場單失敗：{e}")
                continue
            result["placed"].append(row["id"])

        for row in store.by_status("placed"):
            if row["symbol"] != symbol:
                continue
            try:
                action = handle_placed(row, store, engine,
                                       ttl_hours=ttl_hours, now=now)
            except Exception as e:
                result["errors"].append(f"#{row['id']} {symbol} 處理掛單失敗：{e}")
                continue
            if action == ACTION_FILL:
                result["filled"].append(row["id"])
            elif action == ACTION_EXPIRE:
                result["expired"].append(row["id"])

    return result
