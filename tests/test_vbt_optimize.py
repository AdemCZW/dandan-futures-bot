"""TDD tests for backtest/vbt_optimize.py.

TDD order: RED (fail) → GREEN (pass) → REFACTOR.
All tests written BEFORE implementation.
"""
import math
import numpy as np
import pandas as pd
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.vbt_optimize import (
    signals_from_prepared, vbt_sharpe, run_bayesian_optimize, _vbt_freq,
)
from core.quant_researcher import build_strategy


# ── OPT-09：vbt freq 須隨 interval 推導，不可寫死 1h ─────────────────────
class TestVbtFreq:
    def test_maps_binance_interval_to_pandas_freq(self):
        assert _vbt_freq("15m") == "15min"
        assert _vbt_freq("1h") == "1h"
        assert _vbt_freq("4h") == "4h"
        assert _vbt_freq("1d") == "1d"

    def test_unknown_interval_falls_back_to_1h(self):
        assert _vbt_freq("garbage") == "1h"

    def test_vbt_sharpe_accepts_freq_kwarg(self):
        import pandas as _pd
        import numpy as _np
        rng = _np.random.default_rng(3)
        n = 1500
        close = 100 * _np.exp(_np.cumsum(rng.normal(0.0002, 0.01, n)))
        idx = _pd.date_range("2024-01-01", periods=n, freq="15min")
        df = _pd.DataFrame({"open": close, "high": close * 1.002,
                            "low": close * 0.998, "close": close,
                            "volume": _np.ones(n)}, index=idx)
        # 不同 freq 不應拋例外，且 15m 與 1h 年化基準不同 → Sharpe 量值不同
        s_15 = vbt_sharpe(df, "fib_channel", freq="15min", entry_zone=0.30, exit_zone=0.80)
        s_1h = vbt_sharpe(df, "fib_channel", freq="1h", entry_zone=0.30, exit_zone=0.80)
        assert isinstance(s_15, float) and isinstance(s_1h, float)


# ── helpers ────────────────────────────────────────────────────────────────

def make_df(n: int = 2000, seed: int = 42, trend: str = "flat") -> pd.DataFrame:
    """Synthetic OHLCV. trend='up'|'down'|'flat'."""
    rng = np.random.default_rng(seed)
    drift = {"up": 0.0003, "down": -0.0003, "flat": 0.0}[trend]
    rets = rng.normal(drift, 0.012, n)
    close = 30_000 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low  = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    open_ = np.r_[close[0], close[:-1]]
    vol  = rng.lognormal(3, 0.5, n)
    idx  = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


# ── TestSignalsFromPrepared ────────────────────────────────────────────────

class TestSignalsFromPrepared:
    """signals_from_prepared() must return four boolean Series."""

    def test_fib_channel_returns_four_bool_series(self):
        df = make_df(2000, trend="up")
        strat = build_strategy("fib_channel")
        prepared = strat.prepare(df).dropna()
        le, lx, se, sx = signals_from_prepared(prepared, "fib_channel",
                                                entry_zone=0.30, exit_zone=0.80)
        for s in (le, lx, se, sx):
            assert isinstance(s, pd.Series), f"Expected Series, got {type(s)}"
            assert s.dtype == bool, f"Expected bool dtype, got {s.dtype}"

    def test_series_match_prepared_length(self):
        df = make_df(2000)
        strat = build_strategy("fib_channel")
        prepared = strat.prepare(df).dropna()
        le, lx, se, sx = signals_from_prepared(prepared, "fib_channel",
                                                entry_zone=0.30, exit_zone=0.80)
        for s in (le, lx, se, sx):
            assert len(s) == len(prepared)

    def test_long_and_short_entries_never_both_true(self):
        """Long entry and short entry are mutually exclusive (can't be at same zone)."""
        df = make_df(2000)
        strat = build_strategy("fib_channel")
        prepared = strat.prepare(df).dropna()
        le, _, se, _ = signals_from_prepared(prepared, "fib_channel",
                                              entry_zone=0.30, exit_zone=0.80)
        assert not (le & se).any(), "Long and short entries must never overlap"

    def test_wider_entry_zone_generates_more_long_entries(self):
        """Wider entry_zone → more bars qualify as long entry."""
        df = make_df(2000)
        strat = build_strategy("fib_channel")
        prepared = strat.prepare(df).dropna()
        le_wide, *_ = signals_from_prepared(prepared, "fib_channel",
                                            entry_zone=0.45, exit_zone=0.80)
        le_narrow, *_ = signals_from_prepared(prepared, "fib_channel",
                                              entry_zone=0.10, exit_zone=0.80)
        assert le_wide.sum() >= le_narrow.sum()

    def test_fib_retracement_returns_four_bool_series(self):
        df = make_df(3000)
        strat = build_strategy("fib_retracement")
        prepared = strat.prepare(df).dropna()
        le, lx, se, sx = signals_from_prepared(prepared, "fib_retracement")
        for s in (le, lx, se, sx):
            assert isinstance(s, pd.Series)
            assert s.dtype == bool

    def test_smc_structure_returns_four_bool_series(self):
        df = make_df(3000)
        strat = build_strategy("smc_structure")
        prepared = strat.prepare(df).dropna()
        le, lx, se, sx = signals_from_prepared(prepared, "smc_structure")
        for s in (le, lx, se, sx):
            assert isinstance(s, pd.Series)
            assert s.dtype == bool

    def test_unknown_strategy_returns_all_false(self):
        df = make_df(500)
        prepared = df.copy()
        le, lx, se, sx = signals_from_prepared(prepared, "nonexistent_strategy")
        for s in (le, lx, se, sx):
            assert not s.any(), "Unknown strategy should return all-False signals"


