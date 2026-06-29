"""backtest/backtester.run_backtest 的 pytest 測試。

只新增測試、不改任何 source。用確定性 K 線（high=close*1.001 / low=close*0.999）
與假策略子類（吃注入的 "sig" 欄位回傳目標倉位），把 max_daily_loss_pct 開大
關閉單日熔斷，逐項驗證引擎「正確/應有」行為：

  (a) 平盤手續費對稱：多/空各一筆平盤 round-trip，pnl 皆為負且量值相等。
  (b) H1 收尾：尾段仍持倉時 equity_curve 末點 = 已實現現金（start+pnl）。
  (c) win_rate 誠實：漲幅落在 (出場費, 來回費) 之間時 per-trade pnl<0、被記為虧。
  (d) 停損：當根 low<=sl 觸發 exit_sltp、成交價=sl。
  (e) 因果/無前視：df 後接「未來」K 線重跑，前段 equity 前綴不變。
"""
import dataclasses

import numpy as np
import pandas as pd
import pytest

from backtest.backtester import run_backtest, BacktestResult, interval_to_minutes, bars_per_year
from config import Config
from core.quant_researcher import Strategy
from core.risk_officer import RiskOfficer


# ── OPT-09：Sharpe 年化須隨 interval、與樣本長度無關 ───────────────────
def test_interval_to_minutes_parses_binance_intervals():
    assert interval_to_minutes("1m") == 1
    assert interval_to_minutes("15m") == 15
    assert interval_to_minutes("1h") == 60
    assert interval_to_minutes("4h") == 240
    assert interval_to_minutes("1d") == 1440
    assert interval_to_minutes("garbage") == 60.0          # 未知 → 退回 1h，不拋例外


def test_bars_per_year_scales_with_interval():
    assert bars_per_year("1h") == pytest.approx(365 * 24)
    assert bars_per_year("15m") == pytest.approx(365 * 24 * 4)
    assert bars_per_year("1d") == pytest.approx(365)


def test_sharpe_annualized_by_interval_not_sample_length():
    """同一段報酬 pattern 重複 500 vs 2000 次：每根 mean/std 相同 → 年化 Sharpe 應相同。

    舊版乘 sqrt(len)：500*4 vs 2000*4 根會差 sqrt(4)=2 倍（純樣本長度偽影）；
    修正後乘 sqrt(bars_per_year)（與長度無關）→ 兩者近乎相等。
    """
    block = [0.01, -0.005, 0.008, -0.003]
    eq_short = (1 + pd.Series(block * 500)).cumprod()    # 2000 根
    eq_long = (1 + pd.Series(block * 2000)).cumprod()    # 8000 根
    s_short = BacktestResult(equity_curve=eq_short, trades=[], interval="1h").sharpe
    s_long = BacktestResult(equity_curve=eq_long, trades=[], interval="1h").sharpe
    assert abs(s_short - s_long) / max(abs(s_long), 1e-9) < 0.01


def test_sharpe_differs_across_intervals_for_same_returns():
    """同一條報酬序列，15m 標成 1d：年化基準差 96 倍 → Sharpe 差 sqrt(96)。"""
    rng = np.random.default_rng(1)
    rets = rng.normal(0.0003, 0.008, 3000)
    eq = (1 + pd.Series(rets)).cumprod()
    s_15m = BacktestResult(equity_curve=eq, trades=[], interval="15m").sharpe
    s_1d = BacktestResult(equity_curve=eq, trades=[], interval="1d").sharpe
    ratio = bars_per_year("15m") / bars_per_year("1d")     # = 96
    assert s_15m / s_1d == pytest.approx(np.sqrt(ratio), rel=1e-6)


# ── 工具：用 close 序列造確定性 OHLCV ──────────────────────────────
def make_df(closes, start="2021-01-01 00:00:00", freq="5min"):
    """high=close*1.001、low=close*0.999 的確定性 K 線。

    時間戳同一天（避免跨日重置單日熔斷基準干擾），volume 固定。
    """
    idx = pd.date_range(start=start, periods=len(closes), freq=freq)
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": np.full(len(closes), 1000.0),
        },
        index=idx,
    )


