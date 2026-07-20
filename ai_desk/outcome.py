"""提案結果結算 — 用真實 K 線回推「若當初照提案掛單會怎樣」。

**這是紙上推演,不是真實成交**：ai_desk 只提案、不執行（執行接線屬 Phase 2）。
本模組存在的目的是讓你能前瞻累積樣本、看 AI 判斷準不準，不代表帳戶真有這些損益。

純函式、不碰網路：K 線由呼叫端傳入，方便離線測試。
"""
from __future__ import annotations


def evaluate_outcome(*, direction: int, entry: float, stop: float,
                     take_profit: float, qty: float, klines) -> dict:
    """回傳 {"state", "pnl", "filled_at", "closed_at"}。

    state：
      no_data  — 提案後還沒有 K 線
      unfilled — 價格從未觸及進場價（限價單沒成交）
      stopped  — 成交後觸及停損
      target   — 成交後觸及停利
      open     — 成交後兩者都沒觸及，以最新收盤計未實現損益

    同一根同時觸及停損與停利時，保守判為 stopped（不美化績效）。
    """
    if klines is None or len(klines) == 0:
        return {"state": "no_data", "pnl": 0.0, "filled_at": None, "closed_at": None}

    # 進場：空單為賣出限價（漲到 entry 才成交）；多單為買進限價（跌到 entry 才成交）
    fill_mask = klines["high"] >= entry if direction == -1 else klines["low"] <= entry
    filled = klines[fill_mask]
    if filled.empty:
        return {"state": "unfilled", "pnl": 0.0, "filled_at": None, "closed_at": None}

    filled_at = filled.index[0]
    post = klines[klines.index >= filled_at]

    if direction == -1:
        stop_hits = post[post["high"] >= stop]
        tp_hits = post[post["low"] <= take_profit]
    else:
        stop_hits = post[post["low"] <= stop]
        tp_hits = post[post["high"] >= take_profit]

    stop_at = stop_hits.index[0] if not stop_hits.empty else None
    tp_at = tp_hits.index[0] if not tp_hits.empty else None

    # 同一根同時觸及 → 保守取停損
    if stop_at is not None and (tp_at is None or stop_at <= tp_at):
        pnl = -abs(stop - entry) * qty
        return {"state": "stopped", "pnl": pnl,
                "filled_at": str(filled_at), "closed_at": str(stop_at)}
    if tp_at is not None:
        pnl = abs(entry - take_profit) * qty
        return {"state": "target", "pnl": pnl,
                "filled_at": str(filled_at), "closed_at": str(tp_at)}

    last = float(post["close"].iloc[-1])
    pnl = (entry - last) * qty if direction == -1 else (last - entry) * qty
    return {"state": "open", "pnl": pnl,
            "filled_at": str(filled_at), "closed_at": None}


_CLOSED = {"stopped", "target"}


def summarize(rows) -> dict:
    """彙總一組結算結果。已結算(停損/停利)才計入勝率；未成交不計。"""
    closed = [r for r in rows if r["state"] in _CLOSED]
    wins = [r for r in closed if r["state"] == "target"]
    losses = [r for r in closed if r["state"] == "stopped"]
    open_rows = [r for r in rows if r["state"] == "open"]
    return {
        "total": len(rows),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "realized_pnl": sum(r["pnl"] for r in closed),
        "open_pnl": sum(r["pnl"] for r in open_rows),
    }
