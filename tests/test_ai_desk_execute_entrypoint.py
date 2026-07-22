"""run_ai_desk_execute 測試 — 總開關 + build_engines，全離線（假 client）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_ai_desk_execute import build_engines, main


def _exchange_info(symbols):
    return {
        "symbols": [{
            "symbol": s,
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        } for s in symbols],
    }


class FakeClient:
    def __init__(self, symbols):
        self._symbols = symbols
        self.leverage_calls = []

    def futures_exchange_info(self):
        return _exchange_info(self._symbols)

    def futures_change_leverage(self, **kw):
        self.leverage_calls.append(kw)


def test_build_engines_one_per_symbol():
    client = FakeClient(["ETHUSDT", "BTCUSDT"])
    engines = build_engines(client, ["ETHUSDT", "BTCUSDT"])
    assert set(engines) == {"ETHUSDT", "BTCUSDT"}
    assert engines["ETHUSDT"].symbol == "ETHUSDT"
    assert client.leverage_calls == []          # set_leverage=False，不改動槓桿設定


def test_main_does_nothing_when_disabled(monkeypatch, capsys):
    monkeypatch.delenv("AI_DESK_EXEC_ENABLED", raising=False)
    main()                                       # 不應建立任何 client 連線、不應報錯
    out = capsys.readouterr().out
    assert "未開啟" in out


def test_main_disabled_is_case_and_value_strict(monkeypatch, capsys):
    monkeypatch.setenv("AI_DESK_EXEC_ENABLED", "1")   # 非 "true" 一律視為關閉
    main()
    assert "未開啟" in capsys.readouterr().out
