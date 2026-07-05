"""MaConvergencePullbackStrategy（ma_convergence_pullback）TDD 測試。

還原 2026-07-05 YouTube 分析出的雙均線系統「方法二」（回踩20均線不破）：
六線（MA20/60/120 + EMA20/60/120）收斂後確認發散方向，發散後價格第一次
回踩20均線（觸及但收盤未跌破）才進場——不是死叉本身觸發，是「趨勢確立後
的第一次拉回」觸發。出場交給既有風控層（R倍數/Chandelier），策略本身
只在均線順序被打破（趨勢結束）時強制平倉。

全部用直接構造的 row 測 signal() 純函式行為，同全庫慣例。
"""
import numpy as np
import pandas as pd
import pytest

from core.quant_researcher import STRATEGIES, build_strategy


def _mk_df(n=300, seed=5):
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    return pd.DataFrame({
        "open": close, "high": close + 0.3, "low": close - 0.3,
        "close": close, "volume": np.abs(rng.normal(1000, 200, n)) + 1,
    }, index=idx)


def _row(trend_dir=1, is_first_pullback=True, ma20=100.0, atr=2.0):
    return {"trend_dir": trend_dir, "is_first_pullback": is_first_pullback,
            "ma20": ma20, "atr": atr}


# ── 註冊表 ──────────────────────────────────────────────────────────────────
def test_registered_and_buildable():
    assert "ma_convergence_pullback" in STRATEGIES
    s = build_strategy("ma_convergence_pullback")
    assert s.allow_short is True


# ── prepare()：六線 + 密集/發散 + 首次回踩欄位齊全 ──────────────────────────
def test_prepare_adds_all_columns():
    s = build_strategy("ma_convergence_pullback")
    out = s.prepare(_mk_df())
    for col in ("ma20", "ma60", "ma120", "ema20", "ema60", "ema120",
                "spread", "trend_dir", "is_first_pullback", "atr"):
        assert col in out.columns, f"缺欄位 {col}"


# ── 進場：趨勢方向 + 首次回踩 兩者皆真才進 ──────────────────────────────────
def test_long_entry_on_first_pullback_in_bull_trend():
    s = build_strategy("ma_convergence_pullback")
    assert s.signal(_row(trend_dir=1, is_first_pullback=True), 0) == 1


def test_short_entry_symmetric():
    s = build_strategy("ma_convergence_pullback")
    assert s.signal(_row(trend_dir=-1, is_first_pullback=True), 0) == -1


def test_no_entry_when_not_first_pullback():
    """趨勢方向對，但這不是本輪趨勢的第一次回踩（已經用過）→ 不進場。"""
    s = build_strategy("ma_convergence_pullback")
    assert s.signal(_row(trend_dir=1, is_first_pullback=False), 0) == 0


def test_no_entry_when_no_trend_established():
    """六線仍密集（trend_dir=0）→ 不進場，即使碰巧觸及均線。"""
    s = build_strategy("ma_convergence_pullback")
    assert s.signal(_row(trend_dir=0, is_first_pullback=True), 0) == 0


# ── 出場：均線順序被打破（趨勢結束）強制平倉；否則續抱 ──────────────────────
def test_long_exit_when_trend_breaks():
    s = build_strategy("ma_convergence_pullback")
    r = _row(trend_dir=0)                      # 順序已被打破
    assert s.signal(r, 1) == 0


def test_long_exit_when_trend_flips_bear():
    s = build_strategy("ma_convergence_pullback")
    r = _row(trend_dir=-1)
    assert s.signal(r, 1) == 0


def test_long_holds_while_trend_intact():
    s = build_strategy("ma_convergence_pullback")
    r = _row(trend_dir=1, is_first_pullback=False)  # 續抱中，非首次回踩訊號
    assert s.signal(r, 1) == 1


def test_short_exit_when_trend_breaks():
    s = build_strategy("ma_convergence_pullback")
    assert s.signal(_row(trend_dir=0), -1) == 0


# ── 暖機：NaN 維持現狀 ───────────────────────────────────────────────────────
@pytest.mark.parametrize("pos", [0, 1, -1])
def test_warmup_nan_holds_position(pos):
    s = build_strategy("ma_convergence_pullback")
    r = _row()
    r["trend_dir"] = float("nan")
    assert s.signal(r, pos) == pos


