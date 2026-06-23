"""最佳化/泛化檢驗進入點。

用法：
    # 離線示範（不連幣安，用合成資料驗證流程會動）
    python run_optimize.py ema_cross --synthetic

    # 真資料（抓長歷史，建議先 --cache 存檔避免反覆打 API）
    python run_optimize.py ema_cross --start "6 months ago UTC" --cache btc_1h.csv
    python run_optimize.py zscore_revert --cache btc_1h.csv   # 用快取就不再連線

輸出兩段：全樣本參數掃描（敏感度）、walk-forward（樣本外泛化）。
"""
import os
import argparse
import numpy as np
import pandas as pd
from config import Config
from core.market_analyst import (make_client, fetch_historical_klines,
                                 load_klines, save_klines)
from core.risk_officer import RiskOfficer
from backtest.optimize import sweep, walk_forward, param_grid


# 各策略的搜尋網格（要加參數就往這裡加）
GRIDS = {
    "ema_cross": {
        "fast": [8, 12, 16],
        "slow": [26, 34, 50],            # slow≈3×fast 降 whipsaw
        "sep_atr_k": [0.0, 0.5, 1.0],    # EMA 交叉緩衝帶（0=裸交叉）
    },
    "zscore_revert": {
        "window": [30, 50, 80],
        "entry_z": [1.5, 2.0, 2.5, 3.0],
        "exit_z": [0.2, 0.5],
    },
    "zscore_ls": {                       # 多空雙向版（allow_short=True）
        "window": [30, 50, 80],
        "entry_z": [1.5, 2.0, 2.5, 3.0],
        "exit_z": [0.2, 0.5],
    },
    "fib_retracement": {                 # swing pivot + 順勢回調（allow_short=True）
        "pivot_left": [2, 3],
        "pivot_right": [2, 3],
        "ema_trend_period": [100, 200],
    },
    "supertrend": {                      # ATR 趨勢跟蹤（allow_short=True）；經典值 10/3
        "period": [7, 10, 14],
        "multiplier": [2.0, 2.5, 3.0],
    },
    "donchian": {                        # 海龜通道突破（allow_short=True）；經典 20/10、55/20
        "entry_period": [10, 20, 55],
        "exit_period": [5, 10, 20],
    },
    "of_momentum": {                     # CVD 訂單流動量（allow_short=True）；短線用
        "cvd_fast": [5, 10, 20],
        "cvd_slow": [30, 50, 100],
    },
}


# 風控參數的小型搜尋網格（--include-risk 時併入策略網格）。
# ATR 停損倍數 / R 停利倍數 / Chandelier 倍數——避免手挑單一值過擬合，但刻意各只放 2~3 個值。
RISK_GRID = {
    "atr_mult_sl": [1.5, 2.0, 2.5],
    "tp_R_mult": [1.5, 2.0, 3.0],
    "chand_mult": [2.5, 3.0],
}


