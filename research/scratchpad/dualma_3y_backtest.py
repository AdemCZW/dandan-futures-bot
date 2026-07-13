"""雙均線系統（ma_convergence_pullback，目前雙均線頁面/圖表在跑的那套設定）
3年8幣嚴格回測（2026-07-13）。

使用者：「如果用目前這樣的方式去交易，可以試試去交易回測看看」——「目前這樣」
指的是雙均線頁面現在顯示、b9 觀察倉實際下單依據的那套邏輯：
  - require_density_for_breakout=True（圖表面板專用修正，見 core/chart_data.py）
  - 只吃 is_first_pullback（首踩）進場，這是 signal() 預設唯一觸發條件
  - use_htf_filter（日線共振）：b9 現行已開啟，兩種都測

2026-07-05 曾用 365 天資料測過（見 docs/strategy_research_log.md），當時樣本
偏少（+HTF 只 10 筆左右）、結論是「不顯著」。這次改用已快取的 3 年(1095天)
資料重跑，累積更多樣本看結論是否穩固。
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import pandas as pd
from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest
from backtest.tournament import _metrics_from_pnls, bootstrap_mean_lower_bound

CORE8 = ["SUIUSDT", "BTCUSDT", "ETHUSDT", "ARBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT"]
CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")


def load(symbol, interval="4h", days=1095):
    p = os.path.join(CACHE, f"{symbol}_{interval}_{days}.csv")
    return pd.read_csv(p, index_col=0, parse_dates=True)


def make_cfg(**ov):
    base = dict(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0, tp_R_mult=3.0)
    base.update(ov)
    return Config(**base)


def pooled(strat_params=None, which="full", require_density=True):
    pnls = []
    cfg = make_cfg()
    for s in CORE8:
        df = load(s)
        if df.empty:
            continue
        mid = len(df) // 2
        seg = df if which == "full" else (df.iloc[:mid] if which == "first" else df.iloc[mid:])
        strat = build_strategy("ma_convergence_pullback", require_density_for_breakout=require_density,
                               **(strat_params or {}))
        try:
            res = run_backtest(seg.copy(), strat, RiskOfficer(cfg), cfg)
        except Exception as e:
            print(f"  [warn] {s}: {e}")
            continue
        pnls.extend([t["pnl"] for t in res.trades])
    return pnls


def report(name, pnls):
    if not pnls:
        print(f"{name:<32}{'0':>6}  無交易"); return None
    m = _metrics_from_pnls(pnls)
    lb = bootstrap_mean_lower_bound(pnls)
    v = "✅ 顯著正edge" if lb > 0 else ("⚠ 正但不顯著" if m["expectancy"] > 0 else "❌ 負期望")
    print(f"{name:<32}{m['trades']:>6}{m['win_rate']:>8.1%}{m['expectancy']:>9.3f}{lb:>10.3f}{m['profit_factor_raw']:>7.2f}  {v}")
    return lb


if __name__ == "__main__":
    print("=== 雙均線系統(目前圖表/b9設定)：8幣池化、4h、真實成本、3年資料 ===\n")
    print(f"{'配置':<32}{'筆數':>6}{'勝率':>8}{'期望':>9}{'信賴下界':>10}{'PF':>7}")

    print("--- 圖表現在顯示的版本（require_density_for_breakout=True）---")
    report("圖表版(關日線共振)", pooled({"use_htf_filter": False}, require_density=True))
    report("圖表版(開日線共振)", pooled({"use_htf_filter": True}, require_density=True))

    print("\n--- b9 觀察倉實際線上跑的版本（density gate 尚未套用，見chart_data.py註解）---")
    report("b9實盤現行(關日線共振)", pooled({"use_htf_filter": False}, require_density=False))
    report("b9實盤現行(開日線共振,即現在線上設定)", pooled({"use_htf_filter": True}, require_density=False))

    print("\n=== 切半驗證（防過擬合）===")
    for label, params, dens in (("圖表版+開日線共振", {"use_htf_filter": True}, True),
                                 ("b9實盤現行設定", {"use_htf_filter": True}, False)):
        p1 = pooled(params, which="first", require_density=dens)
        p2 = pooled(params, which="second", require_density=dens)
        m1, m2 = _metrics_from_pnls(p1), _metrics_from_pnls(p2)
        lb1, lb2 = bootstrap_mean_lower_bound(p1), bootstrap_mean_lower_bound(p2)
        print(f"[{label}] 前半：{m1['trades']}筆 期望{m1['expectancy']:+.3f} 下界{lb1:+.3f}"
              f"　後半：{m2['trades']}筆 期望{m2['expectancy']:+.3f} 下界{lb2:+.3f}")

    print("\n對照（本session已驗證基準，smc_structure/4h+rr3 現行8幣籃子）：")
    print(f"{'':<32}{'':>6}{'35.5%':>8}{4.993:>9.3f}{1.665:>10.3f}")
