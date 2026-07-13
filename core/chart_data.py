"""圖表資料層（輕量）— K 線 + 費波那契通道 + 交易標記，供前端 TradingView 式圖表使用。

刻意只依賴輕量模組（pandas / urllib / signal_engineer / trade_journal），不碰
backtest / optuna / matplotlib 等肥依賴，這樣「只跑 bot 的雲端容器」也能直接 import
並吐圖表資料，不必為了畫圖背上一整套回測相依 → 記憶體不膨脹。

dashboard 的 service.py 與 bot 容器的 HTTP 路由都從這裡取用，單一真相來源不分岔。
"""
from __future__ import annotations
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.trade_journal import read_trades_db

_now = time.time   # 可在測試裡替換，控制「現在幾點」

# 2026-07-06：真實 fetch（source="testnet"，其實是打 fapi.binance.com 公開合約 API）
# 原本每次呼叫都重新打 Binance、完全沒有快取或退避——測試+部署期間反覆打 /ma6/
# /klines 把伺服器共用 IP 打到被 Binance 回 418(IP已被封)。加短 TTL 快取（K 線在
# 這麼短時間內內容實質不變）+ 429/418 退避（記錄封鎖到期時間，封鎖中有舊資料就
# 先給舊的、沒有就明確報錯，不再重複觸發/延長封鎖）。
_KLINE_CACHE: dict = {}            # (symbol, interval, limit) -> (fetched_at, df)
_KLINE_CACHE_TTL = 30.0            # 秒
_BINANCE_BACKOFF = {"blocked_until": 0.0}
_BACKOFF_DEFAULT_S = {429: 60.0, 418: 300.0}   # 418 代表已經觸發封鎖，預設值故意比 429 長
_MA_WARMUP = 150                   # ma6 圖表多抓的暖機根數（>MA120，讓回傳每根都算得出長均線）


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


def _fetch_ohlcv_df(symbol: str, interval: str, limit: int, source: str):
    """抓 K 線 → 標準 OHLCV DataFrame（供 klines_data / ma6_overlay_data 共用，避免重複）。

    source="synthetic" 離線合成（測試用，lazy-import 才不背回測相依）；
    source="testnet" 抓幣安期貨公開 K 線（免金鑰）——短 TTL 快取 + 429/418 退避，
    見模組開頭註解。
    """
    import pandas as pd

    if source == "synthetic":
        from run_optimize import make_synthetic       # lazy：合成資料才需要（本機測試）
        return make_synthetic(limit)

    key = (symbol, interval, limit)
    now = _now()
    cached = _KLINE_CACHE.get(key)
    if cached is not None and now - cached[0] < _KLINE_CACHE_TTL:
        return cached[1].copy()

    if now < _BINANCE_BACKOFF["blocked_until"]:
        if cached is not None:
            return cached[1].copy()
        remain = _BINANCE_BACKOFF["blocked_until"] - now
        raise RuntimeError(f"Binance 公開 API 退避中，還剩 {remain:.0f} 秒（避免延長封鎖）")

    url = (f"https://fapi.binance.com/fapi/v1/klines"
           f"?symbol={symbol}&interval={interval}&limit={limit}")
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            raw = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (429, 418):
            retry_after = e.headers.get("Retry-After") if e.headers else None
            try:
                backoff_s = float(retry_after) if retry_after is not None else None
            except ValueError:
                backoff_s = None
            if backoff_s is None:
                backoff_s = _BACKOFF_DEFAULT_S.get(e.code, 60.0)
            _BINANCE_BACKOFF["blocked_until"] = now + backoff_s
        if cached is not None:
            return cached[1].copy()
        raise

    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c])
    df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.set_index("timestamp")
    _KLINE_CACHE[key] = (now, df)
    return df.copy()


def klines_data(symbol: str = "BTCUSDT", interval: str = "4h",
                limit: int = 200, source: str = "testnet") -> dict:
    """K 線 + 指標資料 — 供前端 TradingView 式圖表使用。

    回傳 lightweight-charts 格式：{time, open/high/low/close} 蠟燭 +
    supertrend_bull/bear + ema_fast/slow/trend + donchian + 費波那契單一通道各線。
    """
    from core import signal_engineer as se

    df = _fetch_ohlcv_df(symbol, interval, limit, source)

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
    fib_single = se.fib_regression_channel(df, lookback=60)

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


