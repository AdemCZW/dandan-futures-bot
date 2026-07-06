"""多週期共振消融實測（2026-07-05）：日線趨勢過濾對兩個策略的 OOS 影響。

對照組（同一批8幣4h資料、同一套真實成本）：
  smc_structure 原版（線上籃子現行） vs +htf 過濾
  ma_convergence_pullback 原版（b9現行） vs +htf 過濾
判準：池化期望值 + bootstrap 信賴下界。特別注意 smc 原版 LB 是顯著正的，
htf 版必須「下界更高」才值得動已驗證的籃子。
"""
import sys, os
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

def load(symbol):
    p = os.path.join(CACHE, f"{symbol}_4h_{DAYS}.csv")
    return pd.read_csv(p, index_col=0, parse_dates=True) if os.path.exists(p) else pd.DataFrame()

def make_cfg():
    return Config(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                  fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                  funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0)

def pooled(strategy_name, **params):
    pnls = []
    cfg = make_cfg()
    for s in SYMBOLS:
        df = load(s)
        if df.empty: continue
        try:
            res = run_backtest(df.copy(), build_strategy(strategy_name, **params),
                               RiskOfficer(cfg), cfg)
            pnls.extend([t["pnl"] for t in res.trades])
        except Exception as e:
            print(f"  [warn] {s}: {e}")
    return pnls

def report(label, pnls):
    if not pnls:
        print(f"{label:<40} 無交易"); return
    m = _metrics_from_pnls(pnls)
    lb = bootstrap_mean_lower_bound(pnls)
    v = "✅ 顯著正" if lb > 0 else ("⚠ 正但不顯著" if m["expectancy"] > 0 else "❌ 負期望")
    print(f"{label:<40}{m['trades']:>6}{m['win_rate']:>8.1%}{m['expectancy']:>9.3f}{lb:>10.3f}{m['profit_factor_raw']:>7.2f}  {v}")

print(f"{'配置':<40}{'筆數':>6}{'勝率':>8}{'期望':>9}{'信賴下界':>10}{'PF':>7}  判決\n")
report("smc_structure 原版（線上籃子）",       pooled("smc_structure"))
report("smc_structure + 日線共振",             pooled("smc_structure", use_htf_filter=True))
print()
report("ma_convergence_pullback 原版（b9）",   pooled("ma_convergence_pullback"))
report("ma_convergence_pullback + 日線共振",   pooled("ma_convergence_pullback", use_htf_filter=True))
