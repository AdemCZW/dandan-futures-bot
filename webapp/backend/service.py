"""服務層 — 橋接既有的 core / backtest 模組，回傳 JSON 可序列化的資料。

不含任何 Web 框架相依，方便單獨測試。資料來源支援：
  - synthetic：離線合成資料（不需金鑰、不連網），適合 demo / 開發。
  - testnet  ：幣安現貨測試網公開 K 線（需網路，不需金鑰）。
"""
from __future__ import annotations
import dataclasses
import math
import os
import sqlite3

from config import Config
from core.quant_researcher import STRATEGIES, build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest
from backtest.optimize import sweep, walk_forward
from run_optimize import make_synthetic, GRIDS


def list_strategies() -> list[dict]:
    return [{"name": name, "defaults": cls.defaults,
             "allow_short": getattr(cls, "allow_short", False)}
            for name, cls in STRATEGIES.items()]


def _get_data(source: str, cfg: Config, limit: int):
    if source == "synthetic":
        return make_synthetic()
    # testnet 公開行情（不需金鑰）
    from core.market_analyst import make_client, fetch_klines
    client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
    return fetch_klines(client, cfg.symbol, cfg.interval, limit=limit)


def _equity_points(eq, max_points: int = 600) -> list[dict]:
    """權益曲線降採樣成前端好畫的點陣列。"""
    n = len(eq)
    step = max(1, n // max_points)
    pts = []
    for i in range(0, n, step):
        ts = eq.index[i]
        pts.append({"t": str(ts), "equity": round(float(eq.iloc[i]), 2)})
    if pts and pts[-1]["t"] != str(eq.index[-1]):
        pts.append({"t": str(eq.index[-1]), "equity": round(float(eq.iloc[-1]), 2)})
    return pts


def _trades_out(trades: list[dict]) -> list[dict]:
    out = []
    for t in trades:
        out.append({
            "ts": str(t.get("ts", "")),
            "side": t.get("side", ""),
            "dir": int(t.get("dir", 1)),
            "price": round(float(t.get("price", 0.0)), 2),
            "qty": round(float(t.get("qty", 0.0)), 6),
            "pnl": round(float(t.get("pnl", 0.0)), 2),
        })
    return out


def run_backtest_api(strategy: str, symbol: str, interval: str,
                     params: dict | None = None, source: str = "synthetic",
                     limit: int = 1000) -> dict:
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略 {strategy}，可用：{list(STRATEGIES)}")
    cfg = Config()
    cfg.strategy, cfg.symbol, cfg.interval = strategy, symbol, interval
    df = _get_data(source, cfg, limit)
    strat = build_strategy(strategy, **(params or {}))
    result = run_backtest(df, strat, RiskOfficer(cfg), cfg)
    return {
        "strategy": strategy, "symbol": symbol, "interval": interval, "source": source,
        "bars": int(len(df)),
        "start": str(df.index[0]), "end": str(df.index[-1]),
        "metrics": {
            "total_return": result.total_return,
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "sharpe": result.sharpe,
            "trades": len(result.trades),
        },
        "equity": _equity_points(result.equity_curve),
        "trades": _trades_out(result.trades),
    }


_PIPELINE = [
    {"role": "市場分析師", "module": "market_analyst", "does": "提供已收完的 K 線（價、量）"},
    {"role": "信號工程師", "module": "signal_engineer", "does": "算 EMA / RSI / ATR / z-score"},
    {"role": "量化研究員", "module": "quant_researcher", "does": "依指標產生目標倉位 +1/0/-1"},
    {"role": "風控官", "module": "risk_officer", "does": "准入與否、倉位大小、停損停利、單日熔斷"},
    {"role": "執行工程師", "module": "backtester", "does": "依目標對齊倉位、含手續費+滑點成交"},
]


def run_explain_api(strategy: str, symbol: str, interval: str,
                    params: dict | None = None, source: str = "synthetic",
                    limit: int = 1000, only_decisions: bool = True,
                    max_steps: int = 400) -> dict:
    """跑帶決策軌跡的回測，回傳 6 角色 SOP 流程 + 每個位置的逐關決策。"""
    if strategy not in STRATEGIES:
        raise ValueError(f"未知策略 {strategy}，可用：{list(STRATEGIES)}")
    cfg = Config()
    cfg.strategy, cfg.symbol, cfg.interval = strategy, symbol, interval
    df = _get_data(source, cfg, limit)
    strat = build_strategy(strategy, **(params or {}))
    trace: list = []
    result = run_backtest(df, strat, RiskOfficer(cfg), cfg, trace=trace)

    def is_decision(s):
        return bool({a["act"] for a in s["actions"]} - {"hold", "flat"})

    steps = [s for s in trace if is_decision(s)] if only_decisions else trace
    steps = steps[:max_steps]
    return {
        "strategy": strategy, "symbol": symbol, "interval": interval, "source": source,
        "bars": int(len(df)), "total_traced": len(trace), "decision_points": len(steps),
        "only_decisions": only_decisions,
        "pipeline": _PIPELINE,
        "metrics": {
            "total_return": result.total_return, "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate, "sharpe": result.sharpe, "trades": len(result.trades),
        },
        "steps": steps,
    }


def run_optimize_api(strategy: str, source: str = "synthetic",
                     objective: str = "sharpe", train: int = 2000,
                     test: int = 500, limit: int = 1000) -> dict:
    if strategy not in GRIDS:
        raise ValueError(f"無此策略的搜尋網格：{strategy}，可用：{list(GRIDS)}")
    cfg = Config()
    cfg.strategy, cfg.symbol = strategy, cfg.symbol
    df = _get_data(source, cfg, limit)
    space = GRIDS[strategy]
    risk = RiskOfficer(cfg)
    table = sweep(df, strategy, space, risk, cfg, objective)
    keys = list(space)
    xcol, ycol = keys[0], keys[1]
    metric_col = {"return": "total_return", "return_dd": "score"}.get(objective, "sharpe")

    # 熱圖網格（xcol × ycol，其餘參數取平均）
    piv = table.pivot_table(index=ycol, columns=xcol, values=metric_col, aggfunc="mean")
    heatmap = {
        "xlabel": xcol, "ylabel": ycol, "metric": metric_col,
        "xticks": [str(c) for c in piv.columns],
        "yticks": [str(i) for i in piv.index],
        "grid": [[None if (v != v) else round(float(v), 3) for v in row] for row in piv.values],
    }

    wf = walk_forward(df, strategy, space, risk, cfg, train, test, objective)
    if wf.empty:
        wf_summary = {"folds": 0}
        folds = []
    else:
        folds = [{
            "fold": int(r["fold"]),
            "test_start": str(r["test_start"]), "test_end": str(r["test_end"]),
            "IS_return": round(float(r["IS_return"]), 4),
            "OOS_return": round(float(r["OOS_return"]), 4),
            "OOS_sharpe": round(float(r["OOS_sharpe"]), 2),
            "OOS_trades": int(r["OOS_trades"]),
        } for _, r in wf.iterrows()]
        wf_summary = {
            "folds": int(len(wf)),
            "IS_mean": round(float(wf["IS_return"].mean()), 4),
            "OOS_mean": round(float(wf["OOS_return"].mean()), 4),
            "OOS_positive_ratio": round(float((wf["OOS_return"] > 0).mean()), 3),
            "decay": round(float(wf["IS_return"].mean() - wf["OOS_return"].mean()), 4),
        }

    top = table.head(10).replace({float("-inf"): None}).to_dict(orient="records")
    for row in top:
        for k, v in list(row.items()):
            if isinstance(v, float):
                row[k] = None if (v != v) else round(v, 4)
    return {
        "strategy": strategy, "source": source, "objective": objective,
        "combos": int(len(table)), "heatmap": heatmap,
        "top": top, "walkforward": {"summary": wf_summary, "folds": folds},
    }


_RAILWAY_BOT_URL = os.getenv("RAILWAY_BOT_URL", "").rstrip("/")


def _fetch_railway_trades(limit: int = 50, mode: str | None = None) -> list[dict]:
    """從 Railway bot /trades 端點抓近期成交（Railway bot 部署後才有效）。"""
    import json, urllib.request
    if not _RAILWAY_BOT_URL:
        return []
    try:
        qs = f"limit={limit}"
        if mode:
            qs += f"&mode={mode}"
        url = f"{_RAILWAY_BOT_URL}/trades?{qs}"
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def live_status(state_path: str = None) -> dict:
    """即時監控：自動選用最近活躍的 bot（paper / futures testnet）狀態，回傳權益與 SOP。

    優先順序：
      1. RAILWAY_BOT_URL（.env 設定後從雲端 Railway bot 取狀態）
      2. 本機 bot_state_futures.json（2 分鐘內有更新）
      3. 本機 bot_state_paper.json
    """
    import json
    import urllib.request
    from datetime import datetime, timezone

    PAPER_PATH = "bot_state_paper.json"
    FUTURES_PATH = "bot_state_futures.json"

    def _load(path):
        if not os.path.exists(path):
            return {}
        try:
            with open(path) as fh:
                st = json.load(fh)
            return st if isinstance(st, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _ts(st):
        try:
            return datetime.fromisoformat(st.get("updated_at", ""))
        except (ValueError, TypeError, AttributeError):
            return datetime.min.replace(tzinfo=timezone.utc)

    def _fetch_railway() -> dict:
        if not _RAILWAY_BOT_URL:
            return {}
        try:
            url = f"{_RAILWAY_BOT_URL}/state"
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read())
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    if state_path:
        st = _load(state_path)
    else:
        # 1. 先嘗試 Railway 雲端 bot
        st = _fetch_railway()
        if not st:
            now = datetime.now(timezone.utc)

            def _age_s(s):
                try:
                    return (now - _ts(s)).total_seconds()
                except Exception:
                    return float("inf")

            paper_st, futures_st = _load(PAPER_PATH), _load(FUTURES_PATH)
            # 合約 bot 只要在 2 分鐘內更新過，就優先顯示（兩個 bot 同跑時合約優先）
            if futures_st and _age_s(futures_st) < 120:
                st = futures_st
            elif paper_st:
                st = paper_st
            else:
                st = futures_st or {}

    def _fin(x):                           # NaN/Inf/非數 → None（避免非法 JSON 與 500）
        try:
            return float(x) if x is not None and math.isfinite(float(x)) else None
        except (TypeError, ValueError):
            return None

    mode = st.get("mode", "paper")         # "paper" 或 "futures"
    symbol = st.get("symbol", "BTCUSDT")
    interval = st.get("interval", "5m")
    price = _fin(st.get("last_price"))
    try:                                   # 抓即時現價（公開行情、免金鑰）
        from core.market_analyst import make_client, fetch_klines
        live_price = _fin(fetch_klines(make_client("", "", testnet=True), symbol, interval, 2)["close"].iloc[-1])
        if live_price is not None:
            price = live_price
    except Exception:                      # noqa: BLE001 — 連線失敗時退回狀態檔最後價
        pass

    cash, base = _fin(st.get("cash")), _fin(st.get("base")) or 0.0
    in_pos = bool(st.get("in_position", False))
    entry = _fin(st.get("entry_price")) or 0.0
    direction = int(st.get("direction", 1 if in_pos else 0))   # +1/0/-1，合約空倉為 -1

    if mode == "futures":
        equity = _fin(cash)                # 合約：cash = USDT 保證金餘額，不再乘以持倉量
    else:
        equity = _fin(cash + base * price) if (cash is not None and price is not None) else None

    if in_pos and price is not None and entry:
        if direction == -1:                # 空單：entry 高 → price 低 → 正盈利
            unreal = _fin((entry - price) * base)
        else:
            unreal = _fin((price - entry) * base)
    else:
        unreal = 0.0

    age = None
    updated = st.get("updated_at")
    if updated:
        try:
            age = round((datetime.now(timezone.utc) - datetime.fromisoformat(updated)).total_seconds(), 1)
        except (ValueError, TypeError):
            age = None

    trades_mode = "live_futures_testnet" if mode == "futures" else "paper"
    # 若有 Railway URL，近期成交從雲端抓；否則讀本機 DB
    if _RAILWAY_BOT_URL and mode == "futures":
        recent = _fetch_railway_trades(limit=15, mode=trades_mode)
    else:
        recent = read_trades(limit=15, mode=trades_mode, db_path="trades.db")
    return {
        "active": bool(st),
        "mode": mode,
        "symbol": symbol, "interval": interval, "strategy": st.get("strategy"),
        "in_position": in_pos, "direction": direction,
        "entry_price": round(entry, 2), "sl": st.get("sl"), "tp": st.get("tp"),
        "cash": round(cash, 2) if cash is not None else None,
        "base": round(base, 6), "price": round(price, 2) if price is not None else None,
        "equity": round(equity, 2) if equity is not None else None,
        "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
        "updated_at": updated, "age_seconds": age, "poll": st.get("poll"),
        "last_decision": st.get("last_decision"),
        "recent_trades": recent,
    }


def read_trades(limit: int = 50, mode: str | None = None,
                db_path: str = "trades.db") -> list[dict]:
    import os
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = "SELECT ts, mode, symbol, strategy, side, price, qty, pnl FROM trades"
    args: list = []
    if mode:
        q += " WHERE mode = ?"
        args.append(mode)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    try:
        rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    except sqlite3.Error:              # OperationalError / DatabaseError（檔案損毀非 sqlite）等一律回空
        rows = []
    finally:
        conn.close()
    return rows  # ORDER BY id DESC → 最新在最前
