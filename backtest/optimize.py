"""參數掃描 + Walk-forward 分析 — 用來檢驗「那些數據值有沒有道理」。

兩件事：
  1. sweep()        全樣本網格掃描，看績效對參數有多敏感。
                    若只有某一格特別漂亮、隔壁格就崩 → 過擬合的味道。
  2. walk_forward() 把資料切成「訓練段→測試段」逐步前推。
                    在訓練段挑最佳參數，鎖定後只在「沒看過的」測試段評估。
                    這是偵測過擬合的標準做法：訓練漂亮但測試崩 = 不會泛化。

注意：sweep 只在「訓練段」上選參數，評估只在「後續測試段」，不偷看未來。
"""
from __future__ import annotations
import dataclasses
import itertools
import pandas as pd
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest


# 屬於風控官的參數（其餘視為策略參數）。網格裡若出現這些 key，
# 會被套到一份專屬 cfg 上，讓 run_optimize 能一起掃描停損/停利/倉位上限等。
RISK_KEYS = {"risk_per_trade", "max_position_pct", "stop_loss_pct",
             "take_profit_pct", "max_daily_loss_pct",
             "atr_mult_sl", "tp_R_mult", "chand_mult"}   # ATR 停損 / R 停利 / Chandelier 倍數


def param_grid(space: dict) -> list[dict]:
    """把 {param: [候選值...]} 展開成所有組合的笛卡爾積。"""
    keys = list(space)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(space[k] for k in keys))]


def _split_params(params: dict) -> tuple[dict, dict]:
    """把一組參數拆成 (策略參數, 風控參數)。"""
    strat_p = {k: v for k, v in params.items() if k not in RISK_KEYS}
    risk_p = {k: v for k, v in params.items() if k in RISK_KEYS}
    return strat_p, risk_p


def _risk_and_cfg(cfg, risk_p: dict):
    """依風控參數產生專屬 cfg 與「全新」RiskOfficer。

    每個組合都建全新 RiskOfficer，順帶避免單日熔斷狀態在組合間殘留。
    """
    cfg_i = dataclasses.replace(cfg, **risk_p) if risk_p else cfg
    return RiskOfficer(cfg_i), cfg_i


def _score(res, objective: str, min_trades: int) -> float:
    """把一次回測結果換成可排序的分數。

    交易筆數太少直接給 -inf：避免「一兩筆好運」被當成好策略。
    純看 total_return 最容易過擬合，預設用 sharpe，並提供 return_dd。
    """
    if len(res.trades) < min_trades:
        return float("-inf")
    if objective == "return":
        return res.total_return
    if objective == "return_dd":
        dd = abs(res.max_drawdown)
        return res.total_return / dd if dd > 1e-9 else float("-inf")
    return res.sharpe  # default


def sweep(df, strategy_name, space, risk, cfg,
          objective: str = "sharpe", min_trades: int = 5) -> pd.DataFrame:
    """對 space 內所有參數組合各跑一次回測，回傳依分數排序的表格。

    space 可同時含策略參數與風控參數（見 RISK_KEYS）；風控參數會套到
    一份專屬 cfg。注意：每組合都用全新 RiskOfficer，傳入的 risk 參數已不再使用。
    """
    rows = []
    for params in param_grid(space):
        strat_p, risk_p = _split_params(params)
        risk_i, cfg_i = _risk_and_cfg(cfg, risk_p)
        res = run_backtest(df, build_strategy(strategy_name, **strat_p), risk_i, cfg_i)
        rows.append({
            **params,
            "trades": len(res.trades),
            "total_return": res.total_return,
            "max_drawdown": res.max_drawdown,
            "win_rate": res.win_rate,
            "sharpe": res.sharpe,
            "score": _score(res, objective, min_trades),
        })
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


def walk_forward(df, strategy_name, space, risk, cfg,
                 train_bars: int, test_bars: int,
                 objective: str = "sharpe", min_trades: int = 5,
                 purge: int = 0) -> pd.DataFrame:
    """逐 fold 前推：訓練段選最佳參數 → 鎖定 → 在後續測試段評估。

    purge>0（OPT-11）在訓練段與測試段間留 embargo gap，降低跨界序列相關偏誤。
    """
    keys = list(space)
    folds, start, fid = [], 0, 0
    while start + train_bars + purge + test_bars <= len(df):
        train = df.iloc[start: start + train_bars]
        test_start = start + train_bars + purge
        test = df.iloc[test_start: test_start + test_bars]

        ranked = sweep(train, strategy_name, space, risk, cfg, objective, min_trades)
        best = ranked.iloc[0]
        if best["score"] == float("-inf"):
            # 該訓練段所有組合交易數都不足、無可信參數可選 → 跳過此 fold，
            # 不讓無資訊的「任意一格」參數污染 OOS 彙總。仍前推測試窗避免無限迴圈。
            start += test_bars
            continue
        best_params = {k: best[k] for k in keys}

        strat_p, risk_p = _split_params(best_params)
        risk_i, cfg_i = _risk_and_cfg(cfg, risk_p)
        res = run_backtest(test, build_strategy(strategy_name, **strat_p), risk_i, cfg_i)
        folds.append({
            "fold": fid,
            "test_start": test.index[0],
            "test_end": test.index[-1],
            **{f"p_{k}": best_params[k] for k in keys},
            "IS_return": best["total_return"],
            "OOS_return": res.total_return,
            "OOS_sharpe": res.sharpe,
            "OOS_maxDD": res.max_drawdown,
            "OOS_trades": len(res.trades),
        })
        fid += 1
        start += test_bars  # 測試段不重疊，前推一個測試窗
    return pd.DataFrame(folds)