def make_cfg(**overrides):
    """關閉熔斷（max_daily_loss_pct 開大）的 Config；其餘可覆寫。"""
    base = dict(max_daily_loss_pct=10.0, slippage=0.0)
    base.update(overrides)
    return Config(**base)


# ── 假策略：吃注入的整數 "sig" 欄位當「目標倉位」 ──────────────────
class SigStrategy(Strategy):
    """signal() 直接回傳該根 K 線的 'sig' 欄位（+1/0/-1），完全可控。

    allow_short 由建構參數決定，方便分別測多/空路徑。
    """

    name = "sig"

    def __init__(self, allow_short=False, **params):
        super().__init__(**params)
        self.allow_short = allow_short

    def signal(self, row, position):
        return int(row["sig"])


class AlwaysLong(Strategy):
    name = "always_long"
    allow_short = False

    def signal(self, row, position):
        return 1


class AlwaysShort(Strategy):
    name = "always_short"
    allow_short = True

    def signal(self, row, position):
        return -1


# ── 共用建構：一筆乾淨 round-trip 的 sig 欄位 ─────────────────────
def df_with_sig(closes, sigs, **kw):
    df = make_df(closes, **kw)
    df["sig"] = sigs
    return df


# ====================================================================
# (a) 平盤手續費對稱：多/空各一筆平盤 round-trip
# ====================================================================
def test_flat_roundtrip_fee_symmetry_long_short():
    cfg = make_cfg()
    risk_long = RiskOfficer(cfg)
    risk_short = RiskOfficer(cfg)

    # 三根都是同價（平盤）：第1根進場、第2根持有、第3根平倉
    closes = [100.0, 100.0, 100.0]

    # 多單：第1根 target=+1 進多，第3根 target=0 平倉
    df_long = df_with_sig(closes, [1, 1, 0])
    res_long = run_backtest(df_long, SigStrategy(allow_short=False), risk_long, cfg)

    # 空單：第1根 target=-1 進空，第3根 target=0 回補
    df_short = df_with_sig(closes, [-1, -1, 0])
    res_short = run_backtest(df_short, SigStrategy(allow_short=True), risk_short, cfg)

    # 各只應有一筆平倉交易
    assert len(res_long.trades) == 1
    assert len(res_short.trades) == 1

    pnl_long = res_long.trades[0]["pnl"]
    pnl_short = res_short.trades[0]["pnl"]

    # 平盤仍需付雙邊手續費 → pnl 必為負
    assert pnl_long < 0
    assert pnl_short < 0

    # M1 修正後多空對稱：量值相等
    assert pnl_long == pytest.approx(pnl_short, rel=1e-12, abs=1e-9)

    # 與手算對照：qty=equity*max_position_pct/price，pnl = qty*P*[(1-fee)-(1+fee)]
    qty = cfg.start_equity * cfg.max_position_pct / 100.0
    expected = qty * 100.0 * ((1 - cfg.fee_rate) - (1 + cfg.fee_rate))
    assert pnl_long == pytest.approx(expected, rel=1e-12, abs=1e-9)


def test_alwayslong_alwaysshort_symmetry():
    """用 AlwaysLong / AlwaysShort 在平盤序列上跑，最後一根強制收尾平倉，

    兩者尾端已實現現金（= start + 單筆來回費）應對稱相等。
    """
    cfg = make_cfg()
    closes = [100.0, 100.0, 100.0]

    res_long = run_backtest(make_df(closes), AlwaysLong(), RiskOfficer(cfg), cfg)
    res_short = run_backtest(make_df(closes), AlwaysShort(), RiskOfficer(cfg), cfg)

    # 收尾平倉各記一筆
    assert len(res_long.trades) == 1
    assert len(res_short.trades) == 1
    assert res_long.trades[0]["pnl"] < 0
    assert res_short.trades[0]["pnl"] < 0
    assert res_long.trades[0]["pnl"] == pytest.approx(
        res_short.trades[0]["pnl"], rel=1e-12, abs=1e-9
    )
    # 尾端權益（已實現現金）兩者相等
    assert res_long.equity_curve.iloc[-1] == pytest.approx(
        res_short.equity_curve.iloc[-1], rel=1e-12, abs=1e-9
    )


