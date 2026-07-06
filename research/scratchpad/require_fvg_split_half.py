"""smc_structure require_fvg 開關切半驗證（2026-07-06）。

在已驗證有效的策略內部微調（不是新策略），測試 require_fvg=True（BOS 突破
需同時伴隨 Fair Value Gap 才進場）是否比現行 require_fvg=False（預設）更好。
同一套切半防過擬合方法：前半/後半各自算 bootstrap 信賴下界，兩半都不輸
baseline 才算穩健改善。出場固定用現行已部署的 rr3（tp_R_mult=3.0）。
"""
import sys, os, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import pandas as pd
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
    end_ms = int(time.time() * 1000)
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
    # 現行線上實際設定：rr3(tp_R_mult=3.0) 已於 2026-07-05 部署到 8 幣籃子
    return Config(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                  fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                  funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0,
                  tp_R_mult=3.0)


def pooled_pnls(data, require_fvg, which):
    pnls = []
    cfg = make_cfg()
    for s, df in data.items():
        mid = len(df) // 2
        seg = df.iloc[:mid] if which == "first" else (df.iloc[mid:] if which == "second" else df)
        strat = build_strategy("smc_structure", require_fvg=require_fvg)
        try:
            res = run_backtest(seg.copy(), strat, RiskOfficer(cfg), cfg)
            pnls.extend([t["pnl"] for t in res.trades])
        except Exception as e:
            print(f"  [warn] {s} {which}: {e}")
    return pnls


def stats(pnls):
    if not pnls: return None
    m = _metrics_from_pnls(pnls)
    return dict(n=m["trades"], win=m["win_rate"], exp=m["expectancy"],
                lb=bootstrap_mean_lower_bound(pnls), pf=m["profit_factor_raw"])


if __name__ == "__main__":
    data = {}
    for s in SYMBOLS:
        df = fetch(s)
        if not df.empty:
            data[s] = df

    print("=== require_fvg 切半驗證（現行 rr3 出場，8幣池化，真實成本）===\n")
    print(f"{'配置':<20}{'前半LB':>9}{'後半LB':>9}{'前半期望':>10}{'後半期望':>10}{'前半筆':>7}{'後半筆':>7}")

    base_first_s = stats(pooled_pnls(data, False, "first"))
    base_second_s = stats(pooled_pnls(data, False, "second"))
    fvg_first_s = stats(pooled_pnls(data, True, "first"))
    fvg_second_s = stats(pooled_pnls(data, True, "second"))

    def row(name, s1, s2):
        print(f"{name:<20}{s1['lb']:>9.3f}{s2['lb']:>9.3f}{s1['exp']:>10.3f}{s2['exp']:>10.3f}{s1['n']:>7}{s2['n']:>7}")

    row("baseline(現行)", base_first_s, base_second_s)
    row("require_fvg=True", fvg_first_s, fvg_second_s)

    both_ge = (fvg_first_s['lb'] >= base_first_s['lb']) and (fvg_second_s['lb'] >= base_second_s['lb'])
    note = "✅ 兩半都不輸baseline" if both_ge else (
        "⚠ 僅一半贏" if (fvg_first_s['lb'] > base_first_s['lb'] or fvg_second_s['lb'] > base_second_s['lb'])
        else "❌ 兩半都輸")
    print(f"\n判決：{note}")

    print("\n=== 全期間對照（不切半）===")
    full_base = stats(pooled_pnls(data, False, "full"))
    full_fvg = stats(pooled_pnls(data, True, "full"))
    print(f"baseline(現行)      筆數{full_base['n']:>5}  勝率{full_base['win']:>7.1%}  期望{full_base['exp']:>8.3f}  下界{full_base['lb']:>8.3f}")
    print(f"require_fvg=True    筆數{full_fvg['n']:>5}  勝率{full_fvg['win']:>7.1%}  期望{full_fvg['exp']:>8.3f}  下界{full_fvg['lb']:>8.3f}")
