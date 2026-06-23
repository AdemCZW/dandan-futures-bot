"""策略 × 時間框架 矩陣搜尋 — 找出哪個組合在樣本外(OOS)真的為正。

對每個 (策略, interval) 跑 walk-forward，列出 OOS 彙總排行榜。
誠實目的：fib 在 15m 是 OOS 虧損，先確認有沒有任何組合 OOS 為正，
再把訂單流閘門套到勝出的組合上。

用幣安主網合約公開 K 線（含 taker_base，免金鑰）。
"""
import os
import argparse
import numpy as np
import pandas as pd
from binance.client import Client
from binance.enums import HistoricalKlinesType

from config import Config
from core.market_analyst import save_klines, load_klines
from core.risk_officer import RiskOfficer
from backtest.optimize import walk_forward
from run_optimize import GRIDS


def fetch_futures_klines(symbol, interval, start_str):
    c = Client("", "", testnet=False)
    raw = c.get_historical_klines(symbol, interval, start_str,
                                  klines_type=HistoricalKlinesType.FUTURES)
    cols = ["open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for col in ["open", "high", "low", "close", "volume", "taker_base"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df.set_index("open_time")[["open", "high", "low", "close", "volume", "taker_base"]]


def get_data(interval, months):
    cache = f"btc_{interval}_futures_{months}mo.csv"
    if os.path.exists(cache):
        return load_klines(cache)
    print(f"[抓取] {months}mo {interval} …")
    df = fetch_futures_klines("BTCUSDT", interval, f"{months * 30} days ago UTC")
    save_klines(df, cache)
    print(f"[抓取] {len(df)} 根，快取 {cache}")
    return df


# 各 interval 用相稱的 train/test（高框架資料根數少，窗要縮小）
TF = {
    "15m": {"months": 9, "train": 3000, "test": 1000},
    "1h":  {"months": 12, "train": 1500, "test": 500},
    "4h":  {"months": 18, "train": 600,  "test": 200},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="fib_retracement,ema_cross,zscore_ls,zscore_revert")
    ap.add_argument("--intervals", default="15m,1h,4h")
    ap.add_argument("--objective", default="sharpe")
    ap.add_argument("--fee", type=float, default=None, help="覆寫單邊手續費（真實合約 taker≈0.0005）")
    ap.add_argument("--slippage", type=float, default=None, help="覆寫滑點（真實小單≈0.0002）")
    args = ap.parse_args()

    strategies = args.strategies.split(",")
    intervals = args.intervals.split(",")
    cfg = Config()
    if args.fee is not None:
        cfg.fee_rate = args.fee
    if args.slippage is not None:
        cfg.slippage = args.slippage
    print(f"[成本] fee/side={cfg.fee_rate}  slippage={cfg.slippage}")
    rows = []

    for interval in intervals:
        spec = TF[interval]
        df = get_data(interval, spec["months"])
        for strat in strategies:
            risk = RiskOfficer(cfg)
            wf = walk_forward(df, strat, GRIDS[strat], risk, cfg,
                              spec["train"], spec["test"], args.objective)
            if wf.empty:
                print(f"  {strat:16s} {interval:4s}: 無 fold")
                continue
            oos = wf["OOS_return"].mean()
            rows.append({
                "strategy": strat, "interval": interval, "folds": len(wf),
                "OOS_ret/fold": oos,
                "OOS_pos%": (wf["OOS_return"] > 0).mean(),
                "OOS_sharpe": wf["OOS_sharpe"].mean(),
                "OOS_maxDD": wf["OOS_maxDD"].mean(),
                "OOS_trades": int(wf["OOS_trades"].sum()),
                "decay": wf["IS_return"].mean() - oos,
            })
            print(f"  {strat:16s} {interval:4s}: OOS {oos:+.3%}/fold  "
                  f"pos {(wf['OOS_return']>0).mean():.0%}  Sharpe {wf['OOS_sharpe'].mean():+.2f}")

    tbl = pd.DataFrame(rows).sort_values("OOS_sharpe", ascending=False).reset_index(drop=True)
    print("\n=== OOS 排行榜（依平均 Sharpe）===")
    print(tbl.to_string(index=False, formatters={
        "OOS_ret/fold": "{:+.3%}".format, "OOS_pos%": "{:.0%}".format,
        "OOS_sharpe": "{:+.2f}".format, "OOS_maxDD": "{:.2%}".format,
        "decay": "{:+.3%}".format}))
    winners = tbl[(tbl["OOS_ret/fold"] > 0) & (tbl["OOS_sharpe"] > 0)]
    print(f"\nOOS 為正的組合：{len(winners)} / {len(tbl)}")
    if len(winners):
        print(winners[["strategy", "interval", "OOS_ret/fold", "OOS_sharpe"]].to_string(index=False))


if __name__ == "__main__":
    main()
