"""CLI：訓練 XGBoost ML Filter 並儲存模型。

流程：
  1. 讀取歷史 OHLCV（快取 CSV 或幣安 API）
  2. 用指定策略跑 prepare()，取得 entry signal 時間點
  3. Triple Barrier 標記（pt/sl/vb 參數可調）
  4. extract_features + train_filter（XGBoost + PurgedKFold CV）
  5. 儲存 model.pkl 到 --output 路徑

Usage:
    python run_train_filter.py fib_retracement --cache btc_1h.csv --output models/fib_retracement.pkl
    python run_train_filter.py smc_structure   --synthetic --output models/smc_structure.pkl
"""
import argparse
import os
import sys
import numpy as np
import pandas as pd

from config import Config
from core.market_analyst import make_client, fetch_historical_klines, load_klines, save_klines
from core.quant_researcher import build_strategy
from ml.triple_barrier import label_triple_barrier
from ml.purged_kfold import PurgedKFold
from ml.ml_filter import extract_features, train_filter, save_filter, FEATURE_COLS


def make_synthetic(n: int = 8000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.012, n)
    close = 30000 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low  = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    op   = np.r_[close[0], close[:-1]]
    vol  = rng.lognormal(3, 0.5, n)
    idx  = pd.date_range("2025-06-01", periods=n, freq="1h")
    return pd.DataFrame({"open": op, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def main():
    ap = argparse.ArgumentParser(description="Train XGBoost ML Filter")
    ap.add_argument("strategy", help="Strategy name (e.g. fib_retracement)")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--cache",  default="")
    ap.add_argument("--start",  default="12 months ago UTC")
    ap.add_argument("--output", default="models/filter.pkl")
    ap.add_argument("--pt",  type=float, default=0.03,  help="Profit-take ratio")
    ap.add_argument("--sl",  type=float, default=0.03,  help="Stop-loss ratio")
    ap.add_argument("--vb",  type=int,   default=24,    help="Vertical barrier (bars)")
    ap.add_argument("--splits", type=int, default=5,    help="PurgedKFold folds")
    ap.add_argument("--embargo", type=float, default=0.01, help="Embargo pct")
    args = ap.parse_args()

    # ── 1. 資料 ──
    if args.synthetic:
        df = make_synthetic()
        print(f"[合成資料] {len(df)} 根 K 線")
    elif args.cache and os.path.exists(args.cache):
        df = load_klines(args.cache)
        print(f"[快取] {len(df)} 根 K 線：{args.cache}")
    else:
        cfg = Config()
        client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
        df = fetch_historical_klines(client, cfg.symbol, cfg.interval, args.start)
        if args.cache:
            save_klines(df, args.cache)
        print(f"[幣安] {len(df)} 根 K 線")

    # ── 2. prepare + 取 entry events ──
    strat    = build_strategy(args.strategy)
    prepared = strat.prepare(df).dropna()
    print(f"[策略] prepare 完成，{len(prepared)} 根有效 K 線")

    # entry signal = 第一根 long entry bar
    # 用 backtest/vbt_optimize signals_from_prepared 取得 long_entries
    try:
        from backtest.vbt_optimize import signals_from_prepared
        le, _, _, _ = signals_from_prepared(prepared, args.strategy)
        events = prepared.index[le]
    except Exception:
        events = pd.Index([])   # 留給下面的 fallback 處理

    if len(events) < 20:
        print(f"[Entry signals] 策略訊號 {len(events)} 筆不足，退回每 20 根抽樣")
        events = prepared.index[::20]

    print(f"[Entry signals] {len(events)} 個潛在進場點")
    if len(events) < 20:
        print("⚠️  進場訊號太少（<20），建議換策略或延長資料區間")
        sys.exit(1)

    # ── 3. Triple Barrier 標記 ──
    labels = label_triple_barrier(prepared["close"], events,
                                  pt=args.pt, sl=args.sl, vb=args.vb)
    print(f"[Labels] +1={( labels==1).sum()}  -1={(labels==-1).sum()}  0={(labels==0).sum()}")

    # ── 4. 特徵 + 訓練 ──
    X = extract_features(prepared, events)
    y = labels

    # PurgedKFold t1（label 結束時間 = events + vb bars）
    t1_times = []
    for t in events:
        loc = prepared.index.get_loc(t)
        end = min(loc + args.vb, len(prepared) - 1)
        t1_times.append(prepared.index[end])
    t1 = pd.Series(t1_times, index=events)

    cv = PurgedKFold(n_splits=args.splits, t1=t1, pct_embargo=args.embargo)

    # CV 評估
    from sklearn.model_selection import cross_val_score
    from ml.ml_filter import train_filter as _tf
    _model_for_cv = _tf(X, y)
    scores = cross_val_score(_model_for_cv, X, (y == 1).astype(int), cv=cv)
    print(f"[CV PurgedKFold] accuracy: {scores.mean():.3f} ± {scores.std():.3f}")

    # 全樣本訓練
    model = train_filter(X, y)
    save_filter(model, args.output)
    print(f"[完成] 模型儲存至 {args.output}")
    print(f"特徵重要度：")
    imp = dict(zip(FEATURE_COLS, model.feature_importances_))
    for k, v in sorted(imp.items(), key=lambda x: -x[1]):
        print(f"  {k:<15} {v:.4f}")


if __name__ == "__main__":
    main()
