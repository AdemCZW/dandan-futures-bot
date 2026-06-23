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


def test_strategies_registry_has_core_seven_keys():
    # 原始 7 核心策略仍在註冊表（新增短線策略後改子集檢查；完整集合見 *_twelve_keys）
    assert {"ema_cross", "zscore_revert", "zscore_ls",
            "fib_retracement", "supertrend", "donchian", "of_momentum"} <= set(STRATEGIES)
    assert STRATEGIES["ema_cross"] is EMACrossStrategy
    assert STRATEGIES["zscore_revert"] is ZScoreRevertStrategy
    assert STRATEGIES["zscore_ls"] is ZScoreLongShortStrategy
    assert STRATEGIES["fib_retracement"] is FibRetracementStrategy
    assert STRATEGIES["supertrend"] is SupertrendStrategy


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


# --------------------------------------------------------------------------- #
# 市場結構（訂單流）確認閘門 _structure_ok
#   taker_ratio_s = 平滑後主動買盤佔比 ∈ [0,1]。
#   多單：買盤須 ≥ of_long_min（不逆著賣壓做多）；
#   空單：買盤須 ≤ of_short_max（不逆著買盤做空）。
#   缺欄/NaN/use_structure=False → 一律放行（優雅退化、向後相容）。
# --------------------------------------------------------------------------- #
def _struct_row(taker_ratio_s):
    return pd.Series({"close": 100.0, "taker_ratio_s": taker_ratio_s})


def test_structure_gate_off_by_default_is_backward_compatible():
    """預設 use_structure=False → 任何訂單流值都放行（不改既有行為）。"""
    strat = FibRetracementStrategy()
    assert strat.params.get("use_structure", False) is False
    assert strat._structure_ok(_struct_row(0.01), direction=1) is True
    assert strat._structure_ok(_struct_row(0.99), direction=-1) is True


def test_structure_gate_missing_column_passes():
    """缺 taker_ratio_s 欄（合成資料）→ 放行，即使閘門開啟。"""
    strat = FibRetracementStrategy(use_structure=True)
    assert strat._structure_ok(pd.Series({"close": 100.0}), direction=1) is True


def test_structure_gate_nan_passes():
    strat = FibRetracementStrategy(use_structure=True)
    assert strat._structure_ok(_struct_row(float("nan")), direction=1) is True


def test_structure_gate_blocks_long_into_selling():
    """開啟閘門：主動買盤過低（賣壓重）→ 擋掉做多。"""
    strat = FibRetracementStrategy(use_structure=True, of_long_min=0.45)
    assert strat._structure_ok(_struct_row(0.30), direction=1) is False
    assert strat._structure_ok(_struct_row(0.50), direction=1) is True


def test_structure_gate_blocks_short_into_buying():
    """開啟閘門：主動買盤過高（買壓重）→ 擋掉做空。"""
    strat = FibRetracementStrategy(use_structure=True, of_short_max=0.55)
    assert strat._structure_ok(_struct_row(0.70), direction=-1) is False
    assert strat._structure_ok(_struct_row(0.50), direction=-1) is True


def test_structure_prepare_adds_smoothed_ratio_when_taker_present():
    """prepare() 在有 taker_base 時加入 taker_ratio_s 欄供 signal/前端使用。"""
    n = 80
    px = np.linspace(100.0, 120.0, n)
    df = pd.DataFrame({
        "open": px, "high": px + 1, "low": px - 1, "close": px,
        "volume": np.full(n, 10.0), "taker_base": np.full(n, 7.0),
    })
    out = FibRetracementStrategy(use_structure=True).prepare(df)
    assert "taker_ratio_s" in out.columns
    # 7/10 買盤 → 平滑後仍 ≈ 0.7
    assert out["taker_ratio_s"].dropna().iloc[-1] == pytest.approx(0.7, abs=1e-6)


