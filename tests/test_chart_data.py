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


# ═══════════════════════════════════════════════════════════════════════════
# 六線密集/發散圖表資料（2026-07-05）：使用者要求另建版面還原 YouTube 雙均線系統，
# 重用已驗證過的 MaConvergencePullbackStrategy，不重寫狀態機（單一事實來源）。
# ═══════════════════════════════════════════════════════════════════════════

def test_ma6_import_does_not_require_heavy_backtest_deps(monkeypatch):
    """同上：確保新函式的相依（core.quant_researcher）沒有偷偷拉進肥依賴。"""
    real_import = builtins.__import__
    blocked = ("backtest", "backtest.backtester", "backtest.optimize",
               "run_optimize", "optuna", "vectorbt", "matplotlib")

    def guard(name, *a, **k):
        if name in blocked or name.split(".")[0] in ("optuna", "vectorbt", "matplotlib"):
            raise ImportError(f"blocked heavy dep: {name}")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    sys.modules.pop("core.chart_data", None)
    mod = importlib.import_module("core.chart_data")
    assert hasattr(mod, "ma6_overlay_data")


def test_ma6_overlay_data_returns_six_lines_and_signals():
    from core.chart_data import ma6_overlay_data
    out = ma6_overlay_data(source="synthetic", limit=300)
    for key in ("candles", "ma20", "ma60", "ma120", "ema20", "ema60", "ema120", "ma6_signals"):
        assert key in out, f"缺欄位 {key}"
    assert len(out["candles"]) > 0
    # 六線在暖機（前120根）應該是空/短，暖機後應該有值
    assert len(out["ma120"]) < len(out["candles"])   # 120期 rolling 暖機期沒有值，天然比蠟燭少


def test_ma6_signals_have_time_and_direction():
    from core.chart_data import ma6_overlay_data
    out = ma6_overlay_data(source="synthetic", limit=300)
    for sig in out["ma6_signals"]:
        assert "time" in sig and "dir" in sig
        assert sig["dir"] in (1, -1)


def test_ma6_overlay_data_uses_same_strategy_as_live_bot():
    """驗證圖表用的是 MaConvergencePullbackStrategy 本尊算出來的欄位，不是另外重寫的邏輯
    （避免圖表畫的密集/發散跟 b9 實際下單依據的訊號不一致）。"""
    from core.chart_data import ma6_overlay_data
    from core.quant_researcher import build_strategy
    from run_optimize import make_synthetic
    df = make_synthetic(300)
    expected = build_strategy("ma_convergence_pullback").prepare(df.copy())
    out = ma6_overlay_data(source="synthetic", limit=300)
    # pullback1 型訊號數量應與策略本身算出的 is_first_pullback True 數一致
    # （b9 只下單 pullback1，這條保證圖上的首踩標記數 = b9 進場依據數）
    n_pb1 = sum(1 for s in out["ma6_signals"] if s["type"] == "pullback1")
    assert n_pb1 == int(expected["is_first_pullback"].sum())


# ── 三種訊號分型 + 密集區（2026-07-05）：方法一密集突破 / 首次回踩 / 二次回踩 ──
def test_ma6_signals_carry_type_field():
    from core.chart_data import ma6_overlay_data
    out = ma6_overlay_data(source="synthetic", limit=400)
    valid = {"breakout", "pullback1", "pullback2"}
    for sig in out["ma6_signals"]:
        assert sig.get("type") in valid, f"訊號 type 非法：{sig.get('type')}"


def test_ma6_returns_density_zones():
    from core.chart_data import ma6_overlay_data
    out = ma6_overlay_data(source="synthetic", limit=400)
    assert "density" in out          # 密集區逐根布林（前端可標示）
    for d in out["density"]:
        assert "time" in d and "value" in d


def test_ma6_signal_types_match_strategy_columns():
    """圖表三型訊號數量 = 策略欄位 is_breakout/is_first_pullback/is_second_pullback 的 True 數。"""
    from core.chart_data import ma6_overlay_data
    from core.quant_researcher import build_strategy
    from run_optimize import make_synthetic
    df = make_synthetic(400)
    prep = build_strategy("ma_convergence_pullback").prepare(df.copy())
    out = ma6_overlay_data(source="synthetic", limit=400)
    by_type = {}
    for s in out["ma6_signals"]:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    assert by_type.get("breakout", 0) == int(prep["is_breakout"].sum())
    assert by_type.get("pullback1", 0) == int(prep["is_first_pullback"].sum())
    assert by_type.get("pullback2", 0) == int(prep["is_second_pullback"].sum())
