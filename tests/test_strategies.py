"""tests/test_strategies.py — core/quant_researcher.py 策略測試。

signal 新契約：signal(row, position) 回傳目標倉位 +1/0/-1。
僅做多策略（allow_short=False）只會回 0/+1；
ZScoreLongShort（allow_short=True）才會回 -1（做空）。

所有測試用確定性資料、明確 assert，不寫任何檔案到磁碟。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import (
    STRATEGIES,
    EMACrossStrategy,
    FibRetracementStrategy,
    Strategy,
    ZScoreLongShortStrategy,
    ZScoreRevertStrategy,
    build_strategy,
)


def _make_ohlc() -> pd.DataFrame:
    """確定性 OHLC：先跌（超賣）→ 大漲（金叉/超買）→ 回跌。

    足夠長以越過所有指標的 warmup（EMA slow=26、zscore window=50）。
    """
    prices = np.concatenate([
        np.linspace(100.0, 60.0, 40),   # 下跌段：z-score 走低
        np.linspace(60.0, 140.0, 40),   # 上漲段：EMA 金叉、z-score 走高
        np.linspace(140.0, 100.0, 40),  # 回跌段
    ])
    return pd.DataFrame({
        "open": prices,
        "high": prices + 1.0,
        "low": prices - 1.0,
        "close": prices,
    })


def _run_sequential(strat, df: pd.DataFrame) -> list[int]:
    """從空手開始，逐根把上一根的目標倉位當成下一根的目前倉位。"""
    out = []
    pos = 0
    prepared = strat.prepare(df)
    for _, row in prepared.iterrows():
        target = strat.signal(row, pos)
        out.append(target)
        pos = target
    return out


# --------------------------------------------------------------------------- #
# 1. 僅做多策略只會回 0 或 1（allow_short=False）
# --------------------------------------------------------------------------- #

def test_ema_cross_is_long_only():
    strat = EMACrossStrategy()
    assert strat.allow_short is False
    signals = _run_sequential(strat, _make_ohlc())
    assert set(signals) <= {0, 1}
    # 這段資料含明顯漲段，必有進場（出現過 1）才算有意義
    assert 1 in signals


def test_zscore_revert_is_long_only():
    strat = ZScoreRevertStrategy()
    assert strat.allow_short is False
    signals = _run_sequential(strat, _make_ohlc())
    assert set(signals) <= {0, 1}


# --------------------------------------------------------------------------- #
# 2. ZScoreLongShort：allow_short=True，空手時依 z 的方向開多/開空
# --------------------------------------------------------------------------- #

def test_zscore_ls_allow_short_flag():
    assert ZScoreLongShortStrategy.allow_short is True


def test_zscore_ls_flat_short_when_overbought():
    """空手 + z > entry_z（超買）→ 目標做空（-1）。"""
    strat = ZScoreLongShortStrategy()  # entry_z 預設 2.0
    row = pd.Series({"zscore": 3.0})
    assert strat.signal(row, 0) == -1


def test_zscore_ls_flat_long_when_oversold():
    """空手 + z < -entry_z（超賣）→ 目標做多（+1）。"""
    strat = ZScoreLongShortStrategy()
    row = pd.Series({"zscore": -3.0})
    assert strat.signal(row, 0) == 1


def test_zscore_ls_flat_stays_flat_inside_band():
    """空手 + |z| < entry_z（未達門檻）→ 維持空手（0）。"""
    strat = ZScoreLongShortStrategy()
    row = pd.Series({"zscore": 0.5})
    assert strat.signal(row, 0) == 0


def test_zscore_ls_emits_short_in_range_regime():
    """在盤整盤（regime='range'）上，多空策略確實會出現做空訊號 -1。"""
    strat = ZScoreLongShortStrategy(window=20, entry_z=1.0, exit_z=0.3)
    signals = _run_seq_gated(strat, _range_ohlc())
    assert -1 in signals
    assert set(signals) <= {-1, 0, 1}


# --------------------------------------------------------------------------- #
# 3. NaN 防護：指標為 NaN 時，三個策略都維持傳入的 position
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("position", [-1, 0, 1])
def test_ema_cross_nan_holds_position(position):
    strat = EMACrossStrategy()
    row = pd.Series({"ema_fast": np.nan, "ema_slow": 1.0, "rsi": 50.0})
    assert strat.signal(row, position) == position


@pytest.mark.parametrize("position", [-1, 0, 1])
def test_ema_cross_nan_rsi_holds_position(position):
    strat = EMACrossStrategy()
    row = pd.Series({"ema_fast": 2.0, "ema_slow": 1.0, "rsi": np.nan})
    assert strat.signal(row, position) == position


@pytest.mark.parametrize("position", [-1, 0, 1])
def test_zscore_revert_nan_holds_position(position):
    strat = ZScoreRevertStrategy()
    row = pd.Series({"zscore": np.nan})
    assert strat.signal(row, position) == position


@pytest.mark.parametrize("position", [-1, 0, 1])
def test_zscore_ls_nan_holds_position(position):
    strat = ZScoreLongShortStrategy()
    row = pd.Series({"zscore": np.nan})
    assert strat.signal(row, position) == position


# --------------------------------------------------------------------------- #
# 4. build_strategy / STRATEGIES 註冊表
# --------------------------------------------------------------------------- #

def test_build_strategy_unknown_raises():
    with pytest.raises(ValueError):
        build_strategy("does_not_exist")


def test_build_strategy_returns_correct_types():
    assert isinstance(build_strategy("ema_cross"), EMACrossStrategy)
    assert isinstance(build_strategy("zscore_revert"), ZScoreRevertStrategy)
    assert isinstance(build_strategy("zscore_ls"), ZScoreLongShortStrategy)


def test_build_strategy_passes_params():
    strat = build_strategy("zscore_ls", entry_z=1.5, window=30)
    assert strat.params["entry_z"] == 1.5
    assert strat.params["window"] == 30


def test_strategies_registry_has_four_keys():
    assert set(STRATEGIES) == {"ema_cross", "zscore_revert", "zscore_ls", "fib_retracement"}
    assert STRATEGIES["ema_cross"] is EMACrossStrategy
    assert STRATEGIES["zscore_revert"] is ZScoreRevertStrategy
    assert STRATEGIES["zscore_ls"] is ZScoreLongShortStrategy
    assert STRATEGIES["fib_retracement"] is FibRetracementStrategy


# --------------------------------------------------------------------------- #
# 5. FibRetracementStrategy
# --------------------------------------------------------------------------- #

def _fib_row(fib_pos, rsi, position=0):
    """建構一個帶有 fib_pos / rsi 欄位的測試 row。"""
    return pd.Series({"fib_pos": fib_pos, "rsi": rsi,
                      "fib_high": 110.0, "fib_low": 90.0,
                      "fib_382": 97.18, "fib_618": 102.36})


def test_fib_allow_short_flag():
    """FibRetracementStrategy 必須允許做空。"""
    assert FibRetracementStrategy.allow_short is True


def test_fib_long_signal_at_support():
    """fib_pos < 0.382 且 RSI < 55 → 空手時做多（+1）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.20, rsi=40)
    assert strat.signal(row, 0) == 1


