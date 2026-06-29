"""Bayesian parameter optimizer — VectorBT objective + Optuna TPE.

Replaces the exhaustive grid in run_optimize.py with adaptive sampling.
Needs far fewer evaluations (50-200 vs thousands) and runs each evaluation
8-25x faster via VectorBT's vectorized portfolio engine.

Usage:
    # Synthetic data (quick smoke test)
    python run_optimize_bayesian.py fib_channel --synthetic --trials 50

    # Real data with walk-forward validation
    python run_optimize_bayesian.py fib_channel --cache btc_1h.csv --trials 200 --wf-folds 4

    # All supported strategies
    python run_optimize_bayesian.py smc_structure --cache eth_1h.csv --trials 100
"""
import os
import argparse
import numpy as np
import pandas as pd

from config import Config
from core.market_analyst import make_client, fetch_historical_klines, load_klines, save_klines
from backtest.vbt_optimize import run_bayesian_optimize, vbt_sharpe, SEARCH_SPACES


def make_synthetic(n: int = 8000, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0002, 0.012, n)
    close = 30000 * np.exp(np.cumsum(rets))
    high = close * (1 + np.abs(rng.normal(0, 0.003, n)))
    low  = close * (1 - np.abs(rng.normal(0, 0.003, n)))
    op   = np.r_[close[0], close[:-1]]
    vol  = rng.lognormal(3, 0.5, n)
    idx  = pd.date_range("2025-06-01", periods=n, freq="1h")
    return pd.DataFrame({"open": op, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def get_data(args, cfg) -> pd.DataFrame:
    if args.synthetic:
        df = make_synthetic()
        print(f"[合成資料] {len(df)} 根 K 線（離線示範）")
        return df
    if args.cache and os.path.exists(args.cache):
        df = load_klines(args.cache)
        print(f"[快取] 讀入 {len(df)} 根 K 線：{args.cache}")
        return df
    client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
    df = fetch_historical_klines(client, cfg.symbol, cfg.interval, args.start)
    print(f"[幣安] {len(df)} 根 {cfg.interval} K 線：{df.index[0]} ~ {df.index[-1]}")
    if args.cache:
        save_klines(df, args.cache)
        print(f"快取 → {args.cache}")
    return df


def main():
    ap = argparse.ArgumentParser(description="Bayesian strategy optimizer")
    ap.add_argument("strategy", nargs="?", default="fib_channel",
                    choices=list(SEARCH_SPACES))
    ap.add_argument("--synthetic", action="store_true", help="合成資料（離線）")
    ap.add_argument("--start", default="6 months ago UTC")
    ap.add_argument("--cache", default="")
    ap.add_argument("--trials", type=int, default=100, help="Optuna trial 數")
    ap.add_argument("--wf-folds", type=int, default=0,
                    help="Walk-forward fold 數（0=全樣本目標函數）")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    cfg = Config()
    cfg.strategy = args.strategy
    df = get_data(args, cfg)

    space = SEARCH_SPACES[args.strategy]
    n_combos = 1
    for spec in space.values():
        if spec[0] == "float":
            n_combos *= 20   # rough estimate
        elif spec[0] == "int":
            n_combos *= (spec[2] - spec[1] + 1)
    equiv_grid = n_combos

    mode = f"walk-forward ({args.wf_folds} folds)" if args.wf_folds else "full-sample"
    print(f"\n=== 貝葉斯最佳化：{args.strategy} | {args.trials} trials | {mode} ===")
    print(f"搜尋空間維度：{len(space)} 個參數（等效全排列 ≈ {equiv_grid:,} 組）")
    print("優化中…（Optuna TPE + VectorBT 評估）\n")

    best, study = run_bayesian_optimize(
        df, args.strategy,
        n_trials=args.trials,
        n_wf_folds=args.wf_folds,
        seed=args.seed,
    )

    # ── 結果 ──
    best_val = study.best_value
    label = "in-sample Sharpe" if args.wf_folds > 0 else "Sharpe"
    print(f"最佳 {label}：{best_val:.4f}")
    # walk-forward 模式：印出保留 OOS 尾段的樣本外分數（搜尋期間從未使用，OPT-10）。
    # IS 高、OOS 崩 → 過擬合警訊。
    oos = study.user_attrs.get("oos_sharpe")
    if oos is not None:
        gap = best_val - oos
        print(f"保留 OOS Sharpe：{oos:.4f}　(IS→OOS 衰減 {gap:+.4f}"
              f"{'　⚠️ 衰減大，疑過擬合' if gap > 0.5 else ''})")
    print("\n最佳參數：")
    for k, v in best.items():
        print(f"  {k:20s} = {v}")

    # ── 驗證最佳參數的完整統計 ──
    print("\n=== 最佳參數全樣本驗證 ===")
    from core.quant_researcher import build_strategy
    import vectorbt as vbt
    from backtest.vbt_optimize import signals_from_prepared

    strat = build_strategy(args.strategy, **best)
    prepared = strat.prepare(df).dropna()
    le, lx, se, sx = signals_from_prepared(prepared, args.strategy, **best)
    pf = vbt.Portfolio.from_signals(
        close=prepared["close"],
        entries=le, exits=lx,
        short_entries=se, short_exits=sx,
        fees=0.0004, init_cash=10_000, freq="1h",
    )
    stats = pf.stats()
    for metric in ["Total Return [%]", "Annualized Return [%]", "Sharpe Ratio",
                   "Max Drawdown [%]", "Total Trades", "Win Rate [%]"]:
        val = stats.get(metric)
        if val is not None:
            print(f"  {metric:25s}: {val:.2f}")

    # ── Optuna trial 分佈摘要 ──
    trials_df = study.trials_dataframe()
    finished  = trials_df[trials_df["value"].notna()]
    if len(finished) > 0:
        print(f"\n完成 trial：{len(finished)}  "
              f"最佳={finished['value'].max():.3f}  "
              f"中位={finished['value'].median():.3f}  "
              f"差距={finished['value'].max() - finished['value'].median():.3f}")
        print("（差距越大 → 參數敏感度高，越可能過擬合）")


if __name__ == "__main__":
    main()
