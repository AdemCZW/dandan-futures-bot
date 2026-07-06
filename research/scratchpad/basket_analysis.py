"""②③ 籃子擴充 + 逐幣貢獻度（2026-07-05）。

② 擴大籃子：現有8幣 + 候選幣，看池化信賴下界是否進一步墊高。
③ 逐幣貢獻：每幣單獨 OOS 表現 + leave-one-out（拿掉哪個幣讓整體下界最高）。
全部用 smc_structure/4h、真實成本、rr3 出場（tp_R_mult=3.0，因①已驗證優於2.0）。
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
# 候選擴充幣（主流、流動性佳的合約）
CANDIDATES = ["LINKUSDT","AVAXUSDT","NEARUSDT","OPUSDT","INJUSDT","APTUSDT","LTCUSDT"]
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
                  funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0,
                  tp_R_mult=3.0)   # ①驗證後採用 rr3

def coin_pnls(symbol):
    df = fetch(symbol)
    if df.empty: return None
    cfg = make_cfg()
    res = run_backtest(df.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
    return [t["pnl"] for t in res.trades]

def lb(pnls):
    return bootstrap_mean_lower_bound(pnls) if pnls else float("-inf")

# 預抓所有幣
print("=== 抓資料 ===")
per_coin = {}
for s in CORE8 + CANDIDATES:
    p = coin_pnls(s)
    if p is not None:
        per_coin[s] = p
        print(f"  {s:10s} {len(p)} 筆")

# ③ 逐幣單獨表現
print("\n=== ③ 逐幣單獨 OOS 表現（smc rr3）===")
print(f"{'幣種':<10}{'筆數':>6}{'勝率':>8}{'期望':>9}{'信賴下界':>10}")
for s in CORE8:
    p = per_coin[s]
    m = _metrics_from_pnls(p)
    print(f"{s:<10}{m['trades']:>6}{m['win_rate']:>7.1%}{m['expectancy']:>9.3f}{lb(p):>10.3f}")

# ③ leave-one-out：拿掉哪個幣，整體下界最高
pooled8 = [x for s in CORE8 for x in per_coin[s]]
base_lb = lb(pooled8)
print(f"\n現有8幣池化：{len(pooled8)}筆，信賴下界 {base_lb:+.3f}")
print("Leave-one-out（拿掉該幣後的池化下界；↑表示該幣在拖累）：")
loo = []
for s in CORE8:
    rest = [x for o in CORE8 if o != s for x in per_coin[o]]
    l = lb(rest)
    loo.append((s, l))
for s, l in sorted(loo, key=lambda x: -x[1]):
    delta = l - base_lb
    tag = "← 拿掉後改善最多（拖累者）" if delta > 0 else ""
    print(f"  去掉 {s:10s} → 下界 {l:+.3f}（Δ{delta:+.3f}）{tag}")

# ② 擴大籃子
print(f"\n=== ② 擴大籃子（逐步加候選幣，看池化下界）===")
print(f"{'籃子':<40}{'幣數':>5}{'筆數':>7}{'信賴下界':>10}")
print(f"{'現有8幣':<40}{8:>5}{len(pooled8):>7}{base_lb:>10.3f}")
cur = list(CORE8)
cur_pnls = list(pooled8)
for c in CANDIDATES:
    if c not in per_coin: continue
    cur.append(c)
    cur_pnls = cur_pnls + per_coin[c]
    print(f"{'+ ' + c:<40}{len(cur):>5}{len(cur_pnls):>7}{lb(cur_pnls):>10.3f}")
