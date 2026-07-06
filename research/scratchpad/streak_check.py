"""驗證：14筆淨虧在已驗證的 smc_structure/4h 籃子裡，是不是正常波動範圍內（2026-07-05）。

用同一套嚴格 walk-forward OOS 方法（跟證實 +0.65 信賴下界那次同一套），
把 506 筆 OOS 交易依時間排序，滑動 14 筆窗口，算「淨虧」窗口出現的頻率。
"""
import sys, os
sys.path.insert(0, "/Users/adem/量化機器")
import pandas as pd, numpy as np
from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest

SYMBOLS = ["SUIUSDT","BTCUSDT","ETHUSDT","ARBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","DOTUSDT"]
CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")
DAYS = 365

def load(symbol, interval):
    p = os.path.join(CACHE, f"{symbol}_{interval}_{DAYS}.csv")
    return pd.read_csv(p, index_col=0, parse_dates=True) if os.path.exists(p) else pd.DataFrame()

def make_cfg():
    return Config(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                  fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                  funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0)

all_trades = []
cfg = make_cfg()
for s in SYMBOLS:
    df = load(s, "4h")
    if df.empty: continue
    res = run_backtest(df.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
    for t in res.trades:
        all_trades.append((t["ts"], t["pnl"]))

all_trades.sort(key=lambda x: x[0])
pnls = [p for _, p in all_trades]
print(f"OOS 全部交易共 {len(pnls)} 筆（跨8幣池化、依時間排序）\n")

N = 14
windows = [pnls[i:i+N] for i in range(len(pnls) - N + 1)]
sums = [sum(w) for w in windows]
loser_windows = sum(1 for s in sums if s < 0)
print(f"任取連續 {N} 筆的窗口，共 {len(windows)} 個")
print(f"淨虧（總和<0）的窗口數：{loser_windows}（{loser_windows/len(windows)*100:.1f}%）")
print(f"最差連續{N}筆的總和：{min(sums):+.1f}")
print(f"最佳連續{N}筆的總和：{max(sums):+.1f}")
print(f"連續{N}筆總和的中位數：{np.median(sums):+.1f}  平均：{np.mean(sums):+.1f}")

# 目前實盤：14筆、勝率21%（3勝11敗）——查回測裡有沒有出現過這麼低的14筆勝率
win_rates_14 = [sum(1 for p in w if p > 0) / N for w in windows]
worse_or_equal = sum(1 for wr in win_rates_14 if wr <= 3/14)
print(f"\n14筆勝率 ≤ 21%（3勝以下）的窗口：{worse_or_equal}/{len(windows)}（{worse_or_equal/len(windows)*100:.1f}%）")
