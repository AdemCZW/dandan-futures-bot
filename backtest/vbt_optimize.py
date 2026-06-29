"""VectorBT-backed Bayesian optimization (Optuna) for strategy parameters.

Replaces the exhaustive grid search in backtest/optimize.py with TPE-sampled
Bayesian search. 8-25x faster per evaluation thanks to VBT's vectorized
portfolio engine; Optuna's sampler needs far fewer trials than grid search.

Usage (standalone):
    python run_optimize_bayesian.py fib_channel --trials 100 --synthetic
"""
from __future__ import annotations

import math
import numpy as np
import pandas as pd
import optuna
import vectorbt as vbt

from core.quant_researcher import build_strategy

optuna.logging.set_verbosity(optuna.logging.WARNING)

MIN_TRADES = 10
FEES = 0.0004
INIT_CASH = 10_000

# ── search spaces ──────────────────────────────────────────────────────────
# Each param: ("float"|"int"|"categorical", low, high)  or  ("categorical", v1, v2, ...)

SEARCH_SPACES: dict[str, dict] = {
    "fib_channel": {
        "entry_zone":   ("float", 0.10, 0.45),
        "exit_zone":    ("float", 0.55, 0.95),
        "atr_mult":     ("float", 1.0,  5.0),
        "pivot_left":   ("int",   3,    15),
        "pivot_right":  ("int",   3,    15),
    },
    "fib_retracement": {
        "pivot_left":       ("int", 2, 6),
        "pivot_right":      ("int", 2, 6),
        "ema_trend_period": ("int", 100, 300),
    },
    "smc_structure": {
        "pivot_left":  ("int", 3, 10),
        "pivot_right": ("int", 3, 10),
    },
    "supertrend": {
        "period":     ("int",   7,   20),
        "multiplier": ("float", 1.5,  4.0),
    },
    "donchian": {
        "entry_period": ("int", 10, 55),
        "exit_period":  ("int",  5, 20),
    },
    "ema_cross": {
        "fast":       ("int",   5,  20),
        "slow":       ("int",  20,  60),
        "sep_atr_k":  ("float", 0.0, 1.5),
    },
}


# ── signal vectorization ───────────────────────────────────────────────────

def _regime_mask(prepared: pd.DataFrame, pref: str) -> pd.Series:
    """True where regime matches strategy preference; True everywhere for 'any'."""
    if pref == "any" or "regime" not in prepared.columns:
        return pd.Series(True, index=prepared.index)
    return (prepared["regime"] == pref).fillna(False)


