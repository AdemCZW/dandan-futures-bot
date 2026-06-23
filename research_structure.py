"""市場結構（訂單流）確認層 — walk-forward A/B 驗證。

對照組（baseline）：fib_retracement 純 TA（use_structure=False）
實驗組（treatment）：疊上訂單流確認閘門（use_structure=True，掃描門檻）

用幣安【主網】合約公開 K 線（含 taker_base 主動買量，免金鑰、有完整歷史）。
只比較「樣本外（OOS）」彙總，回答：加這層有沒有讓泛化變好？

用法：
    python research_structure.py                 # 預設 15m、9 個月
    python research_structure.py --interval 5m --months 6
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


def fetch_futures_klines(symbol: str, interval: str, start_str: str) -> pd.DataFrame:
    """主網合約歷史 K 線（含 taker_base）。公開資料免金鑰。"""
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


def get_data(interval: str, months: int) -> pd.DataFrame:
    cache = f"btc_{interval}_futures_{months}mo.csv"
    if os.path.exists(cache):
        df = load_klines(cache)
        print(f"[快取] {len(df)} 根 {interval} K 線：{cache}")
        return df
    print(f"[幣安主網] 抓 {months} 個月 {interval} 合約 K 線中…（含 taker_base）")
    df = fetch_futures_klines("BTCUSDT", interval, f"{months * 30} days ago UTC")
    save_klines(df, cache)
    print(f"[幣安主網] {len(df)} 根：{df.index[0]} ~ {df.index[-1]}，已快取 {cache}")
    return df


# 對照組：純 TA。實驗組：疊訂單流閘門（掃描小網格的門檻，避免手挑單值過擬合）。
BASE_GRID = {
    "pivot_left": [2, 3], "pivot_right": [2, 3], "ema_trend_period": [100, 200],
}
STRUCT_GRID = {
    **BASE_GRID,
    "use_structure": [True],
    "of_smooth": [20],
    "of_long_min": [0.45, 0.50],
    "of_short_max": [0.50, 0.55],
}


def summarize(label, wf):
    if wf.empty:
        print(f"{label}: 切不出 fold")
        return None
    is_ret, oos_ret = wf["IS_return"].mean(), wf["OOS_return"].mean()
    pos = (wf["OOS_return"] > 0).mean()
    shp = wf["OOS_sharpe"].mean()
    dd = wf["OOS_maxDD"].mean()
    trades = wf["OOS_trades"].sum()
    print(f"\n── {label} ──")
    print(f"  fold 數          : {len(wf)}")
    print(f"  OOS 平均報酬     : {oos_ret:+.3%}")
    print(f"  OOS 為正比例     : {pos:.0%}")
    print(f"  OOS 平均 Sharpe  : {shp:.3f}")
    print(f"  OOS 平均回撤     : {dd:.2%}")
    print(f"  OOS 總交易數     : {int(trades)}")
    print(f"  IS→OOS 衰減      : {is_ret - oos_ret:+.3%}")
    return {"oos_ret": oos_ret, "pos": pos, "sharpe": shp, "dd": dd, "trades": int(trades)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--months", type=int, default=9)
    ap.add_argument("--train", type=int, default=3000)
    ap.add_argument("--test", type=int, default=1000)
    ap.add_argument("--objective", default="sharpe")
    args = ap.parse_args()

    cfg = Config()
    cfg.strategy = "fib_retracement"
    df = get_data(args.interval, args.months)
    risk = RiskOfficer(cfg)

    print(f"\n=== Walk-forward A/B（{args.interval}, train={args.train}/test={args.test}, "
          f"objective={args.objective}）===")
    wf_base = walk_forward(df, "fib_retracement", BASE_GRID, risk, cfg,
                           args.train, args.test, args.objective)
    wf_struct = walk_forward(df, "fib_retracement", STRUCT_GRID, risk, cfg,
                             args.train, args.test, args.objective)

    a = summarize("對照組 純 TA（baseline）", wf_base)
    b = summarize("實驗組 ＋訂單流閘門（treatment）", wf_struct)

    if a and b:
        print("\n=== 結論 ===")
        d_ret = b["oos_ret"] - a["oos_ret"]
        d_shp = b["sharpe"] - a["sharpe"]
        print(f"  OOS 報酬差    : {d_ret:+.3%}（實驗 − 對照）")
        print(f"  OOS Sharpe 差 : {d_shp:+.3f}")
        verdict = ("✓ 訂單流閘門【有改善】泛化" if (d_ret > 0 and d_shp > 0)
                   else "✗ 訂單流閘門【未改善】（或惡化）泛化"
                   if (d_ret < 0 and d_shp < 0)
                   else "≈ 訊號混雜，改善不明確")
        print(f"  判讀          : {verdict}")


if __name__ == "__main__":
    main()