def test_structure_gate_blocks_a_long_fib_would_otherwise_take():
    """整合：fib 想做多的列，疊上重賣壓訂單流後變成不進場。"""
    strat = FibRetracementStrategy(use_structure=True, of_long_min=0.45)
    # 在多單黃金支撐區、RSI 未過熱、上升趨勢、盤整盤 → 純 TA 會回 1
    base = {"fib_pos": 0.20, "rsi": 45.0, "close": 101.0, "ema_trend": 100.0,
            "regime": "range"}
    assert strat.signal(pd.Series(base), 0) == 1                       # 無訂單流欄 → 放行
    blocked = pd.Series({**base, "taker_ratio_s": 0.25})               # 重賣壓
    assert strat.signal(blocked, 0) == 0                               # 被訂單流閘門擋下
    allowed = pd.Series({**base, "taker_ratio_s": 0.60})               # 買盤支持
    assert strat.signal(allowed, 0) == 1


# --------------------------------------------------------------------------- #
# SupertrendStrategy（ATR 趨勢跟蹤，多空雙向）
#   跟隨 st_dir 翻轉進出；訂單流閘門只擋「新開/翻倉」，不強制平既有倉。
# --------------------------------------------------------------------------- #
from core.quant_researcher import SupertrendStrategy


def _st_row(st_dir, taker_ratio_s=None):
    d = {"close": 100.0, "st_dir": st_dir}
    if taker_ratio_s is not None:
        d["taker_ratio_s"] = taker_ratio_s
    return pd.Series(d)


def test_supertrend_strategy_allows_short():
    assert SupertrendStrategy.allow_short is True


def test_supertrend_enters_long_on_uptrend_flat():
    strat = SupertrendStrategy()
    assert strat.signal(_st_row(1.0), 0) == 1


def test_supertrend_enters_short_on_downtrend_flat():
    strat = SupertrendStrategy()
    assert strat.signal(_st_row(-1.0), 0) == -1


def test_supertrend_holds_through_adverse_flow_no_forced_exit():
    """既有多倉、方向仍多 → 續抱，即使訂單流轉弱也不被閘門踢出。"""
    strat = SupertrendStrategy(use_structure=True, of_long_min=0.45)
    assert strat.signal(_st_row(1.0, taker_ratio_s=0.20), 1) == 1


def test_supertrend_structure_gate_blocks_fresh_long():
    """開啟閘門：空手要新開多單，但賣壓重 → 不進場(0)。"""
    strat = SupertrendStrategy(use_structure=True, of_long_min=0.45)
    assert strat.signal(_st_row(1.0, taker_ratio_s=0.25), 0) == 0
    assert strat.signal(_st_row(1.0, taker_ratio_s=0.60), 0) == 1


def test_supertrend_nan_direction_holds_position():
    strat = SupertrendStrategy()
    assert strat.signal(_st_row(float("nan")), 1) == 1
    assert strat.signal(_st_row(float("nan")), 0) == 0


def test_supertrend_in_strategies_registry():
    assert "supertrend" in STRATEGIES


# --------------------------------------------------------------------------- #
# DonchianBreakoutStrategy（海龜突破，多空雙向）
#   收盤突破進場通道上軌做多/下軌做空；跌破出場通道平倉。訂單流閘門擋新開倉。
# --------------------------------------------------------------------------- #
from core.quant_researcher import DonchianBreakoutStrategy


def _dc_row(close, upper, lower, exit_long, exit_short, taker_ratio_s=None):
    d = {"close": close, "dc_upper": upper, "dc_lower": lower,
         "dc_exit_long": exit_long, "dc_exit_short": exit_short}
    if taker_ratio_s is not None:
        d["taker_ratio_s"] = taker_ratio_s
    return pd.Series(d)


def test_donchian_strategy_allows_short():
    assert DonchianBreakoutStrategy.allow_short is True


def test_donchian_breakout_long_on_upper_break():
    strat = DonchianBreakoutStrategy()
    # close 突破上軌 110 → 做多
    assert strat.signal(_dc_row(111, 110, 90, 95, 105), 0) == 1


def test_donchian_breakout_short_on_lower_break():
    strat = DonchianBreakoutStrategy()
    # close 跌破下軌 90 → 做空
    assert strat.signal(_dc_row(89, 110, 90, 95, 105), 0) == -1