def test_fib_short_signal_at_resistance():
    """fib_pos > 0.618 且 RSI < 50（動能轉弱）→ 空手時做空（-1）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.85, rsi=40)
    assert strat.signal(row, 0) == -1


def test_fib_exit_long_at_midline():
    """持多 + fib_pos > 0.55 → 平多（0）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.70, rsi=55)
    assert strat.signal(row, 1) == 0


def test_fib_exit_short_at_midline():
    """持空 + fib_pos < 0.45 → 平空（0）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.20, rsi=40)
    assert strat.signal(row, -1) == 0


def test_fib_holds_long_while_below_midline():
    """持多 + fib_pos 在 0.382~0.55 之間 → 續抱（+1）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.50, rsi=48)
    assert strat.signal(row, 1) == 1


def test_fib_holds_short_while_above_midline():
    """持空 + fib_pos 在 0.45~0.618 之間 → 續空（-1）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.50, rsi=55)
    assert strat.signal(row, -1) == -1


def test_fib_no_long_when_rsi_overbought():
    """fib_pos < 0.382 但 RSI >= 55 → 不進場（保持空手）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.20, rsi=60)
    assert strat.signal(row, 0) == 0


def test_fib_no_short_when_rsi_above_mid():
    """fib_pos > 0.618 但 RSI >= 50（動能仍強，不宜逆勢空）→ 不做空（保持空手）。"""
    strat = FibRetracementStrategy()
    row = _fib_row(fib_pos=0.85, rsi=60)
    assert strat.signal(row, 0) == 0


