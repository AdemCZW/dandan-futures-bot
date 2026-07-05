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
    （避免圖表畫的密集/發散跟策略本身的訊號不一致）。

    2026-07-06：圖表明確開啟 require_density_for_breakout=True（修正 is_breakout 誤判
    bug），b9 實盤暫時維持預設關閉——兩者在這個參數上刻意分岔，故這裡的 expected
    也要用同樣的參數建構，才是跟圖表對齊的「單一事實來源」比較基準。"""
    from core.chart_data import ma6_overlay_data
    from core.quant_researcher import build_strategy
    from run_optimize import make_synthetic
    df = make_synthetic(300)
    expected = build_strategy("ma_convergence_pullback",
                              require_density_for_breakout=True).prepare(df.copy())
    out = ma6_overlay_data(source="synthetic", limit=300)
    # pullback1 型訊號數量應與策略本身算出的 is_first_pullback True 數一致
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
    """圖表三型訊號數量 = 策略欄位 is_breakout/is_first_pullback/is_second_pullback 的 True 數。

    圖表用 require_density_for_breakout=True（見上一測試的說明），比較基準要用同樣參數。"""
    from core.chart_data import ma6_overlay_data
    from core.quant_researcher import build_strategy
    from run_optimize import make_synthetic
    df = make_synthetic(400)
    prep = build_strategy("ma_convergence_pullback",
                          require_density_for_breakout=True).prepare(df.copy())
    out = ma6_overlay_data(source="synthetic", limit=400)
    by_type = {}
    for s in out["ma6_signals"]:
        by_type[s["type"]] = by_type.get(s["type"], 0) + 1
    assert by_type.get("breakout", 0) == int(prep["is_breakout"].sum())
    assert by_type.get("pullback1", 0) == int(prep["is_first_pullback"].sum())
    assert by_type.get("pullback2", 0) == int(prep["is_second_pullback"].sum())


# ═══════════════════════════════════════════════════════════════════════════
# _fetch_ohlcv_df 快取 + 429/418 退避（2026-07-06）：real fetch（source="testnet"，
# 其實是打 fapi.binance.com 公開合約 API）原本每次呼叫都重新打 Binance、完全沒有
# 快取或退避——今天測試+部署期間反覆打 /ma6 把伺服器的共用 IP 打到被 Binance
# 回 418(IP已被封)。修法：短 TTL 快取 + 429/418 退避（記錄封鎖到期時間，封鎖中
# 有舊資料就先給舊的、沒有就明確報錯，不再重複觸發/延長封鎖）。
# ═══════════════════════════════════════════════════════════════════════════

import urllib.error


def _raw_kline(open_time_ms, price=100.0):
    return [open_time_ms, str(price), str(price + 1), str(price - 1), str(price),
            "1000", open_time_ms + 1, "100000", 10, "500", "50000", "0"]


def _reset_chart_data_cache():
    from core import chart_data
    chart_data._KLINE_CACHE.clear()
    chart_data._BINANCE_BACKOFF["blocked_until"] = 0.0


def test_fetch_ohlcv_df_caches_within_ttl(monkeypatch):
    """TTL 窗口內重複呼叫同一 symbol/interval/limit → 不重新打 Binance。"""
    from core import chart_data
    _reset_chart_data_cache()

    calls = {"n": 0}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps([_raw_kline(1_000_000)]).encode()

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(chart_data.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(chart_data, "_now", lambda: 1000.0)

    chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")
    chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")
    assert calls["n"] == 1, "TTL 窗口內第二次呼叫應該吃快取，不重打 Binance"


def test_fetch_ohlcv_df_refetches_after_ttl_expires(monkeypatch):
    """TTL 過期後應該重新打 Binance。"""
    from core import chart_data
    _reset_chart_data_cache()

    calls = {"n": 0}

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps([_raw_kline(1_000_000)]).encode()

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        return FakeResp()

    monkeypatch.setattr(chart_data.urllib.request, "urlopen", fake_urlopen)
    now_box = {"t": 1000.0}
    monkeypatch.setattr(chart_data, "_now", lambda: now_box["t"])

    chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")
    now_box["t"] = 1000.0 + chart_data._KLINE_CACHE_TTL + 1
    chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")
    assert calls["n"] == 2, "TTL 過期後應該重新打一次"


def test_fetch_ohlcv_df_backs_off_on_429_and_serves_stale_cache(monkeypatch):
    """429 時記錄退避；若有舊快取，用舊資料頂上，不整個報錯，也不再打第二次。"""
    from core import chart_data
    _reset_chart_data_cache()

    calls = {"n": 0}

    class OkResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import json
            return json.dumps([_raw_kline(1_000_000)]).encode()

    def fake_urlopen_ok(req, timeout=10):
        calls["n"] += 1
        return OkResp()

    now_box = {"t": 1000.0}
    monkeypatch.setattr(chart_data, "_now", lambda: now_box["t"])
    monkeypatch.setattr(chart_data.urllib.request, "urlopen", fake_urlopen_ok)
    chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")   # 先成功一次、灌快取
    assert calls["n"] == 1

    now_box["t"] = 1000.0 + chart_data._KLINE_CACHE_TTL + 1     # 讓快取過期，逼下一次真的發請求

    def fake_urlopen_429(req, timeout=10):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url if hasattr(req, "full_url") else "x",
                                     429, "Too Many Requests", {"Retry-After": "30"}, None)

    monkeypatch.setattr(chart_data.urllib.request, "urlopen", fake_urlopen_429)
    df = chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")
    assert calls["n"] == 2
    assert df is not None and len(df) > 0, "429 時應該用舊快取頂上，不能整個掛掉"

    # 退避期間再呼叫一次：不應該再打 Binance（避免延長封鎖）
    now_box["t"] += 5
    chart_data._fetch_ohlcv_df("BTCUSDT", "4h", 5, "testnet")
    assert calls["n"] == 2, "退避期間不該再打 Binance"


def test_fetch_ohlcv_df_raises_clearly_when_rate_limited_with_no_cache(monkeypatch):
    """第一次呼叫就被 418，且沒有舊快取可頂 → 明確報錯（不是靜默回傳壞資料）。"""
    from core import chart_data
    _reset_chart_data_cache()

    calls = {"n": 0}

    def fake_urlopen_418(req, timeout=10):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url if hasattr(req, "full_url") else "x",
                                     418, "I'm a teapot", {}, None)

    monkeypatch.setattr(chart_data.urllib.request, "urlopen", fake_urlopen_418)
    monkeypatch.setattr(chart_data, "_now", lambda: 1000.0)

    with pytest.raises(Exception):
        chart_data._fetch_ohlcv_df("LINKUSDT", "4h", 5, "testnet")
    assert calls["n"] == 1

    # 封鎖中再呼叫：不該再打 Binance（沒有 Retry-After 時 418 用比 429 更長的預設退避）
    with pytest.raises(Exception):
        chart_data._fetch_ohlcv_df("LINKUSDT", "4h", 5, "testnet")
    assert calls["n"] == 1, "封鎖中不該再打 Binance"
