"""雙均線系統（ma_convergence_pullback）3 年資料嚴格驗證（2026-07-06）。

使用者要求：既有 1 年資料樣本太小（160-297筆），信賴下界一直很寬測不出顯著性。
抓 3 年 4h 資料重跑同一套嚴格方法（8幣池化、真實成本、切半驗證、bootstrap
信賴下界），看樣本量放大後結論會不會不同。部分幣種（SUI/ARB 等）上市較晚，
抓不到滿 3 年是預期中的事，腳本會如實抓到多少算多少。

對照組：smc_structure/4h+rr3 也一併驗證 3 年下是否依然顯著（現行冠軍）。
"""
import sys, os, json, time, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import pandas as pd
from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest
from backtest.tournament import _metrics_from_pnls, bootstrap_mean_lower_bound

CORE8 = ["SUIUSDT","BTCUSDT","ETHUSDT","ARBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","DOTUSDT"]
CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")
DAYS = 1095   # 3 年
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


def make_cfg(**overrides):
    base = dict(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0, tp_R_mult=3.0)
    base.update(overrides)
    return Config(**base)


def pooled(data, strategy_name, strat_params=None, which="full"):
    pnls = []
    cfg = make_cfg()
    for s, df in data.items():
        mid = len(df) // 2
        seg = df if which == "full" else (df.iloc[:mid] if which == "first" else df.iloc[mid:])
        strat = build_strategy(strategy_name, **(strat_params or {}))
        try:
            res = run_backtest(seg.copy(), strat, RiskOfficer(cfg), cfg)
            pnls.extend([t["pnl"] for t in res.trades])
        except Exception as e:
            print(f"  [warn] {s} {which}: {e}")
    return pnls


def report(name, pnls):
    if not pnls:
        print(f"{name:<40}{'0':>6}  無交易"); return None
    m = _metrics_from_pnls(pnls)
    lb = bootstrap_mean_lower_bound(pnls)
    v = "✅ 顯著正edge" if lb > 0 else ("⚠ 正但不顯著" if m["expectancy"] > 0 else "❌ 負期望")
    print(f"{name:<40}{m['trades']:>6}{m['win_rate']:>8.1%}{m['expectancy']:>9.3f}{lb:>10.3f}{m['profit_factor_raw']:>7.2f}  {v}")
    return lb


if __name__ == "__main__":
    print("=== 抓 3 年資料 ===")
    data = {}
    for s in CORE8:
        df = fetch(s)
        if not df.empty:
            span_days = (df.index[-1] - df.index[0]).days
            print(f"  {s:10s} {len(df)} 根，實際涵蓋 {span_days} 天")
            data[s] = df

    print(f"\n{'配置':<40}{'筆數':>6}{'勝率':>8}{'期望':>9}{'信賴下界':>10}{'PF':>7}")
    report("smc_structure/4h+rr3（對照冠軍）", pooled(data, "smc_structure"))
    report("雙均線 baseline（僅首踩）", pooled(data, "ma_convergence_pullback"))
    report("雙均線 +HTF共振", pooled(data, "ma_convergence_pullback", {"use_htf_filter": True}))
    report("雙均線 +二次回踩合併", pooled(data, "ma_convergence_pullback", {"use_second_pullback_entry": True}))

    print("\n=== 切半驗證（3年資料切一半＝約1.5年一段）===")
    for name, strat_params in [("雙均線 baseline", {}), ("雙均線 +HTF共振", {"use_htf_filter": True})]:
        p1 = pooled(data, "ma_convergence_pullback", strat_params, which="first")
        p2 = pooled(data, "ma_convergence_pullback", strat_params, which="second")
        m1, m2 = _metrics_from_pnls(p1), _metrics_from_pnls(p2)
        lb1, lb2 = bootstrap_mean_lower_bound(p1), bootstrap_mean_lower_bound(p2)
        print(f"{name:<25} 前半：{m1['trades']}筆 LB{lb1:+.3f}  ｜  後半：{m2['trades']}筆 LB{lb2:+.3f}")