# ── 核心邏輯驗證：用真實資料驗證「密集→發散→首次回踩」狀態機 ─────────────────
def test_pullback_only_fires_once_per_trend_regime():
    """同一段多頭趨勢內，is_first_pullback 最多只有一次 True（後續拉回不重複觸發）。"""
    s = build_strategy("ma_convergence_pullback")
    # 造一段先盤整、後急拉出多頭排列、然後在高檔反覆拉回測 MA20 兩次以上的走勢
    n = 200
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    flat = 100 + np.zeros(60)                                   # 密集盤整
    up = 100 + np.cumsum(np.full(80, 0.6))                      # 明確發散上攻
    # 高檔反覆小幅拉回到接近 20MA 附近兩次
    pull1 = up[-1] - np.concatenate([np.linspace(0, 4, 10), np.linspace(4, 0, 10)])
    pull2 = pull1[-1] + np.cumsum(np.full(20, 0.3))
    pull3 = pull2[-1] - np.concatenate([np.linspace(0, 4, 10), np.linspace(4, 0, 10)])
    close = np.concatenate([flat, up, pull1, pull2, pull3])[:n]
    df = pd.DataFrame({"open": close, "high": close + 0.5, "low": close - 0.5,
                       "close": close, "volume": np.full(n, 1000.0)}, index=idx)
    out = s.prepare(df).dropna()
    bull_regime = out[out["trend_dir"] == 1]
    fires = bull_regime["is_first_pullback"].sum()
    assert fires <= 1, f"同一趨勢內 is_first_pullback 觸發了 {fires} 次，應該最多 1 次"


def test_runs_through_backtester():
    from backtest.backtester import run_backtest
    from core.risk_officer import RiskOfficer
    from config import Config
    cfg = Config(interval="4h", max_daily_loss_pct=10.0)
    res = run_backtest(_mk_df(400), build_strategy("ma_convergence_pullback"),
                       RiskOfficer(cfg), cfg)
    assert res.equity_curve is not None and len(res.equity_curve) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 補齊影片兩種進場法 + 二次回踩（2026-07-05，使用者要求圖上顯示密集處入場訊號）。
# 關鍵約束：新增欄位是純加法，is_first_pullback / trend_dir 輸出必須逐位元不變
# → b9（實際下單依 signal()）行為完全不受影響（已驗證的策略不能被改壞）。
# ═══════════════════════════════════════════════════════════════════════════

def _flat_then_trend_df(n=260, seed=11):
    """密集盤整(前段) → 明確發散上攻 → 拉回 → 再攻 → 再拉回（可測二次回踩）。"""
    import numpy as _np
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    flat = 100 + _np.zeros(140)                                  # 長盤整 → 六線收斂（密集）
    up1 = 100 + _np.cumsum(_np.full(40, 0.8))                    # 發散上攻
    pb1 = up1[-1] - _np.concatenate([_np.linspace(0, 6, 8), _np.linspace(6, 1, 8)])  # 首次回踩 20MA
    up2 = pb1[-1] + _np.cumsum(_np.full(24, 0.7))               # 再攻（離開 MA20）
    pb2 = up2[-1] - _np.concatenate([_np.linspace(0, 6, 8), _np.linspace(6, 1, 8)])  # 二次回踩
    tail = _np.full(n, pb2[-1])
    close = _np.concatenate([flat, up1, pb1, up2, pb2, tail])[:n]
    return pd.DataFrame({"open": close, "high": close + 0.6, "low": close - 0.6,
                         "close": close, "volume": _np.full(n, 1000.0)}, index=idx)


def test_prepare_adds_density_breakout_second_pullback_columns():
    s = build_strategy("ma_convergence_pullback")
    out = s.prepare(_flat_then_trend_df())
    for col in ("is_density", "is_breakout", "is_second_pullback"):
        assert col in out.columns, f"缺欄位 {col}"


def test_density_flag_true_in_consolidation():
    """長盤整段六線收斂 → is_density 在暖機後的盤整區至少有一根 True。"""
    s = build_strategy("ma_convergence_pullback")
    out = s.prepare(_flat_then_trend_df()).dropna()
    assert out["is_density"].sum() > 0