def ma6_overlay_data(symbol: str = "BTCUSDT", interval: str = "4h",
                     limit: int = 300, source: str = "testnet") -> dict:
    """六線密集/發散圖表資料（2026-07-05，使用者要求還原 YouTube 雙均線系統）。

    直接重用 MaConvergencePullbackStrategy.prepare()（單一事實來源）——不重寫
    一份平行的狀態機（否則圖表跟真實訊號兩邊各自維護、容易悄悄不同步）。

    2026-07-06：圖表面板明確開啟 require_density_for_breakout=True，修正
    is_breakout 誤把「已經在半路的強趨勢」標成密集突破的 bug（見
    tests/test_ma_convergence_pullback.py::test_breakout_requires_prior_density_when_enabled）。
    此開關預設關閉，b9（LINKUSDT 觀察倉）實際下單依據的 trend_dir / is_first_pullback
    暫時不受影響、逐位元維持現行線上行為——圖表跟 b9 實盤在這個修正上刻意分岔，
    等使用者確認要讓 b9 也吃這個修正後再回頭打開。

    回傳 lightweight-charts 格式：candles + ma20/ma60/ma120/ema20/ema60/ema120
    六條線 + ma6_signals（三型進場訊號：breakout 密集突破/pullback1 首次回踩/
    pullback2 二次回踩，各帶 dir=+1/-1）+ density（六線密集區逐根布林，供前端標示）。
    b9 實際只下單首次回踩（pullback1）；breakout/pullback2 為圖上顯示供評估。
    """
    from core.quant_researcher import build_strategy
    from core import signal_engineer as se

    # 多抓 _MA_WARMUP 根暖機：MA120/EMA120 等長週期指標需要前置歷史才算得出來。
    # 只抓 limit 根的話，最左邊 ~120 根的 MA120 會是 NaN（畫不出線），往左捲就跟
    # 交易所（無限歷史、每根都有 MA120）看起來不一樣。作法＝抓 limit+暖機、算完
    # 指標後只回傳最後 limit 根（暖機根在視窗外丟掉），這樣回傳的每根都完全暖機。
    fetch_n = limit + _MA_WARMUP
    df = _fetch_ohlcv_df(symbol, interval, fetch_n, source)
    strat = build_strategy("ma_convergence_pullback",
                           require_density_for_breakout=True)
    out = strat.prepare(df)
    density_thresh = float(strat.params["density_thresh"])
    divergence_thresh = float(strat.params["divergence_thresh"])
    # 裁切點：只顯示最後 limit 根（不足 limit+暖機 時全留）。
    cutoff_ts = int(out.index[-limit].timestamp()) if len(out) > limit else None

    def _ts(idx):
        return int(idx.timestamp())

    def _f(v):
        try:
            x = float(v)
            return None if (x != x) else x
        except Exception:
            return None

    lines = {k: [] for k in ("ma20", "ma60", "ma120", "ema20", "ema60", "ema120")}
    candles, signals, density, spread = [], [], [], []
    # 欄位 → 訊號型別（依序判斷；一根最多歸一型，突破優先）
    sig_cols = (("is_breakout", "breakout"),
                ("is_first_pullback", "pullback1"),
                ("is_second_pullback", "pullback2"))

    # 斐波那契「單一乾淨通道」（與 K線圖表頁 klines_data 用同一份 fib_regression_channel，
    # 零軸 fib_ch_0＝趨勢原點：上升→支撐、下降→壓力；一軸 fib_ch_100 為對側目標；
    # 中間比率 0.236/0.382/0.5/0.618/0.786 為結構層級，>1 為延伸目標）。
    fib_cols = list(se.FIB_CHANNEL_RATIOS.values())
    fib_series = {col: [] for col in fib_cols}
    fib_single = se.fib_regression_channel(df, lookback=60)
    _fc = None
    fib_dir = 0
    if fib_single is not None:
        _fc = (fib_single["anchor_idx"], fib_single["anchor_price"],
               fib_single["slope"], fib_single["width"], fib_single["dir"])
        fib_dir = int(fib_single["dir"])

    for pos, (idx, row) in enumerate(out.iterrows()):
        t = _ts(idx)
        if cutoff_ts is not None and t < cutoff_ts:
            continue                                     # 暖機根：只用來算指標，不回傳
        o, h, lo, c = _f(row["open"]), _f(row["high"]), _f(row["low"]), _f(row["close"])
        if None in (o, h, lo, c):
            continue
        candles.append({"time": t, "open": o, "high": h, "low": lo, "close": c})
        for k in lines:
            if (v := _f(row[k])) is not None:
                lines[k].append({"time": t, "value": v})
        trend_dir = _f(row.get("trend_dir"))
        for col, typ in sig_cols:
            if bool(row.get(col, False)) and trend_dir is not None:
                signals.append({"time": t, "dir": 1 if trend_dir > 0 else -1, "type": typ})
                break
        if bool(row.get("is_density", False)):
            density.append({"time": t, "value": c})
        # 六線發散度（入場訊號子圖）：spread=(六線max−min)/close，收斂(密集)→發散(趨勢)
        if (sv := _f(row.get("spread"))) is not None:
            spread.append({"time": t, "value": sv})
        if _fc is not None:
            a_idx, a_px, slope, width, sdir = _fc
            base = a_px + slope * (pos - a_idx)          # 0 線在當根（直線拉滿）
            for r, col in se.FIB_CHANNEL_RATIOS.items():
                fib_series[col].append({"time": t, "value": base + sdir * r * width})

    # 水平斐波那契回撤層（2026-07-12，對齊分析師 TradingView 畫法）：擺動高低點錨定，
    # 0=行情起點（下跌段=高點/上漲段=低點），與斜的迴歸通道是兩個獨立圖層。
    retr = se.fib_swing_retracement(df, lookback=min(180, len(df)))
    fib_retr = {"dir": 0, "levels": []}
    if retr is not None:
        fib_retr = {"dir": int(retr["dir"]),
                    "levels": [{"ratio": float(r), "price": float(p)}
                               for r, p in retr["levels"].items()]}

    return {"candles": candles, **lines, "ma6_signals": signals, "density": density,
            "fib_channel": fib_series, "fib_dir": fib_dir, "fib_retracement": fib_retr,
            "spread": spread, "spread_density_thresh": density_thresh,
            "spread_divergence_thresh": divergence_thresh}
