"""實盤稽核工具（research/scratchpad/live_trade_audit.py）可重跑化的純函式測試。

稽核腳本原本只讀 2026-07-06 凍結的 JSON 快照。要能「定期自動對照」，需要：
  - 自己從 bot /{id}/trades 公開端點抓最新成交（fetch_live_trades，網路注入可測）
  - 產出機器可讀的實盤 vs 回測對照（reconciliation_summary）
本測試檔驗證這些純邏輯（網路以假 opener 注入，不打真端點）。
"""
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "research", "scratchpad", "live_trade_audit.py")


def _load():
    spec = importlib.util.spec_from_file_location("live_trade_audit", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


audit = _load()


# ── URL 組裝 ──────────────────────────────────────────────────────────
def test_bot_trades_url_builds_and_strips_slash():
    assert audit.bot_trades_url("https://x.up.railway.app/", "b1", 200) == \
        "https://x.up.railway.app/b1/trades?limit=200"


def test_bot_trades_url_coerces_limit_int():
    assert audit.bot_trades_url("https://x", "b9", "50") == "https://x/b9/trades?limit=50"


# ── /trades 回應解析 ──────────────────────────────────────────────────
def test_parse_trades_response_plain_list():
    raw = json.dumps([{"side": "entry", "price": 1.0}]).encode()
    assert audit.parse_trades_response(raw) == [{"side": "entry", "price": 1.0}]


def test_parse_trades_response_dict_wrapped():
    raw = json.dumps({"trades": [{"side": "exit_tp", "pnl": 3}]}).encode()
    assert audit.parse_trades_response(raw) == [{"side": "exit_tp", "pnl": 3}]


def test_parse_trades_response_garbage_and_empty_are_safe():
    assert audit.parse_trades_response(b"not json") == []
    assert audit.parse_trades_response(None) == []
    assert audit.parse_trades_response(b"") == []


# ── 抓取並寫快照（網路注入）───────────────────────────────────────────
def test_fetch_live_trades_writes_snapshots(tmp_path):
    class _Resp:
        def __init__(self, payload): self._p = payload
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._p

    calls = []

    def fake_opener(url, timeout=None):
        calls.append(url)
        bot = url.split("/")[-1].split("?")[0] if False else url.rsplit("/", 2)[1]
        payload = {"b1": [{"side": "entry", "price": 1.0}],
                   "b2": []}[bot]
        return _Resp(json.dumps(payload).encode())

    counts = audit.fetch_live_trades("https://host", ["b1", "b2"],
                                     str(tmp_path), limit=100, opener=fake_opener)
    assert counts == {"b1": 1, "b2": 0}
    assert calls == ["https://host/b1/trades?limit=100",
                     "https://host/b2/trades?limit=100"]
    saved = json.load(open(tmp_path / "b1_trades.json"))
    assert saved == [{"side": "entry", "price": 1.0}]


def test_fetch_live_trades_records_none_on_network_error(tmp_path):
    def boom(url, timeout=None):
        raise OSError("connection refused")
    counts = audit.fetch_live_trades("https://host", ["b1"], str(tmp_path), opener=boom)
    assert counts == {"b1": None}
    assert not os.path.exists(tmp_path / "b1_trades.json")   # 失敗不寫壞快照


# ── 實盤 vs 回測機器可讀對照 ──────────────────────────────────────────
def test_reconciliation_summary_aggregates_per_bot_and_total():
    rts = [
        {"bot": "b1", "pnl": 10.0}, {"bot": "b1", "pnl": -4.0},
        {"bot": "b2", "pnl": 2.0},
    ]
    bt = {"b1": [{"ts": "t1"}, {"ts": "t2"}, {"ts": "t3"}], "b2": []}
    s = audit.reconciliation_summary(rts, bt)
    assert s["b1"] == {"live_trips": 2, "live_pnl": 6.0, "bt_entries": 3}
    assert s["b2"] == {"live_trips": 1, "live_pnl": 2.0, "bt_entries": 0}
    assert s["_total"] == {"live_trips": 3, "live_pnl": 8.0, "bt_entries": 3}


def test_reconciliation_summary_includes_bot_with_only_backtest():
    # 回測有進場但實盤 0 回合（例如剛上線）→ 仍要出現在對照
    s = audit.reconciliation_summary([], {"b3": [{"ts": "t"}]})
    assert s["b3"] == {"live_trips": 0, "live_pnl": 0.0, "bt_entries": 1}