# ====================================================================
# (b) H1：收尾仍持倉 → equity_curve 末點 = 已實現現金（start + pnl）
# ====================================================================
def test_h1_final_equity_is_realized_cash_not_marktomarket():
    cfg = make_cfg()
    risk = RiskOfficer(cfg)

    # 一路只做多、從不送平倉信號 → 迴圈結束仍持倉，引擎於收尾平倉並修正末點
    closes = [100.0, 101.0, 102.0, 103.0]
    df = df_with_sig(closes, [1, 1, 1, 1])
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    # 收尾應有一筆 exit_final
    assert len(res.trades) == 1
    final_trade = res.trades[0]
    realized_pnl = final_trade["pnl"]

    last_equity = res.equity_curve.iloc[-1]

    # 末點 = 起始資金 + 已實現 pnl（已扣出場手續費），而非未扣費的浮動市值
    assert last_equity == pytest.approx(
        cfg.start_equity + realized_pnl, rel=1e-12, abs=1e-9
    )

    # 對照手算市值（未扣出場費）以證明兩者不同：
    entry_price = 100.0
    qty = cfg.start_equity * cfg.max_position_pct / entry_price
    cost = qty * entry_price * (1 + cfg.fee_rate)
    cash_after_entry = cfg.start_equity - cost
    last_close = closes[-1]
    mark_to_market = cash_after_entry + qty * last_close  # 未扣出場手續費的浮動值
    # 已實現末點必須嚴格小於「未扣出場費的市值」（被扣了出場手續費）
    assert last_equity < mark_to_market
    # 差額正好是出場手續費 qty*last_close*fee
    assert mark_to_market - last_equity == pytest.approx(
        qty * last_close * cfg.fee_rate, rel=1e-9, abs=1e-9
    )


# ====================================================================
# (c) win_rate 誠實：漲幅落在 (出場費, 來回費) 之間 → per-trade pnl<0、記為虧
# ====================================================================
def test_win_rate_honest_small_gain_counts_as_loss():
    cfg = make_cfg()
    risk = RiskOfficer(cfg)

    fee = cfg.fee_rate
    exit_fee = fee                       # 單邊出場費
    round_trip = 2 * fee / (1 - fee)     # 來回費（多單虧損臨界）
    g = (exit_fee + round_trip) / 2.0    # 介於兩者之間
    assert exit_fee < g < round_trip     # 確認構造落在區間內

    entry_price = 100.0
    exit_price = entry_price * (1 + g)

    # 第1根進多、第2根（小漲）送平倉信號
    df = df_with_sig([entry_price, exit_price], [1, 0])
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    assert len(res.trades) == 1
    trade = res.trades[0]

    # 雖然價格上漲，但漲幅不足以覆蓋來回手續費 → per-trade pnl < 0
    assert trade["pnl"] < 0
    # win_rate 必須誠實地把它算成 0 勝（不四捨五入為賺）
    assert res.win_rate == 0.0

    # 手算對照
    qty = cfg.start_equity * cfg.max_position_pct / entry_price
    expected = qty * exit_price * (1 - fee) - qty * entry_price * (1 + fee)
    assert trade["pnl"] == pytest.approx(expected, rel=1e-12, abs=1e-9)


# ====================================================================
# (d) 停損：當根 low<=sl 觸發 exit_sltp、成交價=sl
# ====================================================================
def test_stop_loss_triggers_exit_at_sl_price():
    cfg = make_cfg()
    risk = RiskOfficer(cfg)

    entry_price = 100.0
    sl_price = entry_price * (1 - cfg.stop_loss_pct)  # 98.0

    # 第3根做一個大跌：close 設到讓 low(=close*0.999) <= sl 的水準。
    # close=95 → low=94.905 <= 98 → 觸發停損。tp=104 不會先被打到（high<104）。
    crash_close = 95.0
    closes = [entry_price, entry_price, crash_close]
    # 第1根進多；停損的判斷在策略信號之前（引擎 step1 先於 step2），所以崩跌根
    # 會先被停損平倉。崩跌根 sig=0（不再開新倉），以隔離出單一筆 exit_sltp。
    df = df_with_sig(closes, [1, 1, 0])
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    # 確認大跌根的 low 真的觸發停損
    assert crash_close * 0.999 <= sl_price

    # 應只有一筆平倉、且 side 為 exit_sltp
    assert len(res.trades) == 1
    trade = res.trades[0]
    assert trade["side"] == "exit_sltp"
    # 成交價必須正好是停損價（不是當根 close、也不是 low）
    assert trade["price"] == pytest.approx(sl_price, rel=1e-12, abs=1e-9)
    # 停損出場必為虧損
    assert trade["pnl"] < 0


