"""backtest/optimize.py 的單元測試。

覆蓋 param_grid / _split_params / sweep / walk_forward 的「應有行為」：
  - param_grid：組合數 = 各維長度乘積，且確為笛卡爾積。
  - _split_params：RISK_KEYS 歸風控、其餘歸策略。
  - sweep：回傳表依 score 由大到小排序、含參數欄與 sharpe/score 等欄；
           space 含風控參數時該欄出現在表中。
  - walk_forward：各 fold test 段不重疊且位於 train 段之後、產出 p_ 欄、
           連跑兩次結果相同（每組合 fresh RiskOfficer、無狀態殘留）；
           min_trades 設極大時全 fold 被跳過 → 回傳空表。

所有資料皆為確定性合成（make_synthetic 固定 seed），不做任何外部 IO。
"""
from __future__ import annotations

import math
from functools import reduce

import pandas as pd
import pytest

import run_optimize
from config import Config
from core.risk_officer import RiskOfficer
from backtest.optimize import (
    RISK_KEYS,
    param_grid,
    _split_params,
    sweep,
    walk_forward,
)


# ── 共用 fixtures ─────────────────────────────────────────────

@pytest.fixture
def df():
    """小份確定性合成 K 線（固定 seed），足夠 ema_cross 切出多筆交易。"""
    return run_optimize.make_synthetic(n=1500, seed=7)


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def risk(cfg):
    return RiskOfficer(cfg)


@pytest.fixture
def strat_space():
    """純策略網格（不含風控參數）。"""
    return {"fast": [8, 12], "slow": [26, 34], "rsi_max": [70, 75]}


# ── param_grid ────────────────────────────────────────────────

def test_param_grid_count_is_product_of_dim_lengths():
    space = {"a": [1, 2, 3], "b": [10, 20], "c": [100, 200, 300, 400]}
    grid = param_grid(space)
    expected = reduce(lambda x, y: x * y, (len(v) for v in space.values()))
    assert len(grid) == expected == 3 * 2 * 4


def test_param_grid_is_cartesian_product():
    space = {"a": [1, 2], "b": ["x", "y"]}
    grid = param_grid(space)
    # 每個組合都是完整字典、key 與 space 對齊
    assert all(set(d) == {"a", "b"} for d in grid)
    got = {(d["a"], d["b"]) for d in grid}
    assert got == {(1, "x"), (1, "y"), (2, "x"), (2, "y")}
    # 無重複組合
    assert len(grid) == len(got)


def test_param_grid_single_value_dims():
    space = {"a": [1], "b": [2], "c": [3, 4, 5]}
    assert len(param_grid(space)) == 3


# ── _split_params ─────────────────────────────────────────────

def test_split_params_routes_risk_and_strategy_keys():
    params = {"fast": 12, "slow": 26, "stop_loss_pct": 0.02, "take_profit_pct": 0.04}
    strat_p, risk_p = _split_params(params)
    assert strat_p == {"fast": 12, "slow": 26}
    assert risk_p == {"stop_loss_pct": 0.02, "take_profit_pct": 0.04}


def test_split_params_all_risk_keys_classified():
    # 每個 RISK_KEYS 都應被歸到風控側、不留在策略側
    params = {k: 0.123 for k in RISK_KEYS}
    params["window"] = 50  # 一個策略參數
    strat_p, risk_p = _split_params(params)
    assert set(risk_p) == set(RISK_KEYS)
    assert strat_p == {"window": 50}


def test_split_params_no_risk_keys():
    params = {"fast": 8, "slow": 34}
    strat_p, risk_p = _split_params(params)
    assert strat_p == params
    assert risk_p == {}


# ── sweep ─────────────────────────────────────────────────────

def test_sweep_has_param_and_metric_columns(df, cfg, risk, strat_space):
    table = sweep(df, "ema_cross", strat_space, risk, cfg,
                  objective="sharpe", min_trades=1)
    # 參數欄
    for k in strat_space:
        assert k in table.columns
    # 指標欄
    for col in ("trades", "total_return", "max_drawdown", "win_rate",
                "sharpe", "score"):
        assert col in table.columns
    # 每個組合一列
    assert len(table) == len(param_grid(strat_space))


def test_sweep_sorted_by_score_descending(df, cfg, risk, strat_space):
    table = sweep(df, "ema_cross", strat_space, risk, cfg,
                  objective="sharpe", min_trades=1)
    scores = table["score"].tolist()
    assert scores == sorted(scores, reverse=True)
    # index 已 reset（0..n-1）
    assert list(table.index) == list(range(len(table)))


def test_sweep_default_objective_is_sharpe(df, cfg, risk, strat_space):
    # objective 預設 sharpe：有效列的 score 應等於 sharpe
    table = sweep(df, "ema_cross", strat_space, risk, cfg, min_trades=1)
    finite = table[table["score"].apply(math.isfinite)]
    assert len(finite) > 0
    assert finite["score"].round(10).tolist() == finite["sharpe"].round(10).tolist()


def test_sweep_min_trades_filters_to_neg_inf(df, cfg, risk, strat_space):
    # min_trades 設極大 → 全部 score = -inf（交易筆數一定不足）
    table = sweep(df, "ema_cross", strat_space, risk, cfg,
                  objective="sharpe", min_trades=10 ** 9)
    assert (table["score"] == float("-inf")).all()


def test_sweep_includes_risk_param_column(df, cfg, risk, strat_space):
    # space 含風控參數時，該欄應出現在結果表中
    space = {**strat_space, "stop_loss_pct": [0.02, 0.03]}
    table = sweep(df, "ema_cross", space, risk, cfg,
                  objective="sharpe", min_trades=1)
    assert "stop_loss_pct" in table.columns
    # 兩個風控值都應出現
    assert set(table["stop_loss_pct"].unique()) == {0.02, 0.03}
    assert len(table) == len(param_grid(space))