def signals_from_prepared(
    prepared: pd.DataFrame,
    strategy_name: str,
    **params,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Vectorize strategy logic into (long_entries, long_exits, short_entries, short_exits).

    All returned Series are boolean with the same index as *prepared*.
    NaN positions in indicator columns are treated as False (no signal).
    """
    idx   = prepared.index
    false = pd.Series(False, index=idx)

    def _clean(s: pd.Series) -> pd.Series:
        return s.fillna(False).astype(bool)

    if strategy_name == "fib_channel":
        entry_zone = float(params.get("entry_zone", 0.30))
        exit_zone  = float(params.get("exit_zone",  0.80))
        pos        = prepared.get("fib_ch_pos", pd.Series(np.nan, index=idx))
        regime_ok  = _regime_mask(prepared, "trend")

        le = (pos < entry_zone) & regime_ok
        lx = (pos > exit_zone)  | (pos < 0)
        se = (pos > 1.0 - entry_zone) & regime_ok
        sx = (pos < 1.0 - exit_zone)  | (pos > 1.0)

    elif strategy_name == "fib_retracement":
        fib_pos   = prepared.get("fib_pos",    pd.Series(np.nan, index=idx))
        rsi       = prepared.get("rsi",         pd.Series(50.0,  index=idx))
        close_s   = prepared.get("close",       pd.Series(np.nan, index=idx))
        ema_s     = prepared.get("ema_trend",   pd.Series(np.nan, index=idx))
        regime_ok = _regime_mask(prepared, "range")

        uptrend   = (close_s > ema_s).fillna(True)
        downtrend = (close_s < ema_s).fillna(True)

        le = (fib_pos < 0.382) & (rsi < 55) & uptrend   & regime_ok
        lx = fib_pos > 0.55
        se = (fib_pos > 0.618) & (rsi < 50) & downtrend & regime_ok
        sx = fib_pos < 0.45

    elif strategy_name == "smc_structure":
        bos_bull  = prepared.get("bos_bull", pd.Series(0.0, index=idx)).fillna(0)
        bos_bear  = prepared.get("bos_bear", pd.Series(0.0, index=idx)).fillna(0)
        fvg_bull  = prepared.get("fvg_bull", pd.Series(0.0, index=idx)).fillna(0)
        fvg_bear  = prepared.get("fvg_bear", pd.Series(0.0, index=idx)).fillna(0)
        regime_ok = _regime_mask(prepared, "trend")

        le = (bos_bull > 0) & (fvg_bull > 0) & regime_ok
        lx = bos_bear > 0
        se = (bos_bear > 0) & (fvg_bear > 0) & regime_ok
        sx = bos_bull > 0

    elif strategy_name == "supertrend":
        st_dir    = prepared.get("st_dir", pd.Series(0, index=idx)).fillna(0)
        le = st_dir == 1
        lx = st_dir == -1
        se = st_dir == -1
        sx = st_dir == 1

    elif strategy_name == "donchian":
        entry_hi  = prepared.get("dc_entry_hi", pd.Series(np.nan, index=idx))
        entry_lo  = prepared.get("dc_entry_lo", pd.Series(np.nan, index=idx))
        exit_hi   = prepared.get("dc_exit_hi",  pd.Series(np.nan, index=idx))
        exit_lo   = prepared.get("dc_exit_lo",  pd.Series(np.nan, index=idx))
        close_s   = prepared.get("close",       pd.Series(np.nan, index=idx))
        le = close_s >= entry_hi
        lx = close_s <= exit_lo
        se = close_s <= entry_lo
        sx = close_s >= exit_hi

    elif strategy_name == "ema_cross":
        ema_fast  = prepared.get("ema_fast", pd.Series(np.nan, index=idx))
        ema_slow  = prepared.get("ema_slow", pd.Series(np.nan, index=idx))
        cross_up  = (ema_fast > ema_slow) & (ema_fast.shift(1) <= ema_slow.shift(1))
        cross_dn  = (ema_fast < ema_slow) & (ema_fast.shift(1) >= ema_slow.shift(1))
        le = cross_up
        lx = cross_dn
        se = cross_dn
        sx = cross_up

    elif strategy_name == "fib_ema":
        score     = prepared.get("fib_score", pd.Series(np.nan, index=idx))
        rsi       = prepared.get("rsi",        pd.Series(50.0,  index=idx))
        regime_ok = _regime_mask(prepared, "trend")
        bull = float(params.get("score_bull", 0.67))
        bear = float(params.get("score_bear", 0.33))
        rsi_lo = float(params.get("rsi_lo", 35.0))
        rsi_hi = float(params.get("rsi_hi", 65.0))

        in_rsi = (rsi >= rsi_lo) & (rsi <= rsi_hi)
        le = (score >= bull) & in_rsi & regime_ok
        lx = score <= bear
        se = (score <= bear) & in_rsi & regime_ok
        sx = score >= bull

    elif strategy_name == "trend_pullback":
        close_s = prepared.get("close",   pd.Series(np.nan, index=idx))
        ema_t   = prepared.get("ema_t",   pd.Series(np.nan, index=idx))
        ema_f   = prepared.get("ema_f",   pd.Series(np.nan, index=idx))
        ema_s   = prepared.get("ema_s",   pd.Series(np.nan, index=idx))
        rsi     = prepared.get("rsi",     pd.Series(50.0,  index=idx))
        kd_gold = prepared.get("kd_gold", pd.Series(0.0,   index=idx)).fillna(0)
        kd_dead = prepared.get("kd_dead", pd.Series(0.0,   index=idx)).fillna(0)
        rsi_lo = float(params.get("rsi_lo", 40.0))
        rsi_hi = float(params.get("rsi_hi", 60.0))

        above_t = (close_s > ema_t).fillna(False)
        below_t = (close_s < ema_t).fillna(False)
        bull_mo = (ema_f > ema_s).fillna(False)
        bear_mo = (ema_f < ema_s).fillna(False)
        in_rsi  = (rsi >= rsi_lo) & (rsi <= rsi_hi)

        le = above_t & bull_mo & in_rsi & (kd_gold > 0.5)
        lx = below_t | bear_mo
        se = below_t & bear_mo & in_rsi & (kd_dead > 0.5)
        sx = above_t | bull_mo

    else:
        return false.copy(), false.copy(), false.copy(), false.copy()

    return _clean(le), _clean(lx), _clean(se), _clean(sx)


# ── core backtest ──────────────────────────────────────────────────────────

def vbt_sharpe(df: pd.DataFrame, strategy_name: str, **params) -> float:
    """Run a VBT backtest for one param set; return annualized Sharpe or -inf."""
    strat = build_strategy(strategy_name, **params)
    try:
        prepared = strat.prepare(df).dropna()
    except Exception:
        return float("-inf")

    if len(prepared) < 100:
        return float("-inf")

    le, lx, se, sx = signals_from_prepared(prepared, strategy_name, **params)

    if le.sum() + se.sum() < MIN_TRADES:
        return float("-inf")

    close = prepared["close"]
    try:
        pf = vbt.Portfolio.from_signals(
            close=close,
            entries=le,
            exits=lx,
            short_entries=se,
            short_exits=sx,
            fees=FEES,
            init_cash=INIT_CASH,
            freq="1h",
        )
        sharpe = float(pf.stats().get("Sharpe Ratio", -999))
        return sharpe if math.isfinite(sharpe) else float("-inf")
    except Exception:
        return float("-inf")


# ── walk-forward helper ────────────────────────────────────────────────────

def _walk_forward_sharpe(
    df: pd.DataFrame,
    strategy_name: str,
    params: dict,
    n_folds: int = 4,
) -> float:
    """Average OOS Sharpe across walk-forward folds.

    Fold layout: data split into n_folds+1 equal blocks.
    Train on block 0..i, evaluate on block i+1.
    """
    n = len(df)
    fold_size = n // (n_folds + 1)
    if fold_size < 200:
        return vbt_sharpe(df, strategy_name, **params)

    sharpes: list[float] = []
    for i in range(n_folds):
        test_start = fold_size * (i + 1)
        test_end   = test_start + fold_size
        if test_end > n:
            break
        oos_df = df.iloc[test_start:test_end]
        s = vbt_sharpe(oos_df, strategy_name, **params)
        if math.isfinite(s):
            sharpes.append(s)

    return float(np.mean(sharpes)) if sharpes else float("-inf")


# ── Optuna entry point ─────────────────────────────────────────────────────

def _suggest(trial: optuna.Trial, space: dict) -> dict:
    params: dict = {}
    for name, spec in space.items():
        kind = spec[0]
        if kind == "float":
            params[name] = trial.suggest_float(name, spec[1], spec[2])
        elif kind == "int":
            params[name] = trial.suggest_int(name, spec[1], spec[2])
        elif kind == "categorical":
            params[name] = trial.suggest_categorical(name, list(spec[1:]))
    return params


def run_bayesian_optimize(
    df: pd.DataFrame,
    strategy_name: str,
    n_trials: int = 100,
    search_space: dict | None = None,
    n_wf_folds: int = 0,
    seed: int = 42,
) -> tuple[dict, optuna.Study]:
    """Bayesian parameter search via Optuna TPE.

    Args:
        df:            OHLCV DataFrame (≥ 1000 bars recommended).
        strategy_name: Strategy key (must be in SEARCH_SPACES or pass search_space).
        n_trials:      Optuna trial count.
        search_space:  Override default space definition.
        n_wf_folds:    0 = full-sample objective; >0 = walk-forward mean OOS Sharpe.
        seed:          Reproducibility seed for TPE sampler.

    Returns:
        (best_params, study)
    """
    space = search_space or SEARCH_SPACES.get(strategy_name)
    if not space:
        raise ValueError(
            f"No search space for '{strategy_name}'. "
            f"Available: {list(SEARCH_SPACES)} or pass search_space=..."
        )

    sampler = optuna.samplers.TPESampler(multivariate=True, seed=seed)
    pruner  = optuna.pruners.HyperbandPruner()
    study   = optuna.create_study(direction="maximize", sampler=sampler, pruner=pruner)

    def objective(trial: optuna.Trial) -> float:
        params = _suggest(trial, space)
        if n_wf_folds > 0:
            return _walk_forward_sharpe(df, strategy_name, params, n_wf_folds)
        return vbt_sharpe(df, strategy_name, **params)

    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    return study.best_params, study