# ====================================================================
# (d2) ATR 動態停損：df 帶 atr 欄時，進場 sl/tp 用 ATR 距離（非固定百分比）
# ====================================================================
def test_atr_stops_used_when_atr_column_present():
    cfg = make_cfg()                       # atr_mult_sl=2.0, tp_R_mult=2.0
    risk = RiskOfficer(cfg)

    closes = [100.0, 100.0, 100.0]
    df = df_with_sig(closes, [1, 1, 0])
    df["atr"] = 3.0                        # 固定 ATR=3 → 與固定 2% 明顯不同
    trace = []
    run_backtest(df, SigStrategy(allow_short=False), risk, cfg, trace=trace)

    entries = [a for s in trace for a in s["actions"] if a["act"] == "entry"]
    assert len(entries) == 1
    e = entries[0]
    fill = e["price"]                      # 100.0（無滑點）
    # ATR 停損：sl = fill - 2*3 = 94（固定 % 會是 98）；tp = fill + tp_R*(mult*atr) = 100+2*6 = 112
    assert e["sl"] == pytest.approx(fill - cfg.atr_mult_sl * 3.0)   # 94
    assert e["tp"] == pytest.approx(fill + cfg.tp_R_mult * cfg.atr_mult_sl * 3.0)  # 112
    # 波動度歸一化倉位：qty = (equity*risk_per_trade)/(mult*atr) = 100/6 ≈ 16.67（未觸 cap 30）
    assert e["qty"] == pytest.approx(cfg.start_equity * cfg.risk_per_trade / (cfg.atr_mult_sl * 3.0), abs=1e-5)


def test_atr_absent_keeps_fixed_pct_stops():
    """無 atr 欄的策略（如 SigStrategy）維持固定百分比停損，向後相容。"""
    cfg = make_cfg()
    risk = RiskOfficer(cfg)
    df = df_with_sig([100.0, 100.0, 100.0], [1, 1, 0])   # 無 atr 欄
    trace = []
    run_backtest(df, SigStrategy(allow_short=False), risk, cfg, trace=trace)
    e = [a for s in trace for a in s["actions"] if a["act"] == "entry"][0]
    fill = e["price"]
    assert e["sl"] == pytest.approx(fill * (1 - cfg.stop_loss_pct))   # 98（固定 %）
    assert e["tp"] == pytest.approx(fill * (1 + cfg.take_profit_pct))  # 104


# ====================================================================
# (d3) Chandelier 追蹤停損：上漲後回落，於 highest-mult*atr 平倉並鎖利
# ====================================================================
def test_chandelier_trailing_locks_profit_on_pullback():
    # tp_R_mult 開大關掉固定停利，隔離出追蹤停損行為
    cfg = make_cfg(tp_R_mult=100.0)        # atr_mult_sl=2.0, chand_mult=3.0
    risk = RiskOfficer(cfg)

    closes = [100.0, 105.0, 110.0, 115.0, 120.0, 113.0]
    df = df_with_sig(closes, [1, 1, 1, 1, 1, 1])   # 全程想做多，只讓停損出場
    df["atr"] = 2.0
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    # 應只有一筆追蹤停損出場
    exits = [t for t in res.trades if t["side"] == "exit_sltp"]
    assert len(exits) == 1
    # 進場後最高 high = 120*1.001=120.12；trailing = 120.12 - chand_mult*atr = 120.12 - 6 = 114.12
    highest = 120.0 * 1.001
    expected_stop = highest - cfg.chand_mult * 2.0
    assert exits[0]["price"] == pytest.approx(expected_stop, rel=1e-9)
    # 鎖利：出場價遠高於進場 100，且明顯高於初始 ATR 停損(100-2*2=96)
    assert exits[0]["price"] > 100.0
    assert exits[0]["pnl"] > 0