def test_donchian_no_signal_inside_channel():
    strat = DonchianBreakoutStrategy()
    assert strat.signal(_dc_row(100, 110, 90, 95, 105), 0) == 0


def test_donchian_long_exits_on_exit_channel_break():
    strat = DonchianBreakoutStrategy()
    # 持多，close 跌破 exit_long 95 → 平倉
    assert strat.signal(_dc_row(94, 110, 90, 95, 105), 1) == 0
    # 仍在通道內 → 續抱
    assert strat.signal(_dc_row(100, 110, 90, 95, 105), 1) == 1


def test_donchian_structure_gate_blocks_fresh_breakout_long():
    strat = DonchianBreakoutStrategy(use_structure=True, of_long_min=0.45)
    assert strat.signal(_dc_row(111, 110, 90, 95, 105, taker_ratio_s=0.25), 0) == 0
    assert strat.signal(_dc_row(111, 110, 90, 95, 105, taker_ratio_s=0.60), 0) == 1


def test_donchian_in_strategies_registry():
    assert "donchian" in STRATEGIES


# --------------------------------------------------------------------------- #
# OrderFlowMomentumStrategy（CVD 訂單流動量，多空雙向，短線用）
#   主訊號＝CVD 的快/慢 EMA 交叉（MACD-on-CVD）：買盤動量強做多、賣盤動量強做空。
#   缺 taker_base（合成資料）→ of_fast/of_slow 為 NaN → 維持現狀（優雅退化）。
# --------------------------------------------------------------------------- #
from core.quant_researcher import OrderFlowMomentumStrategy


def _ofm_row(of_fast, of_slow):
    return pd.Series({"close": 100.0, "of_fast": of_fast, "of_slow": of_slow})


def test_ofm_allows_short():
    assert OrderFlowMomentumStrategy.allow_short is True


def test_ofm_long_when_flow_momentum_up():
    strat = OrderFlowMomentumStrategy()
    assert strat.signal(_ofm_row(120.0, 100.0), 0) == 1      # 買盤動量 → 做多


def test_ofm_short_when_flow_momentum_down():
    strat = OrderFlowMomentumStrategy()
    assert strat.signal(_ofm_row(80.0, 100.0), 0) == -1      # 賣盤動量 → 做空


def test_ofm_nan_holds_position():
    strat = OrderFlowMomentumStrategy()
    assert strat.signal(_ofm_row(float("nan"), 100.0), 1) == 1
    assert strat.signal(_ofm_row(50.0, float("nan")), -1) == -1


def test_ofm_prepare_adds_flow_emas_when_taker_present():
    n = 120
    px = np.linspace(100, 110, n)
    # 前半買盤主導(taker 高)、後半賣盤主導(taker 低) → CVD 先升後降
    taker = np.r_[np.full(n // 2, 9.0), np.full(n - n // 2, 1.0)]
    df = pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px,
                       "volume": np.full(n, 10.0), "taker_base": taker})
    out = OrderFlowMomentumStrategy().prepare(df)
    assert "of_fast" in out.columns and "of_slow" in out.columns
    # 末段賣盤主導 → CVD 下行 → of_fast < of_slow（做空傾向）
    assert out["of_fast"].iloc[-1] < out["of_slow"].iloc[-1]


def test_ofm_graceful_without_taker_column():
    """合成資料無 taker_base → of_fast/of_slow 全 NaN → signal 維持現狀。"""
    n = 60
    px = np.linspace(100, 110, n)
    df = pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px,
                       "volume": np.ones(n)})
    out = OrderFlowMomentumStrategy().prepare(df)
    assert out["of_fast"].isna().all()
    for pos in (-1, 0, 1):
        assert OrderFlowMomentumStrategy().signal(out.iloc[-1], pos) == pos


def test_ofm_in_strategies_registry():
    assert "of_momentum" in STRATEGIES


# --------------------------------------------------------------------------- #
# BOT_PARAMS JSON 解析（run_live_futures 的 parse_bot_params helper）
#   環境變數格式：'{"use_htf_filter": true, "htf_ema_period": 200}'
#   空字串 / 未設 → 空 dict；無效 JSON → 空 dict（不崩潰）。
# --------------------------------------------------------------------------- #
from run_live_futures import parse_bot_params


