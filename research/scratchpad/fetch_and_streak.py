"""重抓8幣4h資料 + 驗證14筆淨虧在已驗證的 smc_structure/4h 籃子裡是否正常波動範圍內。"""
import sys, os, json, time, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, "/Users/adem/量化機器")
import pandas as pd, numpy as np
from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest

SYMBOLS = ["SUIUSDT","BTCUSDT","ETHUSDT","ARBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","DOTUSDT"]
CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")
DAYS = 365
FAPI = "https://fapi.binance.com/fapi/v1/klines"
os.makedirs(CACHE, exist_ok=True)

def fetch(symbol, interval="4h", days=DAYS):
    cache = os.path.join(CACHE, f"{symbol}_{interval}_{days}.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache, index_col=0, parse_dates=True)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    rows, cur = [], start_ms
    while cur < end_ms:
        url = f"{FAPI}?symbol={symbol}&interval={interval}&startTime={cur}&endTime={end_ms}&limit=1500"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"  [warn] {symbol}: {e}"); break
        if not batch: break
        rows.extend(batch); cur = int(batch[-1][6]) + 1; time.sleep(0.05)
    if not rows: return pd.DataFrame()
    cols = ["open_time","open","high","low","close","volume","ct","qv","n","tb","tq","ig"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("open","high","low","close","volume"): df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates("open_time").set_index("open_time")
    df.index = df.index.tz_localize(None)
    out = df[["open","high","low","close","volume"]]
    out.to_csv(cache)
    return out

def make_cfg():
    return Config(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                  fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                  funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0)

print("=== 抓 8 幣 4h 資料 ===")
all_trades = []
cfg = make_cfg()
for s in SYMBOLS:
    df = fetch(s)
    if df.empty:
        print(f"  {s}: 抓取失敗"); continue
    print(f"  {s}: {len(df)} 根")
    res = run_backtest(df.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
    for t in res.trades:
        all_trades.append((t["ts"], t["pnl"]))

all_trades.sort(key=lambda x: x[0])
pnls = [p for _, p in all_trades]
print(f"\nOOS 全部交易共 {len(pnls)} 筆（跨8幣池化、依時間排序，注意：這是全期間非嚴格walk-forward，僅供變異度參考）\n")

N = 14
if len(pnls) >= N:
    windows = [pnls[i:i+N] for i in range(len(pnls) - N + 1)]
    sums = [sum(w) for w in windows]
    loser_windows = sum(1 for s in sums if s < 0)
    print(f"任取連續 {N} 筆的窗口，共 {len(windows)} 個")
    print(f"淨虧（總和<0）的窗口數：{loser_windows}（{loser_windows/len(windows)*100:.1f}%）")
    print(f"最差連續{N}筆總和：{min(sums):+.1f}  最佳：{max(sums):+.1f}")
    print(f"連續{N}筆總和：中位數 {np.median(sums):+.1f}  平均 {np.mean(sums):+.1f}")

    win_rates_14 = [sum(1 for p in w if p > 0) / N for w in windows]
    worse_or_equal = sum(1 for wr in win_rates_14 if wr <= 3/14)
    print(f"\n14筆勝率 ≤ 21%（3勝以下）的窗口：{worse_or_equal}/{len(windows)}（{worse_or_equal/len(windows)*100:.1f}%）")
