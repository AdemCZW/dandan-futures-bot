"""多 bot 監督器 + 命名空間 HTTP + 崩潰隔離（離線、假 client）。

驗證把多台 FuturesLiveTrader 跑在同一進程時的隔離正確性：
state 檔、close 旗標、trades 過濾、崩潰不互相拖累。實際雲端往返需金鑰，這裡只測可離線的部分。
"""
import json
import os
from decimal import Decimal

import pytest

from config import Config
from core.risk_officer import RiskOfficer
from core.bot_state import BotState
import run_live_futures as M


# ── 離線假物件 ──────────────────────────────────────────────────────────
class FakeExecu:
    def __init__(self):
        self.amt = 0.0
        self._filters = {"min_qty": Decimal("0.001")}
        self._mark = 100.0
    def position_amt(self): return self.amt
    def balance(self, a="USDT"): return 10000.0
    def mark_price(self): return self._mark


class FakeJournal:
    def __init__(self): self.records = []
    def log(self, side, price, qty=0, pnl=0, ts=None):
        self.records.append({"side": side, "price": price, "qty": qty, "pnl": pnl})


class ScriptStrat:
    allow_short = True
    def __init__(self, sigs=(0,)): self.sigs = list(sigs); self.i = -1
    def prepare(self, df): return df
    def signal(self, row, pos): self.i += 1; return self.sigs[min(self.i, len(self.sigs) - 1)]
    def warmup_bars(self): return 200


def _make_trader(state_path, symbol="ETHUSDT", strategy="trend_pullback"):
    cfg = Config()
    cfg.symbol = symbol
    cfg.strategy = strategy
    return M.FuturesLiveTrader(cfg, None, ScriptStrat(), RiskOfficer(cfg),
                               FakeExecu(), FakeJournal(), state_path=state_path)


# ── 任務 #72：state_path 實例隔離 ───────────────────────────────────────
def test_state_path_isolation_two_traders_distinct_files(tmp_path, monkeypatch):
    """兩個 trader 用不同 state_path → 各寫各的檔，互不覆蓋。"""
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    p_eth = str(tmp_path / "bot_state_eth.json")
    p_sol = str(tmp_path / "bot_state_sol.json")
    eth = _make_trader(p_eth, symbol="ETHUSDT")
    sol = _make_trader(p_sol, symbol="SOLUSDT")
    eth.dir = 1; eth.entry_price = 2000.0; eth._save()
    sol.dir = -1; sol.entry_price = 70.0; sol._save()
    assert os.path.exists(p_eth) and os.path.exists(p_sol)
    assert BotState.load(p_eth).symbol == "ETHUSDT"
    assert BotState.load(p_eth).direction == 1
    assert BotState.load(p_sol).symbol == "SOLUSDT"
    assert BotState.load(p_sol).direction == -1


def test_state_path_defaults_to_module_global(tmp_path, monkeypatch):
    """不傳 state_path → 解析當前模組全域 STATE_PATH（保 monkeypatch 既有測試相容）。"""
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    patched = str(tmp_path / "global_state.json")
    monkeypatch.setattr(M, "STATE_PATH", patched)
    cfg = Config(); cfg.symbol = "BTCUSDT"; cfg.strategy = "x"
    t = M.FuturesLiveTrader(cfg, None, ScriptStrat(), RiskOfficer(cfg), FakeExecu(), FakeJournal())
    assert t.state_path == patched


# ── 任務 #73：BOTS_CONFIG 解析器 ────────────────────────────────────────
import run_multi_futures as MM


def test_parse_bots_config_valid_two_bots():
    """有效設定（兩台）→ 正規化 list，套用預設值。"""
    raw = ('[{"id":"eth","symbol":"ETHUSDT","strategy":"trend_pullback","interval":"1h"},'
           '{"id":"sol","symbol":"SOLUSDT","strategy":"trend_pullback","interval":"1h"}]')
    bots = MM.parse_bots_config(raw)
    assert [b["id"] for b in bots] == ["eth", "sol"]
    assert bots[0]["symbol"] == "ETHUSDT"
    assert bots[1]["symbol"] == "SOLUSDT"
    # 預設值套用
    assert bots[0]["params"] == {}
    assert bots[0]["leverage"] == 1
    assert bots[0]["poll"] == 30