def test_parse_bot_params_empty_string_returns_empty():
    assert parse_bot_params("") == {}


def test_parse_bot_params_none_returns_empty():
    assert parse_bot_params(None) == {}


def test_parse_bot_params_valid_json():
    result = parse_bot_params('{"use_htf_filter": true, "htf_ema_period": 200}')
    assert result == {"use_htf_filter": True, "htf_ema_period": 200}


def test_parse_bot_params_invalid_json_returns_empty():
    result = parse_bot_params("{bad json}")
    assert result == {}


# --------------------------------------------------------------------------- #
# HTF（高時框）趨勢過濾器 — SupertrendStrategy
#   use_htf_filter=False 預設關閉（向後相容）。
#   開啟後：close > ema_trend → 只做多；close < ema_trend → 只做空。
#   既有倉位不被 HTF 閘門強制平出（與訂單流閘門設計相同）。
#   ema_trend 為 NaN（暖機期）→ 優雅放行。
# --------------------------------------------------------------------------- #

def _st_row_htf(st_dir, close, ema_trend, taker_ratio_s=None):
    d = {"close": close, "st_dir": st_dir, "ema_trend": ema_trend}
    if taker_ratio_s is not None:
        d["taker_ratio_s"] = taker_ratio_s
    return pd.Series(d)


def test_supertrend_htf_filter_off_by_default():
    """預設 use_htf_filter=False → HTF 不過濾，做多皆通過（向後相容）。"""
    strat = SupertrendStrategy()
    assert strat.signal(_st_row_htf(1.0, close=90.0, ema_trend=100.0), 0) == 1


def test_supertrend_htf_filter_blocks_long_in_downtrend():
    """開啟 use_htf_filter + close < ema_trend（下降趨勢）→ 擋掉做多。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(1.0, close=90.0, ema_trend=100.0), 0) == 0


def test_supertrend_htf_filter_blocks_short_in_uptrend():
    """開啟 use_htf_filter + close > ema_trend（上升趨勢）→ 擋掉做空。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(-1.0, close=110.0, ema_trend=100.0), 0) == 0


def test_supertrend_htf_filter_allows_long_in_uptrend():
    """開啟 use_htf_filter + close > ema_trend → 放行做多。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(1.0, close=110.0, ema_trend=100.0), 0) == 1


def test_supertrend_htf_filter_allows_short_in_downtrend():
    """開啟 use_htf_filter + close < ema_trend → 放行做空。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(-1.0, close=90.0, ema_trend=100.0), 0) == -1


def test_supertrend_htf_filter_does_not_force_exit_existing_long():
    """持多倉 + HTF 下降趨勢 → 不強制平倉（只擋新開倉）。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(1.0, close=90.0, ema_trend=100.0), 1) == 1


def test_supertrend_htf_filter_does_not_force_exit_existing_short():
    """持空倉 + HTF 上升趨勢 → 不強制平倉（只擋新開倉）。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(-1.0, close=110.0, ema_trend=100.0), -1) == -1


def test_supertrend_htf_nan_ema_passes_through():
    """ema_trend 為 NaN（暖機期）→ HTF 閘門不阻擋（優雅退化）。"""
    strat = SupertrendStrategy(use_htf_filter=True)
    assert strat.signal(_st_row_htf(1.0, close=100.0, ema_trend=float("nan")), 0) == 1


def test_supertrend_htf_prepare_adds_ema_trend():
    """prepare() 一律計算 ema_trend 欄位。"""
    n = 220
    px = np.linspace(100.0, 200.0, n)
    df = pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px})
    out = SupertrendStrategy(use_htf_filter=True, htf_ema_period=50).prepare(df)
    assert "ema_trend" in out.columns
    assert not out["ema_trend"].isna().all()


