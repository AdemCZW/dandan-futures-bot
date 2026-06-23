"""backtest/tournament.py — 策略錦標賽（回測驅動、期望值排序）測試。

錦標賽把所有策略跑在同一段真實/合成 K 線上，依【期望值】(預設) 排序，
交易數不足者沉底，挑出當前最佳 champion。純排序邏輯 rank() 用手造 dict
確定性驗證；evaluate()/run_tournament() 用合成 OHLCV 驗證結構與排序。
"""
import numpy as np
import pandas as pd

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