# ====================================================================
# (e) 因果/無前視：df 後接「未來」K 線重跑，前段 equity 前綴不變
# ====================================================================
def test_no_lookahead_equity_prefix_invariant():
    cfg = make_cfg()

    base_closes = [100.0, 101.0, 100.5, 102.0, 99.0, 101.5]
    base_sigs = [1, 0, 1, 0, 1, 0]
    df_base = df_with_sig(base_closes, base_sigs)
    res_base = run_backtest(df_base, SigStrategy(allow_short=False), RiskOfficer(cfg), cfg)

    # 後面再接幾根「未來」K 線（時間戳延續、含各自的 sig）
    future_closes = [103.0, 98.0, 100.0]
    future_sigs = [1, 0, 1]
    ext_closes = base_closes + future_closes
    ext_sigs = base_sigs + future_sigs
    df_ext = df_with_sig(ext_closes, ext_sigs)
    res_ext = run_backtest(df_ext, SigStrategy(allow_short=False), RiskOfficer(cfg), cfg)

    # 前段（原始長度）的 equity_curve 前綴必須逐點一致（無前視：未來不影響過去）
    n = len(res_base.equity_curve)
    prefix_base = res_base.equity_curve.iloc[:n]
    prefix_ext = res_ext.equity_curve.iloc[:n]

    # 索引（時間戳）一致
    assert list(prefix_base.index) == list(prefix_ext.index)
    # 數值前綴逐點相等
    np.testing.assert_allclose(prefix_base.values, prefix_ext.values, rtol=1e-12, atol=1e-9)


# ====================================================================
# (f) expectancy / profit_factor 績效指標（錦標賽排序用）
# ====================================================================
def test_expectancy_is_mean_pnl_per_closed_trade():
    """expectancy = 每筆平倉交易 pnl 的平均（含費）。一勝一負兩筆驗證。"""
    cfg = make_cfg(stop_loss_pct=0.9, take_profit_pct=0.9)   # 拉寬停損停利，只讓信號出場
    risk = RiskOfficer(cfg)
    # bar0 進多@100 → bar1 平@110（賺）→ bar2 進多@110 → bar3 平@100（賠）
    df = df_with_sig([100.0, 110.0, 110.0, 100.0], [1, 0, 1, 0])
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    assert len(res.trades) == 2
    pnls = [t["pnl"] for t in res.trades]
    assert pnls[0] > 0 and pnls[1] < 0                       # 一勝一負
    assert res.expectancy == pytest.approx(sum(pnls) / 2, rel=1e-12, abs=1e-9)


def test_profit_factor_is_gross_profit_over_gross_loss():
    cfg = make_cfg(stop_loss_pct=0.9, take_profit_pct=0.9)
    risk = RiskOfficer(cfg)
    df = df_with_sig([100.0, 110.0, 110.0, 100.0], [1, 0, 1, 0])
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    pnls = [t["pnl"] for t in res.trades]
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    assert res.profit_factor == pytest.approx(gross_profit / gross_loss, rel=1e-12)


def test_profit_factor_infinite_when_no_losers():
    """全勝（無虧損交易）→ profit_factor = +inf，expectancy>0。"""
    cfg = make_cfg(stop_loss_pct=0.9, take_profit_pct=0.9)
    risk = RiskOfficer(cfg)
    df = df_with_sig([100.0, 110.0], [1, 0])                 # 單筆賺錢
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)

    assert len(res.trades) == 1 and res.trades[0]["pnl"] > 0
    assert res.profit_factor == float("inf")
    assert res.expectancy > 0


def test_expectancy_zero_when_no_trades():
    cfg = make_cfg()
    risk = RiskOfficer(cfg)
    df = df_with_sig([100.0, 100.0], [0, 0])                 # 從不進場
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)
    assert len(res.trades) == 0
    assert res.expectancy == 0.0
    assert res.profit_factor == 0.0


