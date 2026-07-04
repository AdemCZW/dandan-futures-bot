"""即時監控資料層（輕量）— 單台 bot 的 state + 交易統計 enrichment。

刻意只依賴輕量模組（math / datetime / trade_journal），不碰回測相依，讓「只跑 bot
的雲端容器」能直接吐出與舊 dashboard /api/live 相同 shape 的資料，前端 Live 卡不用大改。

與 dashboard 版 live_status 的差別：這裡是「單台自己」的視角——直接讀該台 state dict
與自己的 DB 紀錄（strategy+symbol 過濾），不做跨服務 railway 代理、不選 paper/futures。
"""
from __future__ import annotations
import math
from datetime import datetime, timezone

from core.trade_journal import read_trades_db


def _fin(x):
    """NaN/Inf/非數 → None（避免非法 JSON 與 500）。"""
    try:
        return float(x) if x is not None and math.isfinite(float(x)) else None
    except (TypeError, ValueError):
        return None


def trade_stats(all_hist: list[dict], init_capital: float = 5000.0) -> dict:
    """全量成交列 → 進階統計：最大回撤%、每筆夏普、多/空拆分勝率損益。

    輸入順序與 read_trades_db 一致（newest-first / id DESC）；內部反轉成時間正序後，
    用 entry/entry_short 狀態機把每筆平倉事件（exit_* 或 scale_out，pnl!=0）歸到當下開倉方向。
    """
    chrono = list(reversed(all_hist))   # newest-first → 時間正序

    def _ts(t):
        """ts 字串 → naive UTC datetime。DB 混雜帶時區（新碼）與不帶（舊碼）的格式，
        aware−naive 相減會拋 TypeError（b7 現場事故 2026-07-05）→ 一律正規化為 naive UTC。"""
        try:
            dt = datetime.fromisoformat(str(t.get("ts", "")).strip())
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    cur_dir = 0
    entry_ts = None
    events: list[tuple[int, float, float | None]] = []
    hold_hours: list[float] = []
    for t in chrono:
        side = str(t.get("side", ""))
        pnl = t.get("pnl")
        if side == "entry":
            cur_dir = 1
            entry_ts = _ts(t)
        elif side == "entry_short":
            cur_dir = -1
            entry_ts = _ts(t)
        elif side == "scale_out" or side.startswith("exit"):
            if pnl is not None and pnl != 0:
                try:
                    notional = float(t.get("price") or 0) * float(t.get("qty") or 0)
                except (TypeError, ValueError):
                    notional = 0.0
                events.append((cur_dir, float(pnl), notional or None))
            if side.startswith("exit"):
                ex_ts = _ts(t)
                if entry_ts is not None and ex_ts is not None:
                    dh = (ex_ts - entry_ts).total_seconds() / 3600
                    if dh >= 0:
                        hold_hours.append(dh)
                cur_dir = 0
                entry_ts = None

    long_pnls  = [pnl for d, pnl, _ in events if d == 1]
    short_pnls = [pnl for d, pnl, _ in events if d == -1]

    max_dd_pct = None
    if events:
        running = peak = init_capital
        worst = 0.0
        for _, pnl, _ in events:
            running += pnl
            if running > peak:
                peak = running
            if peak > 0:
                worst = max(worst, (peak - running) / peak)
        max_dd_pct = round(worst * 100, 2)

    sharpe = None
    rois = [pnl / notional for _, pnl, notional in events if notional]
    if len(rois) >= 2:
        mean = sum(rois) / len(rois)
        var = sum((r - mean) ** 2 for r in rois) / (len(rois) - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = round(mean / std, 3)

    # 完善數據（2026-07-05）：期望值/獲利因子/平均賺虧/最大連虧/平均持倉時長
    pnls = [pnl for _, pnl, _ in events]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    expectancy = round(sum(pnls) / len(pnls), 4) if pnls else None
    gross_loss = -sum(losses)
    profit_factor = (round(sum(wins) / gross_loss, 3)
                     if wins and gross_loss > 0 else None)   # 無虧損 → None（∞ 非合法 JSON）
    avg_win = round(sum(wins) / len(wins), 2) if wins else None
    avg_loss = round(sum(losses) / len(losses), 2) if losses else None
    max_consec = cur = 0
    for p in pnls:
        cur = cur + 1 if p <= 0 else 0
        max_consec = max(max_consec, cur)
    avg_hold = round(sum(hold_hours) / len(hold_hours), 1) if hold_hours else None

    return {
        "max_drawdown_pct": _fin(max_dd_pct),
        "sharpe": _fin(sharpe),
        "expectancy": _fin(expectancy),
        "profit_factor": _fin(profit_factor),
        "avg_win": _fin(avg_win),
        "avg_loss": _fin(avg_loss),
        "max_consec_losses": max_consec,
        "avg_hold_hours": _fin(avg_hold),
        "long_trades": len(long_pnls),
        "long_wins": sum(1 for p in long_pnls if p > 0),
        "long_pnl": round(sum(long_pnls), 2),
        "short_trades": len(short_pnls),
        "short_wins": sum(1 for p in short_pnls if p > 0),
        "short_pnl": round(sum(short_pnls), 2),
    }


def bot_live_status(state: dict, strategy: str, symbol: str, interval: str,
                    init_capital: float = 5000.0, live_price=None,
                    db_path: str = "trades.db") -> dict:
    """單台 bot 的即時監控 enrich（與舊 dashboard /api/live 同 shape）。

    state：該台 state 檔解析出的 dict；strategy/symbol/interval：該台身分（DB 過濾用）。
    live_price：可選即時價（None → 用 state.last_price；前端另用幣安 WS 補即時跳動）。
    """
    st = state or {}
    mode = st.get("mode", "futures")
    price = _fin(st.get("last_price"))
    lp = _fin(live_price)
    if lp is not None:
        price = lp

    cash = _fin(st.get("cash"))
    base = _fin(st.get("base")) or 0.0
    in_pos = bool(st.get("in_position", False))
    entry = _fin(st.get("entry_price")) or 0.0
    direction = int(st.get("direction", 1 if in_pos else 0))

    if mode == "futures":
        equity = _fin(cash)                # 合約：cash = USDT 保證金餘額
    else:
        equity = _fin(cash + base * price) if (cash is not None and price is not None) else None

    if in_pos and price is not None and entry:
        unreal = _fin((entry - price) * base) if direction == -1 else _fin((price - entry) * base)
    else:
        unreal = 0.0

    age = None
    updated = st.get("updated_at")
    if updated:
        try:
            age = round((datetime.now(timezone.utc)
                         - datetime.fromisoformat(updated)).total_seconds(), 1)
        except (ValueError, TypeError):
            age = None

    # 該台自己的紀錄（strategy+symbol 過濾 = 每台隔離）
    recent   = read_trades_db(limit=30,   strategy=strategy, symbol=symbol, db_path=db_path)
    all_hist = read_trades_db(limit=2000, strategy=strategy, symbol=symbol, db_path=db_path)
    closed = [t for t in all_hist if t.get("pnl") and t["pnl"] != 0]
    realized_pnl = round(sum(t["pnl"] for t in closed), 2)
    total_trades = len(closed)
    win_trades   = sum(1 for t in closed if t["pnl"] > 0)
    stats = trade_stats(all_hist, init_capital)

    return {
        "active": bool(st),
        "mode": mode,
        "symbol": symbol, "interval": interval, "strategy": strategy,
        "in_position": in_pos, "direction": direction,
        "entry_price": round(entry, 2), "sl": st.get("sl"), "tp": st.get("tp"),
        "cash": round(cash, 2) if cash is not None else None,
        "base": round(base, 6), "price": round(price, 2) if price is not None else None,
        "equity": round(equity, 2) if equity is not None else None,
        "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
        "realized_pnl": realized_pnl,
        "total_trades": total_trades,
        "win_trades": win_trades,
        "max_drawdown_pct": stats["max_drawdown_pct"],
        "sharpe": stats["sharpe"],
        "expectancy": stats["expectancy"],
        "profit_factor": stats["profit_factor"],
        "avg_win": stats["avg_win"], "avg_loss": stats["avg_loss"],
        "max_consec_losses": stats["max_consec_losses"],
        "avg_hold_hours": stats["avg_hold_hours"],
        "long_trades": stats["long_trades"], "long_wins": stats["long_wins"],
        "long_pnl": stats["long_pnl"],
        "short_trades": stats["short_trades"], "short_wins": stats["short_wins"],
        "short_pnl": stats["short_pnl"],
        "updated_at": updated, "age_seconds": age, "poll": st.get("poll"),
        "last_decision": st.get("last_decision"),
        "recent_trades": recent,
    }
