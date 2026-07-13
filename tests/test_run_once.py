"""一次性進入點 run_once：驗證單輪行為、決策去重、失敗隔離（離線、假 trader/build_fn）。

不碰交易所（build_fn/fetch_fn 全注入假物件），對齊 run_multi_futures.poll_loop 單輪語意。
"""
import pandas as pd
import pytest

import run_once
from run_multi_futures import BotWorker


class FakeTrader:
    """只實作 run_bot_once 會用到的介面。"""

    def __init__(self, last_bar=None):
        self._last_bar = last_bar
        self.dir = 0
        self.calls = []

    def restored_last_bar(self):
        return self._last_bar

    def check_soft_stops(self, price):
        self.calls.append(("soft", price))
        return None

    def check_profit_floor(self, price):
        self.calls.append(("floor", price))
        return False

    def _heartbeat(self, price):
        self.calls.append(("heartbeat", price))

    def on_bar_close(self, bar_time):
        self.calls.append(("on_bar_close", bar_time))

    def manual_close(self):
        return "closed"


class _Cfg:
    symbol = "BTCUSDT"
    interval = "4h"


def _df(bars):
    idx = pd.to_datetime(bars)
    return pd.DataFrame({"close": [100.0] * len(bars)}, index=idx)


def _worker(tmp_path, bid="s1"):
    return BotWorker(
        {"id": bid, "symbol": "BTCUSDT", "strategy": "smc_structure", "interval": "4h"},
        state_dir=str(tmp_path),
    )


_THREE_BARS = ["2026-07-14 00:00", "2026-07-14 04:00", "2026-07-14 08:00"]
# 最新已收盤 = index[-2] = 04:00（index[-1]=08:00 是還在形成的那根）


def test_new_bar_triggers_on_bar_close(tmp_path):
    trader = FakeTrader(last_bar=pd.Timestamp("2026-07-14 00:00"))
    res = run_once.run_bot_once(
        _worker(tmp_path),
        build_fn=lambda w: (trader, object(), _Cfg()),
        fetch_fn=lambda *a, **k: _df(_THREE_BARS),
        log=lambda *a: None,
    )
    assert res == "decided"
    assert ("on_bar_close", pd.Timestamp("2026-07-14 04:00")) in trader.calls
    assert not any(c[0] == "heartbeat" for c in trader.calls)


def test_same_bar_only_heartbeats(tmp_path):
    """restored_last_bar == 最新已收盤那根 → 去重，只心跳、不重複決策（防 F2）。"""
    trader = FakeTrader(last_bar=pd.Timestamp("2026-07-14 04:00"))
    res = run_once.run_bot_once(
        _worker(tmp_path),
        build_fn=lambda w: (trader, object(), _Cfg()),
        fetch_fn=lambda *a, **k: _df(_THREE_BARS),
        log=lambda *a: None,
    )
    assert res == "heartbeat"
    assert not any(c[0] == "on_bar_close" for c in trader.calls)
    assert ("heartbeat", 100.0) in trader.calls


def test_soft_stops_and_floor_run_every_invocation(tmp_path):
    trader = FakeTrader(last_bar=None)
    run_once.run_bot_once(
        _worker(tmp_path),
        build_fn=lambda w: (trader, object(), _Cfg()),
        fetch_fn=lambda *a, **k: _df(_THREE_BARS),
        log=lambda *a: None,
    )
    assert ("soft", 100.0) in trader.calls
    assert ("floor", 100.0) in trader.calls


def test_main_isolates_failures_partial_ok_exit_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(run_once, "STATE_DIR", str(tmp_path))
    monkeypatch.setenv(
        "BOTS_CONFIG",
        '[{"id":"ok","symbol":"BTCUSDT","strategy":"smc_structure","interval":"4h"},'
        '{"id":"bad","symbol":"ETHUSDT","strategy":"smc_structure","interval":"4h"}]',
    )
    seen = []

    def fake_run(worker, **k):
        seen.append(worker.id)
        if worker.id == "bad":
            raise RuntimeError("boom")
        return "decided"

    monkeypatch.setattr(run_once, "run_bot_once", fake_run)
    run_once.main()                     # 部分失敗 → 不 sys.exit
    assert seen == ["ok", "bad"]        # 壞的那台不擋前一台，也照樣被嘗試


def test_main_all_fail_exits_nonzero(tmp_path, monkeypatch):
    monkeypatch.setattr(run_once, "STATE_DIR", str(tmp_path))
    monkeypatch.setenv(
        "BOTS_CONFIG",
        '[{"id":"a","symbol":"BTCUSDT","strategy":"smc_structure","interval":"4h"}]',
    )

    def boom(worker, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(run_once, "run_bot_once", boom)
    with pytest.raises(SystemExit) as ei:
        run_once.main()
    assert ei.value.code == 1


def test_main_bad_config_exits_two(monkeypatch):
    monkeypatch.setenv("BOTS_CONFIG", "not-json")
    with pytest.raises(SystemExit) as ei:
        run_once.main()
    assert ei.value.code == 2