def test_parse_bots_config_defaults_merge_and_override():
    """defaults 提供共用值；每台可覆蓋。"""
    raw = ('[{"id":"eth","symbol":"ETHUSDT"},'
           '{"id":"sol","symbol":"SOLUSDT","leverage":3}]')
    defaults = {"strategy": "trend_pullback", "interval": "1h", "leverage": 1}
    bots = MM.parse_bots_config(raw, defaults=defaults)
    assert bots[0]["strategy"] == "trend_pullback"   # 來自 defaults
    assert bots[0]["leverage"] == 1                   # defaults
    assert bots[1]["leverage"] == 3                   # 每台覆蓋


def test_parse_bots_config_params_preserved():
    """策略 params（dict）原樣保留。"""
    raw = ('[{"id":"sol","symbol":"SOLUSDT","strategy":"fib_channel","interval":"15m",'
           '"params":{"mode":"trend","volume_spike_ratio":1.8}}]')
    bots = MM.parse_bots_config(raw)
    assert bots[0]["params"]["mode"] == "trend"
    assert bots[0]["params"]["volume_spike_ratio"] == 1.8


def test_parse_bots_config_empty_raises():
    for raw in ("", None, "[]"):
        with pytest.raises(ValueError):
            MM.parse_bots_config(raw)


def test_parse_bots_config_invalid_json_raises():
    with pytest.raises(ValueError):
        MM.parse_bots_config("{not json")


def test_parse_bots_config_not_a_list_raises():
    with pytest.raises(ValueError):
        MM.parse_bots_config('{"id":"eth","symbol":"ETHUSDT"}')   # dict 非 list


def test_parse_bots_config_missing_required_field_raises():
    raw = '[{"id":"eth","strategy":"trend_pullback","interval":"1h"}]'   # 缺 symbol
    with pytest.raises(ValueError) as e:
        MM.parse_bots_config(raw)
    assert "symbol" in str(e.value)


def test_parse_bots_config_duplicate_id_raises():
    raw = ('[{"id":"x","symbol":"ETHUSDT","strategy":"trend_pullback","interval":"1h"},'
           '{"id":"x","symbol":"SOLUSDT","strategy":"trend_pullback","interval":"1h"}]')
    with pytest.raises(ValueError) as e:
        MM.parse_bots_config(raw)
    assert "id" in str(e.value).lower()


@pytest.mark.parametrize("bad_id", ["../etc", "a/b", "a.b", "has space", "x!", ""])
def test_parse_bots_config_unsafe_id_raises(bad_id):
    """id 會變成檔名與路由前綴 → 只允許 [A-Za-z0-9_-]，擋路徑穿越。"""
    raw = json.dumps([{"id": bad_id, "symbol": "ETHUSDT",
                       "strategy": "trend_pullback", "interval": "1h"}])
    with pytest.raises(ValueError):
        MM.parse_bots_config(raw)


# ── 任務 #75：命名空間 HTTP 路由 + close 隔離 ──────────────────────────
def _two_workers(state_dir):
    eth = MM.BotWorker({"id": "eth", "symbol": "ETHUSDT", "strategy": "trend_pullback",
                        "interval": "1h", "params": {}, "leverage": 1, "poll": 30},
                       state_dir=str(state_dir))
    sol = MM.BotWorker({"id": "sol", "symbol": "SOLUSDT", "strategy": "trend_pullback",
                        "interval": "1h", "params": {}, "leverage": 1, "poll": 30},
                       state_dir=str(state_dir))
    return {"eth": eth, "sol": sol}


def test_worker_paths_derived_from_id(tmp_path):
    """每台 bot 的 state 檔與 close 旗標路徑都帶 id → 互不覆蓋。"""
    w = _two_workers(tmp_path)
    assert w["eth"].state_path != w["sol"].state_path
    assert w["eth"].state_path.endswith("bot_state_eth.json")
    assert w["sol"].close_flag_path.endswith("close_request_sol.flag")
    assert w["eth"].close_event is not w["sol"].close_event


def test_route_get_health_always_ok(tmp_path):
    status, body = MM.route_get(_two_workers(tmp_path), "/health")
    assert status == 200
    assert json.loads(body) == {"ok": True}


