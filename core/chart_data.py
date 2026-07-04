"""圖表資料層（輕量）— K 線 + 費波那契通道 + 交易標記，供前端 TradingView 式圖表使用。

刻意只依賴輕量模組（pandas / urllib / signal_engineer / trade_journal），不碰
backtest / optuna / matplotlib 等肥依賴，這樣「只跑 bot 的雲端容器」也能直接 import
並吐圖表資料，不必為了畫圖背上一整套回測相依 → 記憶體不膨脹。

dashboard 的 service.py 與 bot 容器的 HTTP 路由都從這裡取用，單一真相來源不分岔。
"""
from __future__ import annotations
from datetime import datetime, timezone

from core.trade_journal import read_trades_db


def parse_ts_unix(ts: str) -> int | None:
    """交易時間字串 → UTC unix 秒。支援 'YYYY-MM-DD HH:MM:SS' 與 ISO8601；失敗回 None。"""
    s = str(ts).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)           # 同時吃 'YYYY-MM-DD HH:MM:SS' 與帶 tz 的 ISO
    except ValueError:
        return None
    if dt.tzinfo is None:                        # 無時區視為 UTC（日誌一律 UTC 記錄）
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def build_trade_markers(trades: list[dict], symbol: str, bucket_hours: int = 6) -> dict:
    """把交易日誌列轉成 K 線標記，每 bucket_hours 小時聚合一個點。

    - 只保留指定 symbol。
    - 每 bucket_hours 小時為一桶（預設 6h）：同桶、同 bot、同進/出場方向的多筆
      聚合成一個點，帶 count（筆數）與均價，避免分鐘級交易在圖上堆疊成柱。
    - side 分類：entry（多 dir=+1 / 空 entry_short dir=−1）、exit（exit_* / scale_out）。
    - 每點帶 strategy + mode（paper / live_futures_testnet / backtest），供前端標明回測。
    - 標記依時間遞增排序（lightweight-charts 要求）。

    回傳 {"markers": [...], "bots": [{strategy, mode, count}, ...]}。
    """
    bucket = max(1, bucket_hours) * 3600
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    bots: dict[str, dict] = {}
    bots_order: list[str] = []

    for t in trades:
        if t.get("symbol") and symbol and t["symbol"] != symbol:
            continue
        unix = parse_ts_unix(t.get("ts", ""))
        if unix is None:
            continue
        side_raw = str(t.get("side", ""))
        if side_raw.startswith("entry"):
            side, direction = "entry", (-1 if "short" in side_raw else 1)
        else:                                    # exit_signal / exit_sltp / scale_out / ...
            side, direction = "exit", 0
        strat = str(t.get("strategy", "") or "—")
        mode  = str(t.get("mode", "") or "—")
        bt    = (unix // bucket) * bucket
        price = float(t.get("price", 0.0))

        key = (strat, side, direction, bt)
        g = groups.get(key)
        if g is None:
            g = {"time": bt, "side": side, "dir": direction, "strategy": strat,
                 "mode": mode, "sum": 0.0, "count": 0}
            groups[key] = g
            order.append(key)
        g["sum"]   += price
        g["count"] += 1

        b = bots.get(strat)
        if b is None:
            bots[strat] = {"strategy": strat, "mode": mode, "count": 0}
            bots_order.append(strat)
        bots[strat]["count"] += 1

    markers = []
    for key in order:
        g = groups[key]
        markers.append({
            "time": g["time"],
            "price": round(g["sum"] / g["count"], 2),
            "side": g["side"],
            "dir": g["dir"],
            "strategy": g["strategy"],
            "mode": g["mode"],
            "count": g["count"],
        })
    markers.sort(key=lambda m: m["time"])
    return {"markers": markers, "bots": [bots[s] for s in bots_order]}


def trade_markers(symbol: str = "BTCUSDT", bucket_hours: int = 6,
                  limit: int = 5000, db_path: str = "trades.db") -> dict:
    """讀交易日誌（全部紀錄）並轉成聚合的 K 線標記。"""
    rows = read_trades_db(limit=limit, db_path=db_path)
    return build_trade_markers(rows, symbol, bucket_hours)


def klines_data(symbol: str = "BTCUSDT", interval: str = "4h",
                limit: int = 200, source: str = "testnet") -> dict:
    """K 線 + 指標資料 — 供前端 TradingView 式圖表使用。

    source="synthetic" 離線合成（測試用，lazy-import 才不背回測相依）；
    source="testnet" 抓幣安期貨公開 K 線（免金鑰）。
    回傳 lightweight-charts 格式：{time, open/high/low/close} 蠟燭 +
    supertrend_bull/bear + ema_fast/slow/trend + donchian + 費波那契單一通道各線。
    """
    import json, urllib.request
    import pandas as pd
    from core import signal_engineer as se

    if source == "synthetic":
        from run_optimize import make_synthetic       # lazy：合成資料才需要（本機測試）
        df = make_synthetic(limit)
    else:
        url = (f"https://fapi.binance.com/fapi/v1/klines"
               f"?symbol={symbol}&interval={interval}&limit={limit}")
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = json.loads(r.read())
        cols = ["open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trades", "taker_buy_base",
                "taker_buy_quote", "ignore"]
        df = pd.DataFrame(raw, columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c])
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
        df = df.set_index("timestamp")

    st = se.supertrend(df, period=10, multiplier=3.0)
    df["st_dir"] = st["st_dir"]
    df["supertrend"] = st["supertrend"]
    df["ema_fast"] = se.ema(df["close"], 9)
    df["ema_slow"] = se.ema(df["close"], 21)
    df["ema_trend"] = se.ema(df["close"], 200)
    don = se.donchian(df, entry_period=20, exit_period=10)
    df["dc_upper"] = don["dc_upper"]
    df["dc_lower"] = don["dc_lower"]
    fib_cols   = list(se.FIB_CHANNEL_RATIOS.values())
    fib_single = se.fib_channel_single(df, pivot_left=5, pivot_right=5)

    def _ts(idx):
        return int(idx.timestamp())

    def _f(v):
        try:
            x = float(v)
            return None if (x != x) else x  # NaN check without math import
        except Exception:
            return None

    candles, st_bull, st_bear = [], [], []
    ema_fast, ema_slow, ema_trend = [], [], []
    dc_upper, dc_lower = [], []
    fib_series = {col: [] for col in fib_cols}

    _fc = None
    if fib_single is not None:
        _fc = (fib_single["anchor_idx"], fib_single["anchor_price"],
               fib_single["slope"], fib_single["width"], fib_single["dir"])

    for pos, (idx, row) in enumerate(df.iterrows()):
        t = _ts(idx)
        o, h, lo, c = _f(row["open"]), _f(row["high"]), _f(row["low"]), _f(row["close"])
        if None in (o, h, lo, c):
            continue
        candles.append({"time": t, "open": o, "high": h, "low": lo, "close": c})
        st_val = _f(row["supertrend"])
        if st_val is not None:
            st_dir = _f(row["st_dir"])
            if st_dir is not None and st_dir > 0:
                st_bull.append({"time": t, "value": st_val})
            else:
                st_bear.append({"time": t, "value": st_val})
        if (v := _f(row["ema_fast"])) is not None:
            ema_fast.append({"time": t, "value": v})
        if (v := _f(row["ema_slow"])) is not None:
            ema_slow.append({"time": t, "value": v})
        if (v := _f(row["ema_trend"])) is not None:
            ema_trend.append({"time": t, "value": v})
        if (v := _f(row["dc_upper"])) is not None:
            dc_upper.append({"time": t, "value": v})
        if (v := _f(row["dc_lower"])) is not None:
            dc_lower.append({"time": t, "value": v})
        if _fc is not None:
            a_idx, a_px, slope, width, sdir = _fc
            base = a_px + slope * (pos - a_idx)         # 0 線在當根（直線）
            for r, col in se.FIB_CHANNEL_RATIOS.items():
                fib_series[col].append({"time": t, "value": base + sdir * r * width})

    return {
        "candles": candles,
        "supertrend_bull": st_bull,
        "supertrend_bear": st_bear,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_trend": ema_trend,
        "donchian_upper": dc_upper,
        "donchian_lower": dc_lower,
        **fib_series,
    }
