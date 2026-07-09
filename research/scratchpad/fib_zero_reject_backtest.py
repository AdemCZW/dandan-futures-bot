"""零軸拒絕交易法嚴格回測（2026-07-09）。

使用者提供分析師的「自然交易理論」零軸拒絕做空法，宣稱零軸/一軸有 80-90%
反轉勝率。復刻成 fib_zero_reject 策略後，用跟這整輪研究同一套嚴格方法檢驗
那個宣稱是否為真：8幣池化、真實成本(fee+slippage+funding+fill_lag)、
bootstrap 信賴下界。下界>0 才算顯著正 edge。

也做量能閘門的 A/B（分析師的核心條件是「量能衰減」，測它到底有沒有貢獻）。
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
    if not os.path.exists(p):
        p = os.path.join(CACHE, f"{symbol}_{interval}_365.csv")
    return pd.read_csv(p, index_col=0, parse_dates=True)


def make_cfg(**ov):
    base = dict(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0)
    base.update(ov)
    return Config(**base)


def pooled(strat_params=None, cfg_ov=None, which="full"):
    pnls = []
    cfg = make_cfg(**(cfg_ov or {}))
    for s in CORE8:
        df = load(s)
        if df.empty:
            continue
        mid = len(df) // 2
        seg = df if which == "full" else (df.iloc[:mid] if which == "first" else df.iloc[mid:])
        strat = build_strategy("fib_zero_reject", **(strat_params or {}))
        res = run_backtest(seg.copy(), strat, RiskOfficer(cfg), cfg)
        pnls.extend([t["pnl"] for t in res.trades])
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
    print("=== 零軸拒絕交易法：8幣池化、4h、真實成本 ===")
    print("（分析師宣稱零軸/一軸 80-90% 反轉勝率——這裡用真實資料檢驗）\n")
    print(f"{'配置':<40}{'筆數':>6}{'勝率':>8}{'期望':>9}{'信賴下界':>10}{'PF':>7}")

    report("零軸拒絕(預設,觸即進+量能閘門)", pooled())
    report("零軸拒絕(關量能閘門)", pooled({"use_volume_gate": False}))
    print("--- 第二根四小時確認（使用者指正的核心：等第二根不突破才進場）---")
    report("零軸拒絕(第二根確認+量能)", pooled({"use_second_candle_confirm": True}))
    report("零軸拒絕(第二根確認,關量能)", pooled({"use_second_candle_confirm": True, "use_volume_gate": False}))
    report("零軸拒絕(第二根+rr3)", pooled({"use_second_candle_confirm": True}, {"tp_R_mult": 3.0}))
    report("零軸拒絕(第二根+短窗40)", pooled({"use_second_candle_confirm": True, "lookback": 40}))

    print("\n對照（本session已驗證基準）：")
    print(f"{'smc_structure/4h+rr3(現行8幣籃子)':<40}{'':>6}{'35.5%':>8}{4.993:>9.3f}{1.665:>10.3f}")

    print("\n=== 切半驗證（防過擬合，測預設版）===")
    p1 = pooled(which="first"); p2 = pooled(which="second")
    m1, m2 = _metrics_from_pnls(p1), _metrics_from_pnls(p2)
    lb1, lb2 = bootstrap_mean_lower_bound(p1), bootstrap_mean_lower_bound(p2)
    print(f"前半：{m1['trades']}筆 期望{m1['expectancy']:+.3f} 下界{lb1:+.3f}")
    print(f"後半：{m2['trades']}筆 期望{m2['expectancy']:+.3f} 下界{lb2:+.3f}")