# =========================================================================== #
# 新增短線策略（研究 + 對抗式驗證後挑選的 5 個，皆 stateless 單列 signal）。
# 全部多空雙向、emit +1/0/-1，從空手才開新倉（不直接翻倉）。
# =========================================================================== #
from core.quant_researcher import (
    VwapBandReversionStrategy, HeikinAshiMomoStrategy, MacdScalpStrategy,
    BollingerSqueezeStrategy, Rsi2ConnorsStrategy,
)


# --- 1. vwap_band_reversion（rolling VWAP 偏離 + 影線拒絕，盤整均值回歸） ----
def _vwap_row(vwdist_z, lower_wick=0.6, upper_wick=0.6, close=100.0,
              vwap_roll=100.0, regime="range"):
    return pd.Series({"close": close, "vwap_roll": vwap_roll, "vwdist_z": vwdist_z,
                      "lower_wick_frac": lower_wick, "upper_wick_frac": upper_wick,
                      "regime": regime})


def test_vwap_allows_short_and_registered():
    assert VwapBandReversionStrategy.allow_short is True
    assert STRATEGIES["vwap_band_reversion"] is VwapBandReversionStrategy


def test_vwap_long_on_deep_negative_z_with_lower_wick():
    s = VwapBandReversionStrategy()              # k=2.2, wick_frac=0.5
    assert s.signal(_vwap_row(-3.0, lower_wick=0.6), 0) == 1


def test_vwap_short_on_deep_positive_z_with_upper_wick():
    s = VwapBandReversionStrategy()
    assert s.signal(_vwap_row(3.0, upper_wick=0.6), 0) == -1


def test_vwap_no_entry_without_rejection_wick():
    s = VwapBandReversionStrategy()
    assert s.signal(_vwap_row(-3.0, lower_wick=0.2), 0) == 0     # 影線不足 → 不接刀


def test_vwap_no_entry_outside_range_regime():
    s = VwapBandReversionStrategy()
    assert s.signal(_vwap_row(-3.0, regime="trend"), 0) == 0     # 趨勢盤不做均值回歸


def test_vwap_exit_long_back_to_fair_value():
    s = VwapBandReversionStrategy()
    # 持多，價回到 VWAP 上方 → 平倉
    assert s.signal(_vwap_row(0.0, close=101.0, vwap_roll=100.0), 1) == 0


def test_vwap_hold_long_while_below_fair_value():
    s = VwapBandReversionStrategy()
    assert s.signal(_vwap_row(-1.5, close=98.0, vwap_roll=100.0), 1) == 1


@pytest.mark.parametrize("pos", [-1, 0, 1])
def test_vwap_nan_holds_position(pos):
    s = VwapBandReversionStrategy()
    assert s.signal(_vwap_row(float("nan")), pos) == pos


# --- 2. heikin_ashi_momo（HA 顏色連續 + 強實體，順勢續抱） -------------------
def _ha_row(ha_color, lower_wick=0.05, upper_wick=0.05, run=3,
            close=110.0, ema_trend=100.0):
    return pd.Series({"ha_color": ha_color, "ha_lower_wick_frac": lower_wick,
                      "ha_upper_wick_frac": upper_wick, "ha_same_color_run": run,
                      "close": close, "ema_trend": ema_trend})


def test_ha_allows_short_and_registered():
    assert HeikinAshiMomoStrategy.allow_short is True
    assert STRATEGIES["heikin_ashi_momo"] is HeikinAshiMomoStrategy


def test_ha_long_on_strong_bull_run_in_uptrend():
    s = HeikinAshiMomoStrategy()                 # wick_frac=0.15, min_run=2
    assert s.signal(_ha_row(1.0, lower_wick=0.05, run=3, close=110, ema_trend=100), 0) == 1


def test_ha_short_on_strong_bear_run_in_downtrend():
    s = HeikinAshiMomoStrategy()
    assert s.signal(_ha_row(-1.0, upper_wick=0.05, run=3, close=90, ema_trend=100), 0) == -1


def test_ha_no_long_with_big_lower_wick():
    s = HeikinAshiMomoStrategy()
    assert s.signal(_ha_row(1.0, lower_wick=0.5, run=3), 0) == 0    # 下影線太長＝動能弱