def test_route_get_state_reads_own_file(tmp_path):
    workers = _two_workers(tmp_path)
    with open(workers["eth"].state_path, "w") as f:
        json.dump({"symbol": "ETHUSDT", "direction": 1}, f)
    with open(workers["sol"].state_path, "w") as f:
        json.dump({"symbol": "SOLUSDT", "direction": -1}, f)
    s_eth, b_eth = MM.route_get(workers, "/eth/state")
    s_sol, b_sol = MM.route_get(workers, "/sol/state")
    assert s_eth == 200 and json.loads(b_eth)["symbol"] == "ETHUSDT"
    assert s_sol == 200 and json.loads(b_sol)["symbol"] == "SOLUSDT"


def test_route_get_state_missing_file_returns_empty_obj(tmp_path):
    workers = _two_workers(tmp_path)
    status, body = MM.route_get(workers, "/eth/state")   # 檔還沒寫
    assert status == 200 and json.loads(body) == {}


def test_route_get_unknown_id_404(tmp_path):
    status, _ = MM.route_get(_two_workers(tmp_path), "/btc/state")
    assert status == 404


def test_route_get_trades_filters_by_strategy_and_symbol(tmp_path, monkeypatch):
    """/{id}/trades 必須用「該台」strategy+symbol 過濾（兩台同策略，只靠 symbol 區分）。"""
    captured = {}
    def fake_read(limit=50, mode=None, strategy=None, symbol=None, **kw):
        captured["strategy"] = strategy
        captured["symbol"] = symbol
        captured["limit"] = limit
        return [{"side": "entry", "symbol": symbol}]
    monkeypatch.setattr("core.trade_journal.read_trades_db", fake_read)
    workers = _two_workers(tmp_path)
    status, body = MM.route_get(workers, "/sol/trades?limit=7")
    assert status == 200
    assert captured["strategy"] == "trend_pullback"
    assert captured["symbol"] == "SOLUSDT"      # 關鍵：sol 路由 → SOLUSDT，不是 ETH
    assert captured["limit"] == 7
    assert json.loads(body)[0]["symbol"] == "SOLUSDT"


def test_route_get_live_enriches_state(tmp_path, monkeypatch):
    """GET /{id}/live → bot_live_status enrich（含統計），用該台 strategy+symbol。"""
    captured = {}
    def fake_live(state, strategy, symbol, interval, **kw):
        captured.update(strategy=strategy, symbol=symbol, interval=interval,
                        in_pos=state.get("in_position"))
        return {"active": True, "symbol": symbol, "strategy": strategy, "realized_pnl": 5.0}
    monkeypatch.setattr("core.live_status.bot_live_status", fake_live)
    workers = _two_workers(tmp_path)
    with open(workers["sol"].state_path, "w") as f:
        f.write('{"in_position": true, "direction": 1}')
    status, body = MM.route_get(workers, "/sol/live")
    assert status == 200
    assert captured["strategy"] == "trend_pullback" and captured["symbol"] == "SOLUSDT"
    assert captured["in_pos"] is True
    assert json.loads(body)["realized_pnl"] == 5.0


