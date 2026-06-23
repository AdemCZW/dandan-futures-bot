"""獨立測試：驗證 run_backtest 做空引擎正確性。

不依賴 pytest，直接執行；逐項 assert 並印出實際值。
"""
from dataclasses import replace

import pandas as pd

from backtest.backtester import run_backtest
from core.quant_researcher import Strategy
from core.risk_officer import RiskOfficer
from config import Config


class AlwaysShortStrategy(Strategy):
    """測試假策略：永遠回 -1（想做空）。allow_short 由建構參數控制。"""
    name = "always_short"

    def __init__(self, allow_short: bool = True, **params):
        super().__init__(**params)
        self.allow_short = allow_short

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def signal(self, row: pd.Series, position: int) -> int:
        return -1


def make_df(closes):
    """以確定性 high=close*1.001、low=close*0.999 構造 K 線。"""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="5min")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [1.0] * len(closes),
        },
        index=idx,
    )


def base_cfg(**over):
    cfg = Config(
        start_equity=10000.0,
        fee_rate=0.001,
        risk_per_trade=0.5,
        max_position_pct=0.9,
        stop_loss_pct=0.5,
        take_profit_pct=0.5,
        max_daily_loss_pct=1e9,  # 關閉單日熔斷，避免干擾確定性序列
    )
    return replace(cfg, **over) if over else cfg


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


results = []


def check(name, cond, detail):
    results.append((name, cond, detail))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}: {detail}")


# ── (a) 下跌：做空應獲利 ──────────────────────────────
cfg = base_cfg()
risk = RiskOfficer(cfg)
strat = AlwaysShortStrategy(allow_short=True)
df = make_df([100, 99, 98, 97, 96, 95, 94, 93, 92, 91])
res = run_backtest(df, strat, risk, cfg)

trades = res.trades  # 只含已平倉
n_closed = len(trades)
check("a.trade_count", n_closed == 1, f"closed trades = {n_closed} (期望 1)")

if n_closed == 1:
    t = trades[0]
    check("a.dir_short", t["dir"] == -1, f"dir = {t['dir']} (期望 -1，空單)")
    check("a.qty", approx(t["qty"], 90.0), f"qty = {t['qty']} (期望 90)")
    check("a.exit_final", t["side"] == "exit_final",
          f"side = {t['side']} (期望 exit_final)")
    exp_pnl = 90 * 100 * 0.999 - 90 * 91 * 1.001  # = 792.81
    check("a.pnl", approx(t["pnl"], exp_pnl, tol=1e-9),
          f"pnl = {t['pnl']:.6f} (期望 {exp_pnl:.6f})")

last_eq = float(res.equity_curve.iloc[-1])
# H1 修復後：收尾平倉以「已實現現金」修正權益曲線末點 = 起始 + 已實現 pnl
exp_last = 10000.0 + (90 * 100 * 0.999 - 90 * 91 * 1.001)   # = 10792.81
check("a.equity_last", approx(last_eq, exp_last, tol=1e-9),
      f"末點權益(已實現) = {last_eq:.6f} (期望 {exp_last:.6f})")
check("a.profitable", last_eq > 10000,
      f"末點權益 {last_eq:.2f} > 10000 (做空下跌應獲利)")


# ── (b) 上漲：做空應虧損 ──────────────────────────────
cfg = base_cfg()
risk = RiskOfficer(cfg)
strat = AlwaysShortStrategy(allow_short=True)
df = make_df([100, 101, 102, 103, 104, 105, 106, 107, 108, 109])
res = run_backtest(df, strat, risk, cfg)

check("b.trade_count", len(res.trades) == 1,
      f"closed trades = {len(res.trades)} (期望 1)")
if res.trades:
    pnl_b = res.trades[0]["pnl"]
    check("b.pnl_negative", pnl_b < 0, f"pnl = {pnl_b:.6f} (期望 < 0)")
last_eq_b = float(res.equity_curve.iloc[-1])
check("b.equity_loss", last_eq_b < 10000,
      f"末點權益 = {last_eq_b:.6f} (期望 < 10000)")


# ── (c) allow_short=False：應全程空手、零交易、權益恆=10000 ──
cfg = base_cfg()
risk = RiskOfficer(cfg)
strat = AlwaysShortStrategy(allow_short=False)
df = make_df([100, 99, 98, 97, 96, 95, 94, 93, 92, 91])
res = run_backtest(df, strat, risk, cfg)

check("c.zero_trades", len(res.trades) == 0,
      f"closed trades = {len(res.trades)} (期望 0)")
eq_const = res.equity_curve
all_flat = bool((eq_const == 10000.0).all())
check("c.equity_flat", all_flat,
      f"權益曲線全等於 10000 = {all_flat} (min={eq_const.min()}, max={eq_const.max()})")


# ── (d) 方向性停損：空單 high 越過停損價 → exit_sltp ──────
cfg = base_cfg(stop_loss_pct=0.03)
risk = RiskOfficer(cfg)
strat = AlwaysShortStrategy(allow_short=True)
df = make_df([100, 101, 104, 103, 102])
res = run_backtest(df, strat, risk, cfg)

# 進場價 100 → 空單停損價 = 100*(1+0.03) = 103
# 第三根 high = 104*1.001 = 104.104 >= 103 → 觸發停損
sides = [t["side"] for t in res.trades]
check("d.has_exit_sltp", "exit_sltp" in sides,
      f"sides = {sides} (期望含 exit_sltp)")
if "exit_sltp" in sides:
    t = next(t for t in res.trades if t["side"] == "exit_sltp")
    check("d.sltp_price", approx(t["price"], 103.0),
          f"停損成交價 = {t['price']} (期望 103)")
    check("d.sltp_dir", t["dir"] == -1,
          f"dir = {t['dir']} (期望 -1，空單)")


# ── 總結 ──────────────────────────────────────────────
passed = sum(1 for _, c, _ in results if c)
total = len(results)
print(f"\n==== {passed}/{total} assertions passed ====")
if passed != total:
    raise SystemExit(1)
print("ALL PASS")
