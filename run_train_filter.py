"""CLI：訓練 XGBoost ML Filter 並儲存模型。

改進點：
  - signals_from_prepared 支援三個 bot 策略（fib_ema / fib_channel / trend_pullback）
  - ATR 自適應 Triple Barrier（--atr-mult）
  - 多標的合併訓練（--symbols SOLUSDT,ETHUSDT,BNBUSDT）
  - 公開主網合約 API 分頁抓歷史資料（免金鑰，預設 2 年）

用法：
    # 三個 bot 策略各自訓練（推薦）
    python run_train_filter.py fib_ema      --interval 15m --symbols SOLUSDT,ETHUSDT,BNBUSDT
    python run_train_filter.py fib_channel  --interval 15m --symbols SOLUSDT,ETHUSDT,BNBUSDT
    python run_train_filter.py trend_pullback --interval 1h --symbols SOLUSDT,ETHUSDT,BNBUSDT

    # 快速煙霧測試（合成資料）
    python run_train_filter.py fib_ema --synthetic
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from core.quant_researcher import build_strategy
from backtest.vbt_optimize import signals_from_prepared
from ml.triple_barrier import label_triple_barrier
from ml.purged_kfold import PurgedKFold
from ml.ml_filter import extract_features, train_filter, save_filter, FEATURE_COLS

FAPI = "https://fapi.binance.com/fapi/v1/klines"
LIMIT_PER_REQ = 1500    # 幣安單次最大 K 線數


def fetch_public_klines(symbol: str, interval: str, days: int = 730) -> pd.DataFrame:
    """從幣安公開合約 API 分頁抓歷史 K 線（免 API key）。

    days=730 ≈ 2 年；15m 約 70,080 根，1h 約 17,520 根。
    自動分頁，每次最多 1500 根，加小延遲避免觸發限速。
    """
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    all_rows: list[list] = []

    cur = start_ms
    while cur < end_ms:
        url = (f"{FAPI}?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit={LIMIT_PER_REQ}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"  [警告] {symbol} 抓取失敗：{e}，跳過此批次")
            break
        if not batch:
            break
        all_rows.extend(batch)
        cur = int(batch[-1][6]) + 1   # close_time + 1ms → 下一批起點
        time.sleep(0.1)               # 避免觸發 IP 限速

    if not all_rows:
        return pd.DataFrame()

    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(all_rows, columns=cols)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates("open_time").set_index("open_time")
    df.index = df.index.tz_localize(None)
    return df[["open", "high", "low", "close", "volume"]]


def make_synthetic(n: int = 8000, seed: int = 7) -> pd.DataFrame:
    rng   = np.random.default_rng(seed)
    rets  = rng.normal(0.0002, 0.012, n)
    close = 30000 * np.exp(np.cumsum(rets))
    high  = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low   = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    op    = np.r_[close[0], close[:-1]]
    vol   = rng.lognormal(3, 0.5, n)
    idx   = pd.date_range("2023-01-01", periods=n, freq="15min")
    return pd.DataFrame({"open": op, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def process_symbol(df: pd.DataFrame, strategy_name: str,
                   pt: float, sl: float, vb: int, atr_mult: float) -> tuple:
    """對單一標的跑 prepare → entry events → Triple Barrier 標記 → 特徵擷取。

    回傳 (X, y, t1)；若訊號不足回傳 None。
    """
    strat    = build_strategy(strategy_name)
    prepared = strat.prepare(df).dropna()
    if len(prepared) < 100:
        return None

    try:
        le, _, se, _ = signals_from_prepared(prepared, strategy_name)
        # 合併多空 entry（都是潛在交易點，標記是對稱的）
        entries = le | se
        events  = prepared.index[entries]
    except Exception as e:
        print(f"  [警告] signals_from_prepared 失敗：{e}")
        events = pd.Index([])

    if len(events) < 20:
        print(f"  [跳過] 訊號只有 {len(events)} 筆，不足 20")
        return None

    # ATR 自適應屏障（若 atr 欄位存在）
    atr_series = prepared.get("atr") if "atr" in prepared.columns else None

    labels = label_triple_barrier(
        prepared["close"], events,
        pt=pt, sl=sl, vb=vb,
        atr=atr_series, atr_mult=atr_mult,
    )

    X = extract_features(prepared, events)
    y = labels

    # PurgedKFold 需要 t1（標記結束時間）
    t1_times = []
    for t in events:
        loc = prepared.index.get_loc(t)
        end = min(loc + vb, len(prepared) - 1)
        t1_times.append(prepared.index[end])
    t1 = pd.Series(t1_times, index=events)

    return X, y, t1


def main() -> None:
    ap = argparse.ArgumentParser(description="Train XGBoost ML Filter")
    ap.add_argument("strategy", help="fib_ema / fib_channel / trend_pullback")
    ap.add_argument("--symbols",   default="SOLUSDT,ETHUSDT,BNBUSDT",
                    help="逗號分隔的標的，預設 SOLUSDT,ETHUSDT,BNBUSDT")
    ap.add_argument("--interval",  default="15m",  help="K 線週期（e.g. 15m / 1h）")
    ap.add_argument("--days",      type=int, default=730, help="歷史天數（預設 2 年）")
    ap.add_argument("--synthetic", action="store_true",  help="使用合成資料（快速煙霧測試）")
    ap.add_argument("--output",    default="",   help="模型輸出路徑（預設 models/<strategy>.pkl）")
    ap.add_argument("--pt",        type=float, default=0.02, help="Triple Barrier 獲利上限（下限）")
    ap.add_argument("--sl",        type=float, default=0.02, help="Triple Barrier 止損下限（下限）")
    ap.add_argument("--vb",        type=int,   default=24,   help="垂直屏障寬度（bar 數）")
    ap.add_argument("--atr-mult",  type=float, default=2.0,  help="ATR 自適應倍數")
    ap.add_argument("--splits",    type=int,   default=5,    help="PurgedKFold folds")
    ap.add_argument("--embargo",   type=float, default=0.01, help="Embargo 比例")
    args = ap.parse_args()

    output = args.output or f"models/{args.strategy}.pkl"
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    print(f"策略：{args.strategy}  |  標的：{symbols}  |  週期：{args.interval}")
    print(f"Triple Barrier: pt≥{args.pt}  sl≥{args.sl}  vb={args.vb}bars  ATR×{args.atr_mult}")

    all_X: list[pd.DataFrame] = []
    all_y: list[pd.Series]    = []
    all_t1: list[pd.Series]   = []

    # ── 1. 逐標的抓資料 + 處理 ──
    for sym in symbols:
        if args.synthetic:
            df = make_synthetic()
            print(f"[{sym}] 合成資料 {len(df)} 根")
        else:
            print(f"[{sym}] 抓 {args.days} 天 {args.interval} K 線…", end=" ", flush=True)
            df = fetch_public_klines(sym, args.interval, args.days)
            if df.empty:
                print("失敗，跳過")
                continue
            print(f"{len(df)} 根")

        result = process_symbol(df, args.strategy, args.pt, args.sl,
                                args.vb, args.atr_mult)
        if result is None:
            continue
        X, y, t1 = result
        print(f"  訊號 {len(y)} 筆  |  +1={( y==1).sum()}  -1={(y==-1).sum()}  0={(y==0).sum()}")
        all_X.append(X)
        all_y.append(y)
        all_t1.append(t1)

    if not all_X:
        print("⚠️  所有標的均無有效訊號，終止訓練")
        sys.exit(1)

    X_full  = pd.concat(all_X,  ignore_index=True)
    y_full  = pd.concat(all_y,  ignore_index=True)
    t1_full = pd.concat(all_t1, ignore_index=True)
    print(f"\n合計 {len(X_full)} 筆樣本  "
          f"|  +1={( y_full==1).sum()}  -1={(y_full==-1).sum()}  0={(y_full==0).sum()}")

    # ── 2. CV 評估 ──
    if len(X_full) >= args.splits * 20:
        from sklearn.model_selection import cross_val_score
        _cv_model = train_filter(X_full, y_full)
        if len(symbols) == 1:
            # 單標的：PurgedKFold（真正防洩漏）
            cv = PurgedKFold(n_splits=args.splits, t1=t1_full, pct_embargo=args.embargo)
        else:
            # 多標的合併：時間軸交錯，改用 StratifiedKFold
            from sklearn.model_selection import StratifiedKFold
            cv = StratifiedKFold(n_splits=args.splits, shuffle=False)
        try:
            scores = cross_val_score(_cv_model, X_full.values,
                                     (y_full == 1).astype(int), cv=cv)
            cv_name = "PurgedKFold" if len(symbols) == 1 else "StratifiedKFold"
            print(f"[CV {cv_name}] accuracy: {scores.mean():.3f} ± {scores.std():.3f}")
        except Exception as e:
            print(f"[CV 略過] {e}")
    else:
        print("[CV 略過] 樣本不足")

    # ── 3. 全樣本訓練 + 儲存 ──
    model = train_filter(X_full, y_full)
    save_filter(model, output)
    print(f"\n[完成] 模型儲存至 {output}")

    print("特徵重要度：")
    imp = dict(zip(FEATURE_COLS, model.feature_importances_))
    for k, v in sorted(imp.items(), key=lambda x: -x[1]):
        if v > 0:
            print(f"  {k:<15} {v:.4f}")


if __name__ == "__main__":
    main()