def test_fib_nan_guard_returns_current_position():
    """任何指標為 NaN 時，維持現有倉位不動。"""
    strat = FibRetracementStrategy()
    for pos in (0, 1, -1):
        row = _fib_row(fib_pos=float("nan"), rsi=50)
        assert strat.signal(row, pos) == pos


def test_fib_long_requires_uptrend():
    """#6 順勢回調：支撐區做多須處於上升趨勢（close > ema_trend）；下降趨勢則不接刀。"""
    strat = FibRetracementStrategy()
    up = _fib_row(fib_pos=0.20, rsi=40); up["close"] = 105.0; up["ema_trend"] = 100.0
    assert strat.signal(up, 0) == 1
    down = _fib_row(fib_pos=0.20, rsi=40); down["close"] = 95.0; down["ema_trend"] = 100.0
    assert strat.signal(down, 0) == 0


def test_fib_short_requires_downtrend():
    """#6 順勢回調：阻力區做空須處於下降趨勢（close < ema_trend）；上升趨勢則不接刀。"""
    strat = FibRetracementStrategy()
    down = _fib_row(fib_pos=0.85, rsi=40); down["close"] = 95.0; down["ema_trend"] = 100.0
    assert strat.signal(down, 0) == -1
    up = _fib_row(fib_pos=0.85, rsi=40); up["close"] = 105.0; up["ema_trend"] = 100.0
    assert strat.signal(up, 0) == 0


def test_fib_in_strategies_registry():
    """fib_retracement 已加入 STRATEGIES 字典。"""
    assert "fib_retracement" in STRATEGIES
    assert STRATEGIES["fib_retracement"] is FibRetracementStrategy


def test_build_fib_strategy():
    """build_strategy('fib_retracement') 回傳正確實例。"""
    strat = build_strategy("fib_retracement", lookback=30)
    assert isinstance(strat, FibRetracementStrategy)
    assert strat.params["lookback"] == 30


# --------------------------------------------------------------------------- #
# 6. Regime 閘門（ER+CHOP+ADX 2-of-3，順勢策略只在趨勢出手、均值回歸只在盤整出手）
# --------------------------------------------------------------------------- #
def _strong_uptrend_ohlc(n=160, step=1.5):
    # 明確多頭但含真實回檔：noise scale=1.0 → 約 7% 下跌根，RSI 有定義，仍是強趨勢（ER/ADX 高）
    rng = np.random.RandomState(0)
    base = 1000.0 + np.cumsum(step + rng.normal(scale=1.0, size=n))
    return pd.DataFrame({"open": base, "high": base + 1.0, "low": base - 1.0, "close": base})


def _range_ohlc(n=220, seed=3):
    # 純白噪音繞同一水位 → 明確盤整（ER 低、CHOP 高、ADX 低，三票一致 'range'），且 |z| 常破門檻
    rng = np.random.RandomState(seed)
    base = 1000.0 + rng.normal(scale=15.0, size=n)
    return pd.DataFrame({"open": base, "high": base + 1.0, "low": base - 1.0, "close": base})


def _run_seq_gated(strat, df):
    """模擬真實 backtester 路徑：prepare().dropna() 後逐根餵入（regime 必為已確認值）。"""
    out, pos = [], 0
    prepared = strat.prepare(df).dropna()
    for _, row in prepared.iterrows():
        target = strat.signal(row, pos)
        out.append(target)
        pos = target
    return out


def test_regime_pref_defaults():
    """各策略宣告正確的市場偏好：順勢=trend、均值回歸=range，基類預設 any。"""
    assert Strategy.regime_pref == "any"
    assert EMACrossStrategy.regime_pref == "trend"
    assert ZScoreRevertStrategy.regime_pref == "range"
    assert ZScoreLongShortStrategy.regime_pref == "range"
    assert FibRetracementStrategy.regime_pref == "range"


def test_regime_ok_matches_pref():
    """_regime_ok：regime 與偏好相符放行、不符擋下。"""
    strat = ZScoreLongShortStrategy()  # pref='range'
    assert strat._regime_ok(pd.Series({"regime": "range"})) is True
    assert strat._regime_ok(pd.Series({"regime": "trend"})) is False


