"""古典圖表形態 + 迴歸通道 兩個新策略嚴格回測（2026-07-06）。

使用者要求測試 TradingView 兩個編輯精選腳本背後的核心概念：
  - Chart Patterns Screener（古典三角形/楔形收斂突破）→ chart_pattern_breakout
  - Polynomial/Linear Regression Volume Profile（迴歸配適通道）→ regression_channel

兩個策略跟現有八個策略訊號來源完全不同（真實樞紐趨勢線 vs 統計OLS迴歸，
而非六線價差/BOS結構），策略程式碼見 core/quant_researcher.py（TDD，
tests/test_chart_pattern_breakout.py + tests/test_regression_channel.py）。

同一套嚴格方法：8幣池化、真實成本、bootstrap 信賴下界。
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
                funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0)
    base.update(overrides)
    return Config(**base)


def pooled(strategy_name, strat_params=None, cfg_overrides=None):
    pnls = []
    cfg = make_cfg(**(cfg_overrides or {}))
    for s in CORE8:
        df = fetch(s)
        if df.empty: continue
        strat = build_strategy(strategy_name, **(strat_params or {}))
        res = run_backtest(df.copy(), strat, RiskOfficer(cfg), cfg)
        pnls.extend([t["pnl"] for t in res.trades])
    return pnls


def report(name, pnls):
    if not pnls:
        print(f"{name:<45}{'0':>6}  無交易"); return
    m = _metrics_from_pnls(pnls)
    lb = bootstrap_mean_lower_bound(pnls)
    v = "✅ 顯著正edge" if lb > 0 else ("⚠ 正但不顯著" if m["expectancy"] > 0 else "❌ 負期望")
    print(f"{name:<45}{m['trades']:>6}{m['win_rate']:>8.1%}{m['expectancy']:>9.3f}{lb:>10.3f}{m['profit_factor_raw']:>7.2f}  {v}")


if __name__ == "__main__":
    print(f"{'配置':<45}{'筆數':>6}{'勝率':>8}{'期望':>9}{'信賴下界':>10}{'PF':>7}")
    report("chart_pattern_breakout(預設)", pooled("chart_pattern_breakout"))
    report("chart_pattern_breakout(rr3出場)", pooled("chart_pattern_breakout", cfg_overrides={"tp_R_mult": 3.0}))
    report("chart_pattern(嚴格收斂0.5+寬樞紐8/8)",
           pooled("chart_pattern_breakout", {"pivot_left": 8, "pivot_right": 8, "convergence_ratio": 0.5}))
    report("chart_pattern(收斂0.6+樞紐6/6)",
           pooled("chart_pattern_breakout", {"pivot_left": 6, "pivot_right": 6, "convergence_ratio": 0.6}))
    report("regression_channel(預設 w=100)", pooled("regression_channel"))
    report("regression_channel(w=50)", pooled("regression_channel", {"window": 50}))
    report("regression_channel(band=2.5)", pooled("regression_channel", {"window": 100, "band_mult": 2.5}))
    report("regression_channel(band=3.0)", pooled("regression_channel", {"window": 100, "band_mult": 3.0}))
    report("regression_channel(w=200,band=2.5)", pooled("regression_channel", {"window": 200, "band_mult": 2.5}))
    print()
    print("對照組（本session已驗證的基準）：")
    print(f"{'smc_structure/4h+rr3(現行8幣籃子)':<45}{448:>6}{'35.5%':>8}{4.993:>9.3f}{1.665:>10.3f}")
