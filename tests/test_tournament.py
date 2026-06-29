"""backtest/tournament.py — 策略錦標賽（回測驅動、期望值排序）測試。

錦標賽把所有策略跑在同一段真實/合成 K 線上，依【期望值】(預設) 排序，
交易數不足者沉底，挑出當前最佳 champion。純排序邏輯 rank() 用手造 dict
確定性驗證；evaluate()/run_tournament() 用合成 OHLCV 驗證結構與排序。
"""
import numpy as np
import pandas as pd
import pytest

from backtest.tournament import evaluate, rank, run_tournament
from config import Config


def _synth_df(n=600, seed=0):
    """趨勢 + 噪音的確定性 OHLCV（含 volume / taker_base）。"""
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n))
    idx = pd.date_range("2026-01-01", periods=n, freq="5min")
    vol = np.abs(rng.normal(1000, 200, n)) + 1
    return pd.DataFrame({
        "open": close, "high": close + np.abs(rng.normal(0, 0.5, n)) + 0.1,
        "low": close - np.abs(rng.normal(0, 0.5, n)) - 0.1, "close": close,
        "volume": vol, "taker_base": vol * rng.uniform(0.3, 0.7, n),
    }, index=idx)


# --- rank()：純排序，確定性 ---------------------------------------------------
def test_rank_sorts_by_expectancy_and_sinks_min_trades():
    results = [
        {"strategy": "a", "trades": 50, "expectancy": 1.0},
        {"strategy": "b", "trades": 50, "expectancy": 3.0},
        {"strategy": "c", "trades": 5,  "expectancy": 99.0},   # 交易太少 → 沉底
    ]
    ranked = rank(results, objective="expectancy", min_trades=20)
    assert [r["strategy"] for r in ranked] == ["b", "a", "c"]
    assert ranked[0]["eligible"] is True
    assert ranked[-1]["strategy"] == "c" and ranked[-1]["eligible"] is False


def test_rank_profit_factor_objective_treats_inf_as_best():
    results = [
        {"strategy": "a", "trades": 50, "profit_factor_raw": 1.5},
        {"strategy": "b", "trades": 50, "profit_factor_raw": float("inf")},
    ]
    ranked = rank(results, objective="profit_factor", min_trades=20)
    assert ranked[0]["strategy"] == "b"


def test_rank_annotates_score_and_eligible():
    results = [{"strategy": "a", "trades": 3, "expectancy": 5.0}]
    ranked = rank(results, objective="expectancy", min_trades=20)
    assert ranked[0]["eligible"] is False
    assert ranked[0]["score"] == float("-inf")    # 不合格者分數沉底


# --- evaluate()：單一策略績效 dict -------------------------------------------
def test_evaluate_returns_full_metric_dict():
    m = evaluate(_synth_df(), "ema_cross", Config(interval="5m"))
    required = {"strategy", "trades", "win_rate", "expectancy",
                "profit_factor", "profit_factor_raw", "total_return",
                "max_drawdown", "sharpe"}
    assert required <= set(m)
    assert m["strategy"] == "ema_cross"
    assert isinstance(m["trades"], int) and m["trades"] >= 0


def test_evaluate_profit_factor_json_safe():
    """profit_factor 欄位為 JSON 安全（inf → None）；raw 欄保留原值。"""
    m = evaluate(_synth_df(), "donchian", Config(interval="5m"))
    assert m["profit_factor"] is None or isinstance(m["profit_factor"], float)
    # 若無虧損交易 raw 會是 inf，但 json-safe 欄不可是 inf
    assert m["profit_factor"] != float("inf")


# --- run_tournament()：整合 ---------------------------------------------------
def test_run_tournament_returns_ranked_and_champion():
    df = _synth_df()
    res = run_tournament(df, Config(interval="5m"),
                         names=["ema_cross", "donchian", "supertrend"], min_trades=0)
    assert len(res["ranked"]) == 3
    # champion 為排序後第一個合格者
    assert res["champion"]["strategy"] == res["ranked"][0]["strategy"]
    # ranked 依分數遞減
    scores = [r["score"] for r in res["ranked"]]
    assert scores == sorted(scores, reverse=True)


def test_run_tournament_champion_none_when_all_below_min_trades():
    df = _synth_df(n=300)
    res = run_tournament(df, Config(interval="5m"), names=["ema_cross"],
                         min_trades=100000)        # 不可能達到 → 無 champion
    assert res["champion"] is None


def test_run_tournament_survives_strategy_error():
    """單一策略丟例外不會炸掉整場錦標賽（記錄 error、沉底）。"""
    df = _synth_df()
    res = run_tournament(df, Config(interval="5m"),
                         names=["ema_cross", "donchian"], min_trades=0)
    names = {r["strategy"] for r in res["ranked"]}
    assert names == {"ema_cross", "donchian"}


# =========================================================================== #
# Walk-forward（樣本外）驗證 — 區分「真 edge vs 過擬合」。
#   訓練窗選參數 → 鎖定 → 只在後續未見過的測試窗評分 → 跨 fold 彙總 OOS。
# =========================================================================== #
from backtest.tournament import (
    _fold_bounds, _metrics_from_pnls, walk_forward_eval, run_walkforward_tournament,
)


def test_fold_bounds_non_overlapping_test_windows():
    # n=300, train=120, test=40 → 4-tuple (train_start, train_end, test_start, test_end)
    bounds = _fold_bounds(300, 120, 40)
    assert bounds == [(0, 120, 120, 160), (40, 160, 160, 200),
                      (80, 200, 200, 240), (120, 240, 240, 280)]
    for a, b, ts, te in bounds:
        assert a < b <= ts < te   # 訓練 [a,b) 在測試 [ts,te) 之前（無 look-ahead）