def test_regime_ok_allows_when_absent_or_none():
    """精簡 row（無 regime 欄）或 regime=None 時不阻擋（向後相容單元測試 / warmup）。"""
    strat = ZScoreLongShortStrategy()
    assert strat._regime_ok(pd.Series({"zscore": 3.0})) is True
    assert strat._regime_ok(pd.Series({"regime": None})) is True


def test_zscore_ls_no_countertrend_in_strong_trend():
    """破口修補：強趨勢盤即使 |z|>entry，多空策略也不開逆勢單（不出現 -1）。"""
    strat = ZScoreLongShortStrategy(window=20, entry_z=1.0, exit_z=0.3)
    signals = _run_seq_gated(strat, _strong_uptrend_ohlc())
    assert -1 not in signals


def test_ema_cross_no_entry_in_range_regime():
    """順勢策略在盤整盤被閘門擋下：不進場（不出現 1）。"""
    strat = EMACrossStrategy()
    signals = _run_seq_gated(strat, _range_ohlc())
    assert 1 not in signals


def test_ema_cross_enters_in_trend_regime():
    """順勢策略在趨勢盤正常進場（出現 1）。

    用 rsi_max=100 隔離 regime 閘門本身——強多頭中 RSI 常 >70，舊的 rsi<70 過濾會擋住進場
    （正是 #4 要修的鈍化問題），這裡單獨驗證「趨勢盤 regime 放行」。
    """
    strat = EMACrossStrategy(rsi_max=100)
    signals = _run_seq_gated(strat, _strong_uptrend_ohlc())
    assert 1 in signals


# --------------------------------------------------------------------------- #
# 7. #4 RSI(50) 中線方向閘門 + #5 EMA 交叉緩衝帶（ATR separation band）
# --------------------------------------------------------------------------- #
def _ema_row(ema_fast, ema_slow, rsi, atr=1.0, regime="trend"):
    return pd.Series({"ema_fast": ema_fast, "ema_slow": ema_slow,
                      "rsi": rsi, "atr": atr, "regime": regime})


def test_ema_cross_requires_rsi_above_mid():
    """rsi <= 50（動能未確認）→ 不進場，即使金叉+分離+趨勢都成立。"""
    strat = EMACrossStrategy()
    assert strat.signal(_ema_row(110.0, 100.0, rsi=45.0), 0) == 0


def test_ema_cross_enters_with_rsi_above_mid():
    """50 < rsi < rsi_max + 金叉 + 分離足夠 + 趨勢盤 → 進場做多。"""
    strat = EMACrossStrategy()
    assert strat.signal(_ema_row(110.0, 100.0, rsi=60.0), 0) == 1


def test_ema_cross_enters_when_strong_but_below_max():
    """rsi=75（舊 rsi<70 會擋）現在放行——修補強趨勢鈍化（rsi_max 放寬到 80）。"""
    strat = EMACrossStrategy()
    assert strat.signal(_ema_row(110.0, 100.0, rsi=75.0), 0) == 1


def test_ema_cross_blocks_extreme_overbought():
    """rsi >= rsi_max(80)：極端過熱仍不追高。"""
    strat = EMACrossStrategy()
    assert strat.signal(_ema_row(110.0, 100.0, rsi=85.0), 0) == 0


def test_ema_cross_separation_band_blocks_marginal_cross():
    """分離 < sep_atr_k*atr（貼零軸抖動）→ 不進場。"""
    strat = EMACrossStrategy()  # sep_atr_k=0.5, atr=1 → 門檻 0.5
    assert strat.signal(_ema_row(100.2, 100.0, rsi=60.0), 0) == 0   # 分離 0.2 < 0.5


def test_ema_cross_separation_band_allows_clear_cross():
    """分離 > sep_atr_k*atr → 放行。"""
    strat = EMACrossStrategy()
    assert strat.signal(_ema_row(102.0, 100.0, rsi=60.0), 0) == 1   # 分離 2.0 > 0.5


def test_ema_cross_exit_uses_bare_cross_hysteresis():
    """持多出場只需裸死叉（不需反向分離）；仍金叉即使分離不足也續抱。"""
    strat = EMACrossStrategy()
    assert strat.signal(_ema_row(99.9, 100.0, rsi=60.0), 1) == 0    # 死叉 → 平
    assert strat.signal(_ema_row(100.2, 100.0, rsi=60.0), 1) == 1   # 仍金叉（分離不足）→ 續抱