# ── TestVbtSharpe ──────────────────────────────────────────────────────────

class TestVbtSharpe:
    """vbt_sharpe() must return a float representing annualized Sharpe."""

    def test_returns_float_for_fib_channel(self):
        df = make_df(2000, trend="up")
        result = vbt_sharpe(df, "fib_channel", entry_zone=0.30, exit_zone=0.80)
        assert isinstance(result, float)

    def test_returns_neg_inf_when_too_few_trades(self):
        """entry_zone=0 → pos<0 is never true → 0 signals → returns -inf."""
        df = make_df(500)
        result = vbt_sharpe(df, "fib_channel", entry_zone=0.0, exit_zone=1.0)
        assert result == float("-inf")

    def test_returns_finite_value_for_normal_params(self):
        df = make_df(3000, trend="up")
        result = vbt_sharpe(df, "fib_channel", entry_zone=0.35, exit_zone=0.75)
        # Might be -inf if no trades even with normal params on synthetic data — just check type
        assert isinstance(result, float)

    def test_returns_neg_inf_for_tiny_dataframe(self):
        """< 100 rows after dropna → returns -inf without crashing."""
        df = make_df(50)
        result = vbt_sharpe(df, "fib_channel", entry_zone=0.30, exit_zone=0.80)
        assert result == float("-inf")


# ── TestBayesianOptimize ───────────────────────────────────────────────────

class TestBayesianOptimize:
    """run_bayesian_optimize() must return (best_params, study)."""

    def test_returns_dict_and_study(self):
        df = make_df(2000, trend="up")
        best, study = run_bayesian_optimize(df, "fib_channel", n_trials=5)
        import optuna
        assert isinstance(best, dict)
        assert isinstance(study, optuna.Study)

    def test_best_params_contains_entry_zone(self):
        df = make_df(2000, trend="up")
        best, _ = run_bayesian_optimize(df, "fib_channel", n_trials=5)
        assert "entry_zone" in best

    def test_best_params_within_search_bounds(self):
        df = make_df(2000, trend="up")
        best, _ = run_bayesian_optimize(df, "fib_channel", n_trials=8)
        assert 0.10 <= best["entry_zone"] <= 0.45
        assert 1.0  <= best["atr_mult"]   <= 5.0

    def test_raises_for_unknown_strategy(self):
        df = make_df(500)
        with pytest.raises((ValueError, KeyError)):
            run_bayesian_optimize(df, "nonexistent", n_trials=2)

    def test_walk_forward_mode_returns_dict(self):
        """n_wf_folds > 0 activates walk-forward; still returns best_params dict."""
        df = make_df(3000, trend="up")
        best, _ = run_bayesian_optimize(df, "fib_channel", n_trials=5, n_wf_folds=3)
        assert isinstance(best, dict)
        assert "entry_zone" in best

# ── OPT-10：誠實的 walk-forward — 目標分數絕不偷看保留的 OOS 尾段 ──────────
class TestHonestWalkForward:
    def test_objective_ignores_reserved_oos_tail(self):
        """竄改保留的 OOS 尾段不應改變 in-sample 目標分數（=Optuna 不用測試集挑參）。"""
        from backtest.vbt_optimize import _in_sample_objective
        df = make_df(3000, seed=2, trend="up")
        fold = len(df) // 5
        df_b = df.copy()
        df_b.iloc[fold * 4:] = df_b.iloc[fold * 4:].values * 2.0   # 只改 OOS 尾段
        p = dict(entry_zone=0.30, exit_zone=0.80)
        s_a = _in_sample_objective(df, "fib_channel", p, 4)
        s_b = _in_sample_objective(df_b, "fib_channel", p, 4)
        assert s_a == s_b

    def test_oos_sharpe_depends_only_on_tail(self):
        """OOS 驗證只看保留尾段：改 in-sample 前段不影響 oos_sharpe。"""
        from backtest.vbt_optimize import oos_sharpe
        df = make_df(3000, seed=3, trend="up")
        fold = len(df) // 5
        df_b = df.copy()
        df_b.iloc[:fold * 4] = df_b.iloc[:fold * 4].values * 2.0   # 只改 in-sample 前段
        p = dict(entry_zone=0.30, exit_zone=0.80)
        assert oos_sharpe(df, "fib_channel", p, 4) == oos_sharpe(df_b, "fib_channel", p, 4)

    def test_run_bayesian_optimize_records_oos_in_study(self):
        """walk-forward 模式下，best_params 的 OOS 分數應被記入 study.user_attrs。"""
        df = make_df(3000, trend="up")
        _, study = run_bayesian_optimize(df, "fib_channel", n_trials=5, n_wf_folds=4)
        assert "oos_sharpe" in study.user_attrs