# ====================================================================
# 健全性：max_daily_loss_pct 開大確實關閉熔斷（能正常進場）
# ====================================================================
def test_circuit_breaker_disabled_allows_entry():
    cfg = make_cfg()
    assert cfg.max_daily_loss_pct == 10.0
    risk = RiskOfficer(cfg)
    df = df_with_sig([100.0, 100.0, 100.0], [1, 1, 0])
    res = run_backtest(df, SigStrategy(allow_short=False), risk, cfg)
    # 有成功進場並平倉 → 有一筆交易
    assert len(res.trades) == 1


# ── OPT-08：回測注入 funding + 成交對齊實盤(fill_lag)，讓回測-實盤偏差可診斷 ──
def test_funding_cost_reduces_long_equity():
    """持多付資金費（funding_rate_per_8h>0）→ 終值低於無 funding。預設 0 不影響。"""
    df = make_df([100.0] * 12)          # 平盤，排除價格 pnl 干擾
    df["sig"] = [1] * 12                # 全程做多
    cfg0 = make_cfg(funding_rate_per_8h=0.0, interval="5m", fee_rate=0.0)
    cfgf = make_cfg(funding_rate_per_8h=0.02, interval="5m", fee_rate=0.0)
    eq0 = run_backtest(df.copy(), SigStrategy(), RiskOfficer(cfg0), cfg0).equity_curve.iloc[-1]
    eqf = run_backtest(df.copy(), SigStrategy(), RiskOfficer(cfgf), cfg0 and cfgf).equity_curve.iloc[-1]
    assert eqf < eq0


def test_funding_default_zero_is_backward_compatible():
    """funding_rate_per_8h 預設 0 → 與舊回測結果完全一致。"""
    df = make_df([100.0] * 12)
    df["sig"] = [1] * 12
    assert Config().funding_rate_per_8h == 0.0


def test_funding_credits_short_position():
    """持空在正資金費率下「收」funding → 終值高於無 funding（方向相反）。"""
    df = make_df([100.0] * 12)
    df["sig"] = [-1] * 12
    cfg0 = make_cfg(funding_rate_per_8h=0.0, interval="5m", fee_rate=0.0)
    cfgf = make_cfg(funding_rate_per_8h=0.02, interval="5m", fee_rate=0.0)
    eq0 = run_backtest(df.copy(), SigStrategy(allow_short=True), RiskOfficer(cfg0), cfg0).equity_curve.iloc[-1]
    eqf = run_backtest(df.copy(), SigStrategy(allow_short=True), RiskOfficer(cfgf), cfgf).equity_curve.iloc[-1]
    assert eqf > eq0


def test_fill_lag_executes_signal_entry_at_next_bar_open():
    """fill_lag=1：訊號在第 i 根、成交在第 i+1 根 open。用 TP 價反推進場價（res.trades 只留出場）。"""
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0]
    df = make_df(closes)                # open=close、上升序列
    df["sig"] = [1] * 8
    cfg1 = make_cfg(fill_lag=1, interval="5m", fee_rate=0.0, slippage=0.0)
    cfg0 = make_cfg(fill_lag=0, interval="5m", fee_rate=0.0, slippage=0.0)
    tp1 = next(t for t in run_backtest(df.copy(), SigStrategy(), RiskOfficer(cfg1), cfg1).trades
               if t["side"] == "exit_sltp")["price"]
    tp0 = next(t for t in run_backtest(df.copy(), SigStrategy(), RiskOfficer(cfg0), cfg0).trades
               if t["side"] == "exit_sltp")["price"]
    # take_profit_pct=0.04：fill_lag=1 進場 101→TP 105.04；fill_lag=0 進場 100→TP 104
    assert tp1 == pytest.approx(101.0 * 1.04)
    assert tp0 == pytest.approx(100.0 * 1.04)
    assert tp1 > tp0                    # 上升序列延後一根進場價更高 → TP 更高


def test_fill_lag_zero_is_backward_compatible():
    assert Config().fill_lag == 0