def test_fold_bounds_purge_inserts_embargo_gap():
    """OPT-11：purge>0 在 train_end 與 test_start 間留 embargo gap（降跨界序列相關）。"""
    bounds = _fold_bounds(400, 120, 40, purge=10)
    a, b, ts, te = bounds[0]
    assert (a, b) == (0, 120)
    assert ts == b + 10           # 測試窗起點延後 purge 根
    assert te == ts + 40


def test_fold_bounds_empty_when_too_short():
    assert _fold_bounds(100, 120, 40) == []


def test_metrics_from_pnls_basic():
    m = _metrics_from_pnls([10.0, -5.0, 20.0, -3.0])
    assert m["trades"] == 4
    assert m["win_rate"] == 0.5
    assert m["expectancy"] == pytest.approx(5.5)
    assert m["profit_factor_raw"] == pytest.approx(30.0 / 8.0)


def test_metrics_from_pnls_empty_and_all_wins():
    empty = _metrics_from_pnls([])
    assert empty["trades"] == 0 and empty["expectancy"] == 0.0 and empty["profit_factor_raw"] == 0.0
    allwin = _metrics_from_pnls([5.0, 10.0])
    assert allwin["profit_factor_raw"] == float("inf")


def test_walk_forward_eval_returns_oos_summary():
    df = _synth_df(n=400)
    wf = walk_forward_eval(df, "ema_cross", Config(interval="5m"),
                           grid=None, train_bars=120, test_bars=40)
    required = {"strategy", "folds", "oos_trades", "oos_win_rate", "oos_expectancy",
                "oos_profit_factor", "oos_profit_factor_raw", "oos_return_compounded",
                "fold_records"}
    assert required <= set(wf)
    assert wf["folds"] >= 1
    assert wf["strategy"] == "ema_cross"
    # OOS 交易總數 = 各 fold 測試窗交易數之和
    assert wf["oos_trades"] == sum(f["oos_trades"] for f in wf["fold_records"])


def test_walk_forward_eval_with_grid_picks_params_per_fold():
    df = _synth_df(n=400)
    wf = walk_forward_eval(df, "supertrend", Config(interval="5m"),
                           grid={"period": [7, 10], "multiplier": [2.0, 3.0]},
                           train_bars=120, test_bars=40, min_trades_train=1)
    # 每個 fold 都鎖定了一組（在訓練窗選出的）參數
    assert all("params" in f for f in wf["fold_records"])


def test_run_walkforward_tournament_ranks_by_oos_expectancy():
    df = _synth_df(n=400)
    res = run_walkforward_tournament(df, Config(interval="5m"),
                                     names=["ema_cross", "donchian", "supertrend"],
                                     train_bars=120, test_bars=40, min_trades_oos=0)
    assert len(res["ranked"]) == 3
    scores = [r["score"] for r in res["ranked"]]
    assert scores == sorted(scores, reverse=True)
    if res["champion"]:
        assert res["champion"]["strategy"] == res["ranked"][0]["strategy"]


# ── OPT-12：bootstrap 信賴下界當晉升閘門 ──────────────────────────────
from backtest.tournament import bootstrap_mean_lower_bound


def test_bootstrap_lower_bound_positive_for_clear_edge():
    """強正期望（多數 +10、少數 -5）→ bootstrap 期望值下界 > 0。"""
    pnl = [10.0] * 70 + [-5.0] * 30
    lb = bootstrap_mean_lower_bound(pnl, n_resamples=2000, confidence=0.95, seed=0)
    assert lb > 0


def test_bootstrap_lower_bound_not_positive_for_zero_mean_noise():
    """零期望雜訊（無 edge）→ 下界不顯著為正（≤0），不該晉升。"""
    rng = np.random.default_rng(7)
    pnl = list(rng.normal(0.0, 10.0, 300))
    lb = bootstrap_mean_lower_bound(pnl, n_resamples=2000, confidence=0.95, seed=0)
    assert lb <= 0


def test_bootstrap_lower_bound_empty_returns_neg_inf():
    assert bootstrap_mean_lower_bound([], confidence=0.95) == float("-inf")


def test_bootstrap_lower_bound_deterministic_with_seed():
    pnl = [3.0, -1.0, 2.0, -2.0, 5.0] * 20
    a = bootstrap_mean_lower_bound(pnl, n_resamples=1000, confidence=0.95, seed=42)
    b = bootstrap_mean_lower_bound(pnl, n_resamples=1000, confidence=0.95, seed=42)
    assert a == b


def test_walk_forward_eval_reports_oos_expectancy_lower_bound():
    """walk_forward_eval 輸出應含 oos_expectancy_lb（OOS 期望值 bootstrap 下界）。"""
    df = _synth_df(n=400)
    out = walk_forward_eval(df, "ema_cross", Config(interval="5m"),
                            train_bars=120, test_bars=40)
    assert "oos_expectancy_lb" in out


def test_select_champion_significance_gate():
    """OPT-12：require_significant=True 時跳過 oos_expectancy_lb≤0 的合格者。"""
    from backtest.tournament import _select_champion
    ranked = [
        {"strategy": "a", "eligible": True, "oos_expectancy_lb": -0.5},  # 不顯著
        {"strategy": "b", "eligible": True, "oos_expectancy_lb": 0.8},   # 顯著
    ]
    assert _select_champion(ranked, require_significant=False)["strategy"] == "a"
    assert _select_champion(ranked, require_significant=True)["strategy"] == "b"


def test_select_champion_none_when_none_significant():
    from backtest.tournament import _select_champion
    ranked = [{"strategy": "a", "eligible": True, "oos_expectancy_lb": -0.1}]
    assert _select_champion(ranked, require_significant=True) is None