def make_synthetic(n: int = 8000, seed: int = 7) -> pd.DataFrame:
    """合成隨機漫步 K 線，純為離線驗證流程。隨機資料本就不該有穩定 alpha。"""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0001, 0.012, n)
    close = 30000 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    op = np.r_[close[0], close[:-1]]
    vol = rng.lognormal(3, 0.5, n)
    idx = pd.date_range("2025-06-01", periods=n, freq="15min")
    return pd.DataFrame({"open": op, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def get_data(args, cfg) -> pd.DataFrame:
    if args.synthetic:
        df = make_synthetic()
        print(f"[合成資料] {len(df)} 根 K 線（離線示範用）")
        return df
    if args.cache and os.path.exists(args.cache):
        df = load_klines(args.cache)
        print(f"[快取] 讀入 {len(df)} 根 K 線：{args.cache}")
        return df
    client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
    df = fetch_historical_klines(client, cfg.symbol, cfg.interval, args.start)
    print(f"[幣安] 抓到 {len(df)} 根 {cfg.interval} K 線：{df.index[0]} ~ {df.index[-1]}")
    if args.cache:
        save_klines(df, args.cache)
        print(f"已快取到 {args.cache}")
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("strategy", nargs="?", default="ema_cross",
                    choices=list(GRIDS))
    ap.add_argument("--synthetic", action="store_true", help="用合成資料離線測試")
    ap.add_argument("--start", default="6 months ago UTC", help="歷史起點")
    ap.add_argument("--cache", default="", help="K 線快取 CSV 路徑")
    ap.add_argument("--objective", default="sharpe",
                    choices=["sharpe", "return", "return_dd"])
    ap.add_argument("--train", type=int, default=2000, help="每 fold 訓練 K 線數")
    ap.add_argument("--test", type=int, default=500, help="每 fold 測試 K 線數")
    ap.add_argument("--include-risk", action="store_true",
                    help="把風控參數（停損/停利）也併入搜尋網格一起掃描")
    ap.add_argument("--plot", action="store_true",
                    help="把參數掃描畫成熱圖 PNG（heatmap_<策略>.png）")
    args = ap.parse_args()

    cfg = Config()
    cfg.strategy = args.strategy
    df = get_data(args, cfg)
    space = GRIDS[args.strategy]
    if args.include_risk:
        space = {**space, **RISK_GRID}
        print(f"[含風控網格] 併入 {list(RISK_GRID)}；總組合數 = {len(param_grid(space))}")
    risk = RiskOfficer(cfg)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)

    # 1) 全樣本參數掃描：看績效對參數有多敏感
    print(f"\n=== 參數掃描（全樣本, 依 {args.objective} 排序）===")
    table = sweep(df, args.strategy, space, risk, cfg, args.objective)
    print(table.head(8).to_string(index=False,
          formatters={"total_return": "{:+.2%}".format,
                      "max_drawdown": "{:.2%}".format,
                      "win_rate": "{:.1%}".format,
                      "sharpe": "{:.2f}".format,
                      "score": "{:.3f}".format}))
    valid = table[np.isfinite(table["score"])]
    if len(valid) > 1:
        print(f"\n組合數 {len(table)}；最佳 {args.objective}={valid['score'].iloc[0]:.3f}、"
              f"中位={valid['score'].median():.3f}。兩者差距越大，越可能是運氣/過擬合。")

    if args.plot:
        from core.plotting import plot_heatmap
        keys = list(space)
        metric_col = {"return": "total_return", "return_dd": "score"}.get(args.objective, "sharpe")
        xcol, ycol = keys[0], keys[1]   # 取網格前兩個參數，其餘以平均邊際化
        path = f"heatmap_{args.strategy}.png"
        plot_heatmap(table, xcol, ycol, metric=metric_col, path=path,
                     title=f"{args.strategy} {metric_col}  ({ycol} x {xcol})")
        print(f"已存參數掃描熱圖 → {path}")

    # 2) Walk-forward：訓練段選參數，只在後續測試段評估
    print(f"\n=== Walk-forward（樣本外, train={args.train}/test={args.test}）===")
    wf = walk_forward(df, args.strategy, space, risk, cfg,
                      args.train, args.test, args.objective)
    if wf.empty:
        print("資料長度不足，切不出 fold。請抓更長歷史，或調小 --train/--test。")
        return
    p_cols = [c for c in wf.columns if c.startswith("p_")]   # 含被選中的策略+風控參數
    show = wf[["fold", "test_start", "test_end"] + p_cols +
              ["IS_return", "OOS_return", "OOS_sharpe", "OOS_maxDD", "OOS_trades"]]
    print(show.to_string(index=False,
          formatters={"IS_return": "{:+.2%}".format,
                      "OOS_return": "{:+.2%}".format,
                      "OOS_sharpe": "{:.2f}".format,
                      "OOS_maxDD": "{:.2%}".format}))

    is_ret, oos_ret = wf["IS_return"].mean(), wf["OOS_return"].mean()
    print("\n--- 彙總 ---")
    print(f"fold 數           : {len(wf)}")
    print(f"IS  平均報酬      : {is_ret:+.2%}")
    print(f"OOS 平均報酬      : {oos_ret:+.2%}")
    print(f"OOS 為正比例      : {(wf['OOS_return'] > 0).mean():.0%}")
    print(f"OOS 平均 Sharpe   : {wf['OOS_sharpe'].mean():.2f}")
    print(f"IS→OOS 報酬衰減   : {is_ret - oos_ret:+.2%}（衰減越大＝越過擬合）")
    print("\n判讀：若 IS 漂亮但 OOS 接近 0/為負、且每個 fold 的最佳參數一直跳，"
          "代表這些『數據值』沒有穩定道理，只是貼合了歷史。")


if __name__ == "__main__":
    main()