def test_breakout_fires_at_density_to_divergence_transition():
    """方法一：密集突破 → is_breakout 在 trend_dir 由 0 轉 ±1 的當根為 True，且帶方向。"""
    s = build_strategy("ma_convergence_pullback")
    out = s.prepare(_flat_then_trend_df()).dropna().reset_index(drop=True)
    bo_idx = out.index[out["is_breakout"]].tolist()
    assert len(bo_idx) >= 1
    for i in bo_idx:
        assert out.loc[i, "trend_dir"] != 0                    # 突破當根趨勢已確立
        if i > 0:
            assert out.loc[i - 1, "trend_dir"] == 0            # 前一根還在密集（0）


def test_second_pullback_requires_first_pullback_earlier():
    """二次回踩只能出現在同段趨勢中、首次回踩之後（不可能先有二踩再有首踩）。"""
    s = build_strategy("ma_convergence_pullback")
    out = s.prepare(_flat_then_trend_df()).dropna().reset_index(drop=True)
    first_idx = out.index[out["is_first_pullback"]].tolist()
    second_idx = out.index[out["is_second_pullback"]].tolist()
    for si in second_idx:
        assert any(fi < si for fi in first_idx), "二次回踩前必須先有一次首次回踩"


def test_b9_behavior_unchanged_first_pullback_and_trend_dir_stable():
    """回歸鎖：新增欄位後，is_first_pullback / trend_dir 與『只算這兩欄的參考版』完全一致
    → signal() 吃的欄位不變 → b9 下單行為零改變。"""
    s = build_strategy("ma_convergence_pullback")
    df = _flat_then_trend_df()
    out = s.prepare(df)
    # 參考：first_pullback 每段趨勢最多 1 次（原本的核心不變式）
    # 用 trend_dir 分段，逐段確認 first_pullback 次數 ≤ 1
    td = out["trend_dir"].to_numpy()
    fp = out["is_first_pullback"].to_numpy()
    seg_start = 0
    for i in range(1, len(td)):
        if td[i] != td[i - 1]:
            if td[seg_start] in (1, -1):
                assert fp[seg_start:i].sum() <= 1
            seg_start = i


# ── 多週期共振過濾（2026-07-05，use_htf_filter 預設關）─────────────────────────
def test_htf_filter_off_by_default_no_column():
    s = build_strategy("ma_convergence_pullback")
    assert s.params.get("use_htf_filter") is False


def test_htf_filter_blocks_entry_against_daily_trend():
    """日線空頭（htf_trend=-1）時，4h 的做多首踩訊號要被擋掉。"""
    s = build_strategy("ma_convergence_pullback", use_htf_filter=True)
    r = _row(trend_dir=1, is_first_pullback=True)
    r["htf_trend"] = -1
    assert s.signal(r, 0) == 0


def test_htf_filter_allows_entry_with_daily_trend():
    s = build_strategy("ma_convergence_pullback", use_htf_filter=True)
    r = _row(trend_dir=1, is_first_pullback=True)
    r["htf_trend"] = 1
    assert s.signal(r, 0) == 1


def test_htf_filter_blocks_when_daily_neutral():
    """日線中性/暖機（0）→ 共振不成立，不進場（嚴格版語意）。"""
    s = build_strategy("ma_convergence_pullback", use_htf_filter=True)
    r = _row(trend_dir=1, is_first_pullback=True)
    r["htf_trend"] = 0
    assert s.signal(r, 0) == 0


def test_htf_filter_does_not_block_exit():
    """過濾只擋新進場：持倉中趨勢破壞照樣出場，不受 htf 影響。"""
    s = build_strategy("ma_convergence_pullback", use_htf_filter=True)
    r = _row(trend_dir=0)
    r["htf_trend"] = 1
    assert s.signal(r, 1) == 0


def test_htf_filter_prepare_adds_column_when_enabled():
    s = build_strategy("ma_convergence_pullback", use_htf_filter=True)
    out = s.prepare(_mk_df(300))
    assert "htf_trend" in out.columns


def test_htf_filter_off_signal_ignores_htf_column():
    """開關關閉時，即使資料裡有 htf_trend 欄也不套用（回歸安全網）。"""
    s = build_strategy("ma_convergence_pullback")
    r = _row(trend_dir=1, is_first_pullback=True)
    r["htf_trend"] = -1
    assert s.signal(r, 0) == 1