def test_route_get_klines_returns_chart_json(tmp_path, monkeypatch):
    """GET /klines?symbol=&interval=&limit= → 呼叫輕量 chart_data.klines_data 回圖表 JSON。"""
    captured = {}
    def fake_klines(symbol="BTCUSDT", interval="4h", limit=200, source="testnet"):
        captured.update(symbol=symbol, interval=interval, limit=limit, source=source)
        return {"candles": [{"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]}
    monkeypatch.setattr("core.chart_data.klines_data", fake_klines)
    workers = _two_workers(tmp_path)
    status, body = MM.route_get(workers, "/klines?symbol=BTCUSDT&interval=15m&limit=120")
    assert status == 200
    assert captured == {"symbol": "BTCUSDT", "interval": "15m", "limit": 120, "source": "testnet"}
    assert json.loads(body)["candles"][0]["close"] == 1.5


def test_route_get_klines_error_returns_empty_candles(tmp_path, monkeypatch):
    """klines_data 拋錯（如網路失敗）→ 200 + 空 candles，不讓前端 500。"""
    def boom(**kw):
        raise RuntimeError("network down")
    monkeypatch.setattr("core.chart_data.klines_data", boom)
    status, body = MM.route_get(_two_workers(tmp_path), "/klines?symbol=BTCUSDT")
    assert status == 200
    assert json.loads(body)["candles"] == []


def test_route_get_markers_returns_aggregated(tmp_path, monkeypatch):
    """GET /markers?symbol=&bucket_hours=&limit= → chart_data.trade_markers 聚合標記。"""
    captured = {}
    def fake_markers(symbol="BTCUSDT", bucket_hours=6, limit=5000, **kw):
        captured.update(symbol=symbol, bucket_hours=bucket_hours, limit=limit)
        return {"markers": [{"time": 1, "price": 100.0}], "bots": []}
    monkeypatch.setattr("core.chart_data.trade_markers", fake_markers)
    status, body = MM.route_get(_two_workers(tmp_path), "/markers?symbol=ETHUSDT&bucket_hours=3&limit=99")
    assert status == 200
    assert captured == {"symbol": "ETHUSDT", "bucket_hours": 3, "limit": 99}
    assert json.loads(body)["markers"][0]["price"] == 100.0


def test_route_get_ma6_returns_six_line_overlay(tmp_path, monkeypatch):
    """GET /ma6?symbol=&interval=&limit= → 呼叫輕量 chart_data.ma6_overlay_data 回六線圖表 JSON。"""
    captured = {}
    def fake_ma6(symbol="BTCUSDT", interval="4h", limit=300, source="testnet"):
        captured.update(symbol=symbol, interval=interval, limit=limit, source=source)
        return {"candles": [], "ma20": [], "ma6_signals": []}
    monkeypatch.setattr("core.chart_data.ma6_overlay_data", fake_ma6)
    workers = _two_workers(tmp_path)
    status, body = MM.route_get(workers, "/ma6?symbol=LINKUSDT&interval=4h&limit=250")
    assert status == 200
    assert captured == {"symbol": "LINKUSDT", "interval": "4h", "limit": 250, "source": "testnet"}
    assert "ma6_signals" in json.loads(body)


def test_route_get_ma6_error_returns_empty_candles(tmp_path, monkeypatch):
    def boom(**kw):
        raise RuntimeError("network down")
    monkeypatch.setattr("core.chart_data.ma6_overlay_data", boom)
    status, body = MM.route_get(_two_workers(tmp_path), "/ma6?symbol=BTCUSDT")
    assert status == 200
    assert json.loads(body)["candles"] == []


def test_route_post_close_writes_only_target_flag(tmp_path):
    """POST /eth/close 只寫 eth 旗標 + set eth event，絕不誤觸 sol。"""
    workers = _two_workers(tmp_path)
    status, payload = MM.route_post(workers, "/eth/close",
                                    token_header="secret", env_token="secret")
    assert status == 200 and payload["ok"] is True
    assert os.path.exists(workers["eth"].close_flag_path)
    assert not os.path.exists(workers["sol"].close_flag_path)
    assert workers["eth"].close_event.is_set()
    assert not workers["sol"].close_event.is_set()


def test_route_post_close_rejects_bad_token(tmp_path):
    workers = _two_workers(tmp_path)
    status, payload = MM.route_post(workers, "/eth/close",
                                    token_header="wrong", env_token="secret")
    assert status == 403 and payload["ok"] is False
    assert not os.path.exists(workers["eth"].close_flag_path)
    assert not workers["eth"].close_event.is_set()


def test_route_post_close_disabled_when_no_env_token(tmp_path):
    workers = _two_workers(tmp_path)
    status, payload = MM.route_post(workers, "/eth/close",
                                    token_header="anything", env_token="")
    assert status == 403 and payload["ok"] is False


def test_route_post_close_unknown_id_404(tmp_path):
    status, _ = MM.route_post(_two_workers(tmp_path), "/btc/close",
                              token_header="secret", env_token="secret")
    assert status == 404


# ── 向後相容根路由：無 id 前綴 → 第一台 bot（既有指向 root 的 dashboard URL 不壞）──
def test_route_get_root_state_serves_first_bot(tmp_path):
    workers = _two_workers(tmp_path)            # 插入序：eth 在前
    with open(workers["eth"].state_path, "w") as f:
        json.dump({"symbol": "ETHUSDT"}, f)
    status, body = MM.route_get(workers, "/state")
    assert status == 200 and json.loads(body)["symbol"] == "ETHUSDT"


def test_route_get_root_trades_serves_first_bot(tmp_path, monkeypatch):
    captured = {}
    def fake_read(limit=50, mode=None, strategy=None, symbol=None, **kw):
        captured["symbol"] = symbol
        return []
    monkeypatch.setattr("core.trade_journal.read_trades_db", fake_read)
    MM.route_get(_two_workers(tmp_path), "/trades?limit=5")
    assert captured["symbol"] == "ETHUSDT"      # 第一台


def test_route_post_root_close_targets_first_bot(tmp_path):
    workers = _two_workers(tmp_path)
    status, payload = MM.route_post(workers, "/close",
                                    token_header="secret", env_token="secret")
    assert status == 200 and payload["ok"] is True
    assert os.path.exists(workers["eth"].close_flag_path)        # 第一台
    assert not os.path.exists(workers["sol"].close_flag_path)


def test_route_get_root_state_empty_when_no_workers():
    status, body = MM.route_get({}, "/state")
    assert status == 200 and json.loads(body) == {}


# ── 任務 #74：監督執行緒 + 崩潰隔離 ────────────────────────────────────
import threading


class _StubCfg:
    def __init__(self, symbol="ETHUSDT", interval="1h", poll=30):
        self.symbol = symbol
        self.interval = interval
        self.poll_seconds = poll


def test_poll_loop_swallows_transient_errors(tmp_path, monkeypatch):
    """單輪拋錯（網路抖動）→ 吞掉、續跑，不中斷迴圈（與單台主迴圈韌性一致）。"""
    import pandas as pd
    workers = _two_workers(tmp_path)
    w = workers["eth"]
    calls = {"fetch": 0, "bars": 0}

    def fake_fetch(client, symbol, interval, n, futures=True):
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise RuntimeError("transient network blip")
        idx = pd.date_range("2026-06-30", periods=3, freq="1h")
        return pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    monkeypatch.setattr(MM, "fetch_klines", fake_fetch)

    class FakeTrader:
        _profit_above_since = None
        def on_bar_close(self, bt): calls["bars"] += 1
        def _heartbeat(self, p=None): pass
        def manual_close(self): return {"ok": True}
        def check_profit_floor(self, p): return False

    stop = threading.Event()
    def sleep_fn(secs, st, ev=None):
        if calls["fetch"] >= 2:
            st.set()
    MM.poll_loop(w, FakeTrader(), None, _StubCfg(), stop, sleep_fn, lambda *a: None)
    assert calls["fetch"] >= 2     # 第一次拋錯後仍續跑
    assert calls["bars"] == 1      # 第二次成功觸發決策


def test_poll_loop_processes_close_flag(tmp_path, monkeypatch):
    """偵測到 close 旗標 → 呼叫 manual_close 並刪旗標。"""
    import pandas as pd
    workers = _two_workers(tmp_path)
    w = workers["eth"]
    with open(w.close_flag_path, "w") as f:
        f.write("now")
    closed = {"n": 0}

    def fake_fetch(client, symbol, interval, n, futures=True):
        idx = pd.date_range("2026-06-30", periods=3, freq="1h")
        return pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    monkeypatch.setattr(MM, "fetch_klines", fake_fetch)

    class FakeTrader:
        _profit_above_since = None
        def on_bar_close(self, bt): pass
        def _heartbeat(self, p=None): pass
        def manual_close(self): closed["n"] += 1; return {"ok": True}
        def check_profit_floor(self, p): return False

    stop = threading.Event()
    MM.poll_loop(w, FakeTrader(), None, _StubCfg(), stop,
                 lambda secs, st, ev=None: st.set(), lambda *a: None)
    assert closed["n"] == 1
    assert not os.path.exists(w.close_flag_path)   # 旗標已消費


def test_supervise_restarts_on_crash(tmp_path):
    """run 階段拋致命錯 → 監督層 backoff 後重建，restarts/last_error 記錄。"""
    w = _two_workers(tmp_path)["eth"]
    stop = threading.Event()
    runs = {"n": 0}

    def build_fn(worker): return ("trader", "client", "cfg")
    def run_fn(worker, trader, client, cfg, st, sleep, log):
        runs["n"] += 1
        raise RuntimeError("crash")
    def sleep_fn(secs, st, ev=None):
        if runs["n"] >= 3:
            st.set()
    MM.supervise(w, build_fn, run_fn, stop, sleep_fn, lambda *a: None, max_backoff=0.001)
    assert runs["n"] >= 3
    assert w.restarts >= 3
    assert w.last_error is not None


def test_supervise_retries_failed_init(tmp_path):
    """build（init）連續拋錯 → 不放棄，持續重試（API 暫時不可用情境）。"""
    w = _two_workers(tmp_path)["eth"]
    stop = threading.Event()
    builds = {"n": 0}

    def build_fn(worker):
        builds["n"] += 1
        if builds["n"] >= 3:
            stop.set()
        raise RuntimeError("API down")
    def run_fn(*a): pass
    MM.supervise(w, build_fn, run_fn, stop, lambda *a, **k: None,
                 lambda *a: None, max_backoff=0.001)
    assert builds["n"] >= 3        # 連續重試，未放棄


def test_supervisor_crash_isolation_concurrent(tmp_path):
    """一台不斷崩潰，另一台在獨立執行緒持續推進、完全不受影響。"""
    workers = _two_workers(tmp_path)
    stop = threading.Event()
    cB = {"n": 0}

    def build_fn(w): return ("t", "c", "cfg")
    def run_crash(w, *a): raise RuntimeError("A down")
    def run_count(w, t, c, cfg, st, sleep, log):
        while not st.is_set():
            cB["n"] += 1
            if cB["n"] >= 20:
                st.set()
            sleep(0.001, st)
    def sleep_fn(secs, st, ev=None): st.wait(min(secs, 0.005))

    tA = threading.Thread(target=MM.supervise,
                          args=(workers["eth"], build_fn, run_crash, stop, sleep_fn, lambda *a: None),
                          kwargs={"max_backoff": 0.001}, daemon=True)
    tB = threading.Thread(target=MM.supervise,
                          args=(workers["sol"], build_fn, run_count, stop, sleep_fn, lambda *a: None),
                          daemon=True)
    tA.start(); tB.start()
    tB.join(timeout=5); stop.set(); tA.join(timeout=5)
    assert cB["n"] >= 20                       # B 在 A 崩潰期間照常推進
    assert workers["eth"].restarts >= 1        # A 確實被重啟過
    assert not tB.is_alive()


# ── 委派：run_live_futures 偵測 BOTS_CONFIG → 走多 bot 監督器 ───────────
def test_single_main_delegates_to_multi_when_bots_config_set(monkeypatch):
    """設了 BOTS_CONFIG → run_live_futures.main() 委派 run_multi_futures.main()，
    不走單台路徑（同一 start command 即可跑合併 service，純 env var 切換）。"""
    monkeypatch.setenv("BOTS_CONFIG",
                       '[{"id":"x","symbol":"ETHUSDT","strategy":"trend_pullback","interval":"1h"}]')
    called = {"n": 0}
    monkeypatch.setattr(MM, "main", lambda: called.__setitem__("n", called["n"] + 1))
    M.main()
    assert called["n"] == 1


def _make_trader_flags(state_path, **flags):
    cfg = Config(); cfg.symbol = "SOLUSDT"; cfg.strategy = "fib_channel"
    return M.FuturesLiveTrader(cfg, None, ScriptStrat(), RiskOfficer(cfg),
                               FakeExecu(), FakeJournal(), state_path=str(state_path), **flags)


def test_dcg_enabled_param_overrides_env(tmp_path, monkeypatch):
    """合併進程：DCG 須 per-bot 可設，不被共用 env 綁死。

    無 env 但參數 True → 啟用；env=1 但參數 False → 停用（Bot2 的 DCG 不波及他台）。
    """
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    monkeypatch.delenv("DCG_ENABLED", raising=False)
    assert _make_trader_flags(tmp_path / "a.json", dcg_enabled=True)._dcg.enabled is True
    monkeypatch.setenv("DCG_ENABLED", "1")
    assert _make_trader_flags(tmp_path / "b.json", dcg_enabled=False)._dcg.enabled is False


def test_dcg_enabled_none_falls_back_to_env(tmp_path, monkeypatch):
    """未指定（None）→ 退回 os.getenv（單台行為完全不變）。"""
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    monkeypatch.setenv("DCG_ENABLED", "1")
    assert _make_trader_flags(tmp_path / "c.json", dcg_enabled=None)._dcg.enabled is True


def test_exchange_stop_param_overrides_env(tmp_path, monkeypatch):
    """exchange_stop 同樣 per-bot 可設（未來各台可不同），預設退回 env。"""
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    monkeypatch.delenv("EXCHANGE_STOP_ENABLED", raising=False)
    assert _make_trader_flags(tmp_path / "d.json", exchange_stop_enabled=True)._exchange_stop is True
    monkeypatch.setenv("EXCHANGE_STOP_ENABLED", "1")
    assert _make_trader_flags(tmp_path / "e.json", exchange_stop_enabled=False)._exchange_stop is False


def test_single_main_no_delegation_without_bots_config(monkeypatch):
    """未設 BOTS_CONFIG → 不委派（單台行為不變）。短路單台路徑驗證未進多 bot。"""
    monkeypatch.delenv("BOTS_CONFIG", raising=False)
    monkeypatch.setattr("sys.argv", ["run_live_futures.py"])   # 避免 argparse 吃到 pytest argv
    called = {"n": 0}
    monkeypatch.setattr(MM, "main", lambda: called.__setitem__("n", called["n"] + 1))
    # 讓單台路徑在 _start_state_server 處以 SystemExit 跳出，避免 while True 卡住測試
    def _boom(): raise SystemExit
    monkeypatch.setattr(M, "_start_state_server", _boom)
    with pytest.raises(SystemExit):
        M.main()
    assert called["n"] == 0          # 未委派多 bot


# ── 決策路徑修補：ML 過濾旗標 + 重啟不重複決策同一根 K 棒 ────────────────────

def test_resolve_ml_path_disabled_by_conf():
    """conf ml_filter:false → 回空字串（不載未驗證模型；錦標賽驗證不含 ML 層）。"""
    assert MM.resolve_ml_path({"ml_filter": False}, "smc_structure") == ""


def test_resolve_ml_path_default_uses_strategy_model(monkeypatch):
    monkeypatch.delenv("ML_FILTER_PATH", raising=False)
    assert MM.resolve_ml_path({}, "smc_structure") == "models/smc_structure.pkl"


def test_resolve_ml_path_env_override(monkeypatch):
    monkeypatch.setenv("ML_FILTER_PATH", "custom/path.pkl")
    assert MM.resolve_ml_path({}, "whatever") == "custom/path.pkl"
    # 顯式 false 優先於 env
    assert MM.resolve_ml_path({"ml_filter": False}, "whatever") == ""


def test_poll_loop_skips_bar_already_decided_before_restart(tmp_path, monkeypatch):
    """重啟後：restored_last_bar() 回傳「已決策過的 bar」→ 同 bar 不再 on_bar_close
    （只刷心跳），避免重啟重複進出、白繳手續費。"""
    import pandas as pd
    workers = _two_workers(tmp_path)
    w = workers["eth"]
    idx = pd.date_range("2026-06-30", periods=3, freq="1h")

    def fake_fetch(client, symbol, interval, n, futures=True):
        return pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    monkeypatch.setattr(MM, "fetch_klines", fake_fetch)

    calls = {"bars": 0, "hb": 0}

    class FakeTrader:
        _profit_above_since = None
        def on_bar_close(self, bt): calls["bars"] += 1
        def _heartbeat(self, p=None): calls["hb"] += 1
        def manual_close(self): return {"ok": True}
        def check_profit_floor(self, p): return False
        def restored_last_bar(self): return idx[-2]      # state 檔說這根已決策過

    stop = threading.Event()
    MM.poll_loop(w, FakeTrader(), None, _StubCfg(), stop,
                 lambda secs, st, ev=None: st.set(), lambda *a: None)
    assert calls["bars"] == 0      # 同根不重複決策
    assert calls["hb"] == 1        # 只刷心跳


def test_poll_loop_decides_when_no_restored_bar(tmp_path, monkeypatch):
    """restored_last_bar() 回 None（fresh 啟動）→ 正常決策。"""
    import pandas as pd
    workers = _two_workers(tmp_path)
    w = workers["eth"]
    idx = pd.date_range("2026-06-30", periods=3, freq="1h")
    monkeypatch.setattr(MM, "fetch_klines",
                        lambda *a, **k: pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx))
    calls = {"bars": 0}

    class FakeTrader:
        _profit_above_since = None
        def on_bar_close(self, bt): calls["bars"] += 1
        def _heartbeat(self, p=None): pass
        def manual_close(self): return {"ok": True}
        def check_profit_floor(self, p): return False
        def restored_last_bar(self): return None

    stop = threading.Event()
    MM.poll_loop(w, FakeTrader(), None, _StubCfg(), stop,
                 lambda secs, st, ev=None: st.set(), lambda *a: None)
    assert calls["bars"] == 1


def test_route_get_bots_lists_ids_in_order(tmp_path):
    """GET /bots → 依設定順序列出所有 bot id + symbol/strategy/interval（前端動態渲染 N 台用）。"""
    workers = _two_workers(tmp_path)
    status, body = MM.route_get(workers, "/bots")
    assert status == 200
    bots = json.loads(body)
    assert [b["id"] for b in bots] == ["eth", "sol"]
    assert bots[0]["symbol"] == "ETHUSDT" and bots[0]["strategy"] == "trend_pullback"
    assert bots[0]["interval"] == "1h"


# ── CORS 預檢（OPTIONS）：直連 bot 平倉需要瀏覽器 preflight 通過 ──────────────

def test_cors_preflight_headers_allow_close_token_header():
    """OPTIONS 預檢回應必須宣告允許 POST 方法 + X-Close-Token 自訂標頭，
    否則瀏覽器會擋掉跨網域帶自訂 header 的 POST（GitHub Pages 直連 bot 平倉會失敗）。"""
    status, headers = MM.cors_preflight_response()
    assert status == 204
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "POST" in headers["Access-Control-Allow-Methods"]
    assert "x-close-token" in headers["Access-Control-Allow-Headers"].lower()


# ── BOTS_CONFIG 出場參數逐台覆蓋（2026-07-05）：apply_risk_overrides 白名單設 cfg ──
def test_apply_risk_overrides_sets_whitelisted_fields():
    cfg = Config()
    assert cfg.tp_R_mult == 2.0                       # 預設
    MM.apply_risk_overrides(cfg, {"tp_R_mult": 3.0, "use_fixed_tp": False,
                                  "atr_mult_sl": 1.5, "chand_mult": 4.0})
    assert cfg.tp_R_mult == 3.0
    assert cfg.use_fixed_tp is False
    assert cfg.atr_mult_sl == 1.5
    assert cfg.chand_mult == 4.0


def test_apply_risk_overrides_ignores_non_whitelisted():
    """非白名單鍵（防注入亂設 cfg，例如金鑰欄）一律忽略、不拋例外。"""
    cfg = Config()
    original_key = cfg.futures_api_key
    MM.apply_risk_overrides(cfg, {"futures_api_key": "HACKED", "tp_R_mult": 3.0})
    assert cfg.futures_api_key == original_key        # 未被亂改
    assert cfg.tp_R_mult == 3.0                        # 白名單的仍生效


def test_apply_risk_overrides_none_or_empty_is_noop():
    cfg = Config()
    MM.apply_risk_overrides(cfg, None)
    MM.apply_risk_overrides(cfg, {})
    assert cfg.tp_R_mult == 2.0                         # 不變


def test_parse_bots_config_preserves_risk_block():
    raw = json.dumps([{"id": "b1", "symbol": "BTCUSDT", "strategy": "smc_structure",
                       "interval": "4h", "risk": {"tp_R_mult": 3.0}}])
    bots = MM.parse_bots_config(raw)
    assert bots[0]["risk"] == {"tp_R_mult": 3.0}