def test_ha_no_long_below_trend_filter():
    s = HeikinAshiMomoStrategy()
    assert s.signal(_ha_row(1.0, close=90, ema_trend=100), 0) == 0  # 在 EMA 下方不做多


def test_ha_no_long_when_run_too_short():
    s = HeikinAshiMomoStrategy()
    assert s.signal(_ha_row(1.0, run=1), 0) == 0                    # 連續根數不足


def test_ha_exit_long_on_color_flip():
    s = HeikinAshiMomoStrategy()
    assert s.signal(_ha_row(-1.0, close=110, ema_trend=100), 1) == 0


@pytest.mark.parametrize("pos", [-1, 0, 1])
def test_ha_nan_holds_position(pos):
    s = HeikinAshiMomoStrategy()
    assert s.signal(_ha_row(float("nan")), pos) == pos


# --- 3. macd_scalp（價格 MACD 零軸 + ADX 趨勢閘門） -------------------------
def _macd_row(line, signal, hist, hist_prev, hist_prev2, cross_up, cross_dn,
              close=110.0, ema_trend=100.0, adx=25.0, regime="trend"):
    return pd.Series({"macd_line": line, "macd_signal": signal, "macd_hist": hist,
                      "macd_hist_prev": hist_prev, "macd_hist_prev2": hist_prev2,
                      "cross_up": cross_up, "cross_dn": cross_dn,
                      "close": close, "ema_trend": ema_trend, "adx": adx, "regime": regime})


def test_macd_allows_short_and_registered():
    assert MacdScalpStrategy.allow_short is True
    assert STRATEGIES["macd_scalp"] is MacdScalpStrategy


def test_macd_long_on_bull_cross_above_zero_in_trend():
    s = MacdScalpStrategy()                       # adx_min=18
    row = _macd_row(5.0, 4.0, 2.0, 1.0, 0.5, True, False, close=110, ema_trend=100, adx=25)
    assert s.signal(row, 0) == 1


def test_macd_short_on_bear_cross_below_zero_in_trend():
    s = MacdScalpStrategy()
    row = _macd_row(-5.0, -4.0, -2.0, -1.0, -0.5, False, True, close=90, ema_trend=100, adx=25)
    assert s.signal(row, 0) == -1


def test_macd_no_long_below_zero_line():
    s = MacdScalpStrategy()
    row = _macd_row(-1.0, -2.0, 0.5, 0.2, 0.0, True, False, close=110, ema_trend=100, adx=25)
    assert s.signal(row, 0) == 0                  # macd_line<0 → 零軸過濾擋下


def test_macd_no_entry_when_adx_below_min():
    s = MacdScalpStrategy()
    row = _macd_row(5.0, 4.0, 2.0, 1.0, 0.5, True, False, close=110, ema_trend=100, adx=10)
    assert s.signal(row, 0) == 0                  # 無趨勢強度 → 不進場


def test_macd_exit_long_on_two_bar_hist_fade():
    s = MacdScalpStrategy()
    # 持多，柱狀圖連兩根走弱（hist<prev<prev2）→ 動能衰竭出場
    row = _macd_row(5.0, 4.5, 0.5, 1.0, 1.5, False, False, close=110, ema_trend=100, adx=25)
    assert s.signal(row, 1) == 0


@pytest.mark.parametrize("pos", [-1, 0, 1])
def test_macd_nan_holds_position(pos):
    s = MacdScalpStrategy()
    row = _macd_row(float("nan"), 4.0, 2.0, 1.0, 0.5, True, False)
    assert s.signal(row, pos) == pos


# --- 4. bb_squeeze_breakout（布林帶寬壓縮 → 波動突破） ----------------------
def _bb_row(close, squeeze_prev, pct_b=1.2, bb_mid=100.0, bb_upper=110.0,
            bb_lower=90.0, adx=25.0):
    return pd.Series({"close": close, "squeeze_prev": squeeze_prev, "pct_b": pct_b,
                      "bb_mid": bb_mid, "bb_upper": bb_upper, "bb_lower": bb_lower,
                      "adx": adx})


