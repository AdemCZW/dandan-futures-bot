"""core.chart_data 輕量圖表資料層測試。

重點保證：這個模組能在「沒有回測/最佳化肥依賴」的環境 import 成功，
這樣只跑 bot 的雲端容器才能直接用它吐圖表資料而不背 vectorbt/optuna/matplotlib。
"""
import sys
import importlib
import builtins

import pytest

from core.chart_data import build_trade_markers, parse_ts_unix, trade_markers, klines_data


def test_import_does_not_require_heavy_backtest_deps(monkeypatch):
    """模擬 backtest / run_optimize / optuna 不存在 → chart_data 仍能 import。"""
    real_import = builtins.__import__
    blocked = ("backtest", "backtest.backtester", "backtest.optimize",
               "run_optimize", "optuna", "vectorbt", "matplotlib")

    def guard(name, *a, **k):
        if name in blocked or name.split(".")[0] in ("optuna", "vectorbt", "matplotlib"):
            raise ImportError(f"blocked heavy dep: {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    sys.modules.pop("core.chart_data", None)
    mod = importlib.import_module("core.chart_data")   # 不該因缺肥依賴而失敗
    assert hasattr(mod, "klines_data")


def test_parse_ts_unix_iso_and_plain():
    assert parse_ts_unix("2026-07-01 00:00:00") is not None
    assert parse_ts_unix("2026-07-01T00:00:00+00:00") is not None
    assert parse_ts_unix("") is None
    assert parse_ts_unix("garbage") is None


def test_build_trade_markers_buckets_and_filters_symbol():
    trades = [
        {"ts": "2026-07-01 00:00:00", "symbol": "BTCUSDT", "side": "entry",
         "price": 100.0, "strategy": "fib_channel", "mode": "live_futures_testnet"},
        {"ts": "2026-07-01 00:30:00", "symbol": "BTCUSDT", "side": "entry",
         "price": 102.0, "strategy": "fib_channel", "mode": "live_futures_testnet"},
        {"ts": "2026-07-01 00:10:00", "symbol": "ETHUSDT", "side": "entry",
         "price": 50.0, "strategy": "fib_channel", "mode": "live_futures_testnet"},
    ]
    out = build_trade_markers(trades, "BTCUSDT", bucket_hours=6)
    # 同 6h 桶、同方向的兩筆 BTC entry 聚合成 1 點，均價 101；ETH 被過濾掉
    assert len(out["markers"]) == 1
    m = out["markers"][0]
    assert m["price"] == 101.0 and m["count"] == 2 and m["dir"] == 1


def test_build_trade_markers_short_direction():
    trades = [{"ts": "2026-07-01 00:00:00", "symbol": "BTCUSDT",
               "side": "entry_short", "price": 100.0, "strategy": "s", "mode": "m"}]
    out = build_trade_markers(trades, "BTCUSDT")
    assert out["markers"][0]["dir"] == -1
