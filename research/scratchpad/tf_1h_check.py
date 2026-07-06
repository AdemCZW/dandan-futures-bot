"""驗證 smc_structure 切到 1h 是否還有 edge（2026-07-05，使用者想加快樣本累積）。

同一套嚴格方法：8幣池化、真實成本、rr3(tp_R_mult=3.0)、bootstrap 信賴下界。
判準：1h 的下界要「不明顯低於」4h 的 +1.67，切過去才划算（換更多交易但不丟edge）。
"""
import sys, os, json, time, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, "/Users/adem/量化機器")
import pandas as pd, numpy as np
from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest
from backtest.tournament import _metrics_from_pnls, bootstrap_mean_lower_bound

CORE8 = ["SUIUSDT","BTCUSDT","ETHUSDT","ARBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","DOTUSDT"]
CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")
DAYS = 365
FAPI = "https://fapi.binance.com/fapi/v1/klines"

def fetch(symbol, interval, days=DAYS):
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
            print(f"  [warn] {symbol} {interval}: {e}"); break
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

def make_cfg(interval):
    return Config(interval=interval, risk_per_trade=0.003, futures_leverage=3,
                  fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                  funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0, tp_R_mult=3.0)

def pooled(interval):
    pnls = []
    cfg = make_cfg(interval)
    for s in CORE8:
        df = fetch(s, interval)
        if df.empty: continue
        res = run_backtest(df.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
        pnls.extend([t["pnl"] for t in res.trades])
    return pnls

print("=== smc_structure + rr3：4h vs 1h（8幣池化、真實成本）===\n")
print(f"{'週期':<6}{'筆數':>7}{'勝率':>8}{'期望':>9}{'信賴下界':>10}{'PF':>7}  判決")
for interval in ("4h", "1h"):
    p = pooled(interval)
    m = _metrics_from_pnls(p)
    lb = bootstrap_mean_lower_bound(p)
    v = "✅ 顯著正edge" if lb > 0 else ("⚠ 正但不顯著" if m["expectancy"] > 0 else "❌ 負期望")
    print(f"{interval:<6}{m['trades']:>7}{m['win_rate']:>7.1%}{m['expectancy']:>9.3f}{lb:>10.3f}{m['profit_factor_raw']:>7.2f}  {v}")