def test_bb_allows_short_and_registered():
    assert BollingerSqueezeStrategy.allow_short is True
    assert STRATEGIES["bb_squeeze_breakout"] is BollingerSqueezeStrategy


def test_bb_long_on_squeeze_breakout_up():
    s = BollingerSqueezeStrategy()                # adx_min=20
    assert s.signal(_bb_row(111.0, squeeze_prev=True, bb_upper=110.0, adx=25), 0) == 1


def test_bb_short_on_squeeze_breakdown():
    s = BollingerSqueezeStrategy()
    assert s.signal(_bb_row(89.0, squeeze_prev=True, bb_lower=90.0, adx=25), 0) == -1


def test_bb_no_breakout_without_prior_squeeze():
    s = BollingerSqueezeStrategy()
    assert s.signal(_bb_row(111.0, squeeze_prev=False, bb_upper=110.0, adx=25), 0) == 0


def test_bb_no_breakout_when_adx_weak():
    s = BollingerSqueezeStrategy()
    assert s.signal(_bb_row(111.0, squeeze_prev=True, bb_upper=110.0, adx=10), 0) == 0


def test_bb_exit_long_back_through_mid():
    s = BollingerSqueezeStrategy()
    # 持多，跌回中軌下 → 突破失敗出場
    assert s.signal(_bb_row(99.0, squeeze_prev=False, pct_b=0.3, bb_mid=100.0), 1) == 0


@pytest.mark.parametrize("pos", [-1, 0, 1])
def test_bb_nan_holds_position(pos):
    s = BollingerSqueezeStrategy()
    row = _bb_row(float("nan"), squeeze_prev=True)
    assert s.signal(row, pos) == pos


# --- 5. rsi2_connors（RSI(2) 極端 + EMA200 方向閘門，順勢回調） -------------
def _rsi2_row(rsi2, close=110.0, trend_ema=100.0, sma_exit=100.0):
    return pd.Series({"rsi2": rsi2, "close": close, "trend_ema": trend_ema,
                      "sma_exit": sma_exit})


def test_rsi2_allows_short_and_registered():
    assert Rsi2ConnorsStrategy.allow_short is True
    assert STRATEGIES["rsi2_connors"] is Rsi2ConnorsStrategy


def test_rsi2_long_on_oversold_in_uptrend():
    s = Rsi2ConnorsStrategy()                     # rsi_lo=5
    assert s.signal(_rsi2_row(3.0, close=110, trend_ema=100), 0) == 1


def test_rsi2_short_on_overbought_in_downtrend():
    s = Rsi2ConnorsStrategy()                     # rsi_hi=95
    assert s.signal(_rsi2_row(97.0, close=90, trend_ema=100), 0) == -1


def test_rsi2_no_long_against_trend():
    s = Rsi2ConnorsStrategy()
    assert s.signal(_rsi2_row(3.0, close=90, trend_ema=100), 0) == 0   # 跌破 EMA200 不接刀


def test_rsi2_exit_long_above_short_mean():
    s = Rsi2ConnorsStrategy()
    assert s.signal(_rsi2_row(50.0, close=105.0, sma_exit=100.0), 1) == 0


def test_rsi2_hold_long_below_short_mean():
    s = Rsi2ConnorsStrategy()
    assert s.signal(_rsi2_row(50.0, close=98.0, sma_exit=100.0), 1) == 1


@pytest.mark.parametrize("pos", [-1, 0, 1])
def test_rsi2_nan_holds_position(pos):
    s = Rsi2ConnorsStrategy()
    assert s.signal(_rsi2_row(float("nan")), pos) == pos


# --- 註冊表擴充為 12 個策略 -------------------------------------------------
def test_strategies_registry_has_twelve_keys():
    assert set(STRATEGIES) == {
        "ema_cross", "zscore_revert", "zscore_ls", "fib_retracement",
        "supertrend", "donchian", "of_momentum",
        "vwap_band_reversion", "heikin_ashi_momo", "macd_scalp",
        "bb_squeeze_breakout", "rsi2_connors",
    }
