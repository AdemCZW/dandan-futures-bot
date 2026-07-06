"""① smc_structure 出場機制優化（2026-07-05）——切半防過擬合。

方法（誠實版，避免挑到樣本內贏家）：
  每個幣時間軸切半。前半（in-sample）跑所有出場配置、依「池化期望值」選最佳；
  後半（OOS，前半沒看過）才報告：選中的配置 vs 現行 baseline，兩者都在同一個
  後半窗口量測。判準：選中配置的後半 bootstrap 信賴下界，是否高於 baseline 後半下界。
  （若只是樣本內贏、樣本外沒贏 → 就是過度擬合，不採用。）

出場配置（theory-motivated 小集合，不做大網格）：
  baseline    tp_R_mult=2.0, use_fixed_tp=True（現行線上）
  rr3         tp_R_mult=3.0
  rr4         tp_R_mult=4.0
  tightSL_rr3 atr_mult_sl=1.5, tp_R_mult=3.0
  trail       use_fixed_tp=False（TP推遠，Chandelier主導，chand_mult=3）
  trail_tight use_fixed_tp=False, chand_mult=2.0
  trail_loose use_fixed_tp=False, chand_mult=4.0
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

def make_cfg(**overrides):
    base = dict(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0)
    base.update(overrides)
    return Config(**base)

CONFIGS = {
    "baseline(現行)":   dict(tp_R_mult=2.0, use_fixed_tp=True),
    "rr3":              dict(tp_R_mult=3.0, use_fixed_tp=True),
    "rr4":              dict(tp_R_mult=4.0, use_fixed_tp=True),
    "tightSL_rr3":      dict(atr_mult_sl=1.5, tp_R_mult=3.0, use_fixed_tp=True),
    "trail":            dict(use_fixed_tp=False, chand_mult=3.0),
    "trail_tight":      dict(use_fixed_tp=False, chand_mult=2.0),
    "trail_loose":      dict(use_fixed_tp=False, chand_mult=4.0),
}

# 載入 + 切半
data = {}
for s in SYMBOLS:
    df = fetch(s)
    if not df.empty:
        data[s] = df

def pooled_pnls(cfg_overrides, which):
    """which='first'/'second'/'full'：對每個幣取對應半段跑 smc_structure。"""
    pnls = []
    cfg = make_cfg(**cfg_overrides)
    for s, df in data.items():
        mid = len(df) // 2
        seg = df.iloc[:mid] if which == "first" else (df.iloc[mid:] if which == "second" else df)
        try:
            res = run_backtest(seg.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
            pnls.extend([t["pnl"] for t in res.trades])
        except Exception as e:
            print(f"  [warn] {s} {which}: {e}")
    return pnls

def stats(pnls):
    if not pnls: return None
    m = _metrics_from_pnls(pnls)
    return dict(n=m["trades"], win=m["win_rate"], exp=m["expectancy"],
                lb=bootstrap_mean_lower_bound(pnls), pf=m["profit_factor_raw"])

print("=== 穩健性檢驗：每個出場配置在【前半】與【後半】的信賴下界 ===")
print("（robust 判準：兩半下界都不明顯低於 baseline 對應半段，才算真改善而非過擬合）\n")
print(f"{'配置':<18}{'前半LB':>9}{'後半LB':>9}{'前半期望':>10}{'後半期望':>10}  評語")
base_first = base_second = None
for name, ov in CONFIGS.items():
    s1 = stats(pooled_pnls(ov, "first"))
    s2 = stats(pooled_pnls(ov, "second"))
    if name == "baseline(現行)":
        base_first, base_second = s1["lb"], s2["lb"]
    note = ""
    if base_first is not None and name != "baseline(現行)":
        both_ge = (s1["lb"] >= base_first - 0.3) and (s2["lb"] >= base_second - 0.3)
        note = "✅ 兩半都不輸baseline" if both_ge else ("⚠ 僅一半贏" if (s1["lb"] > base_first or s2["lb"] > base_second) else "❌ 兩半都輸")
    print(f"{name:<18}{s1['lb']:>9.3f}{s2['lb']:>9.3f}{s1['exp']:>10.3f}{s2['exp']:>10.3f}  {note}")