def test_sweep_deterministic(df, cfg, risk, strat_space):
    t1 = sweep(df, "ema_cross", strat_space, risk, cfg, min_trades=1)
    t2 = sweep(df, "ema_cross", strat_space, risk, cfg, min_trades=1)
    pd.testing.assert_frame_equal(t1, t2)


def test_sweep_atr_risk_params_routed(df, cfg, risk, strat_space):
    """ATR 停損 / R 停利倍數納入網格：欄位出現、各值齊備、組合數正確（確認路由到風控 cfg）。"""
    space = {**strat_space, "atr_mult_sl": [1.5, 2.5], "tp_R_mult": [2.0, 3.0]}
    table = sweep(df, "ema_cross", space, risk, cfg, objective="sharpe", min_trades=1)
    assert "atr_mult_sl" in table.columns and "tp_R_mult" in table.columns
    assert set(table["atr_mult_sl"].unique()) == {1.5, 2.5}
    assert set(table["tp_R_mult"].unique()) == {2.0, 3.0}
    assert len(table) == len(param_grid(space))


def test_walk_forward_runs_with_atr_risk_params(df, cfg, risk, strat_space):
    """含 ATR 風控參數的網格仍能跑 walk-forward，OOS 欄位齊全。"""
    space = {**strat_space, "atr_mult_sl": [1.5, 2.5]}
    wf = walk_forward(df, "ema_cross", space, risk, cfg,
                      objective="sharpe", min_trades=1, train_bars=500, test_bars=300)
    assert not wf.empty
    assert "p_atr_mult_sl" in wf.columns
    for col in ("OOS_return", "OOS_sharpe", "OOS_maxDD", "OOS_trades"):
        assert col in wf.columns


# ── walk_forward ──────────────────────────────────────────────

def _train_test_step():
    """測試共用的 fold 切窗參數。"""
    return dict(train_bars=500, test_bars=300)


def test_walk_forward_produces_p_columns(df, cfg, risk, strat_space):
    wf = walk_forward(df, "ema_cross", strat_space, risk, cfg,
                      objective="sharpe", min_trades=1, **_train_test_step())
    assert not wf.empty
    for k in strat_space:
        assert f"p_{k}" in wf.columns
    # 基本欄位齊備
    for col in ("fold", "test_start", "test_end", "IS_return",
                "OOS_return", "OOS_sharpe", "OOS_maxDD", "OOS_trades"):
        assert col in wf.columns


def test_walk_forward_test_after_train_and_non_overlapping(df, cfg, risk, strat_space):
    tt = _train_test_step()
    wf = walk_forward(df, "ema_cross", strat_space, risk, cfg,
                      objective="sharpe", min_trades=1, **tt)
    assert len(wf) >= 2

    # 各 fold 的 test 段起點，必須晚於該 fold train 段的最後一根。
    # fold f 的 start = f * test_bars；train = [start, start+train_bars)，
    # test = [start+train_bars, start+train_bars+test_bars)。
    starts = [0]  # 第 0 個 fold 的視窗起點（min_trades=1 不會跳過）
    for i in range(len(wf)):
        start = i * tt["test_bars"]
        train_seg = df.iloc[start: start + tt["train_bars"]]
        test_start = wf["test_start"].iloc[i]
        # test 段起點晚於 train 段最後一根時間戳
        assert test_start > train_seg.index[-1]

    # 各 fold test 段彼此不重疊且時間遞增：
    # test_start 嚴格遞增，且前一 fold 的 test_end < 後一 fold 的 test_start。
    test_starts = wf["test_start"].tolist()
    test_ends = wf["test_end"].tolist()
    assert test_starts == sorted(test_starts)
    assert all(s1 < s2 for s1, s2 in zip(test_starts, test_starts[1:]))
    for end_prev, start_next in zip(test_ends, test_starts[1:]):
        assert end_prev < start_next


def test_walk_forward_deterministic_no_state_leak(df, cfg, risk, strat_space):
    # 連跑兩次結果完全相同：每組合用 fresh RiskOfficer，無熔斷狀態殘留。
    tt = _train_test_step()
    wf1 = walk_forward(df, "ema_cross", strat_space, risk, cfg,
                       objective="sharpe", min_trades=1, **tt)
    wf2 = walk_forward(df, "ema_cross", strat_space, risk, cfg,
                       objective="sharpe", min_trades=1, **tt)
    pd.testing.assert_frame_equal(wf1, wf2)


def test_walk_forward_includes_risk_p_column(df, cfg, risk, strat_space):
    space = {**strat_space, "stop_loss_pct": [0.02, 0.03]}
    wf = walk_forward(df, "ema_cross", space, risk, cfg,
                      objective="sharpe", min_trades=1, **_train_test_step())
    assert not wf.empty
    assert "p_stop_loss_pct" in wf.columns


def test_walk_forward_empty_when_min_trades_huge(df, cfg, risk, strat_space):
    # min_trades 設極大 → 每 fold 的訓練段最佳 score = -inf → 全 fold 被跳過 → 空表
    wf = walk_forward(df, "ema_cross", strat_space, risk, cfg,
                      objective="sharpe", min_trades=10 ** 9, **_train_test_step())
    assert wf.empty
    assert len(wf) == 0


def test_walk_forward_empty_when_data_too_short(cfg, risk, strat_space):
    # 資料長度不足一個 fold（train+test）→ 切不出 fold → 空表
    short = run_optimize.make_synthetic(n=400, seed=7)
    wf = walk_forward(short, "ema_cross", strat_space, risk, cfg,
                      objective="sharpe", min_trades=1,
                      train_bars=500, test_bars=300)
    assert wf.empty
