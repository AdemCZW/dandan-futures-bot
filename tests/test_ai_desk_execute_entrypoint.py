"""run_ai_desk_execute 測試 — 總開關 + build_engines，全離線（假 client）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_ai_desk_execute import build_engines, main, report_result


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


# ── 錯誤回報：run_execution_pass 收集到的 errors 必須被看見 ──────
#
# 之前 -4045 的例外會直接中止整輪，所以錯誤自然「很大聲」。改成隔離後若不主動
# 印出來，就會變成靜默失敗——排程 log 只會看到「本輪結束」，比原本更難察覺。

def _empty_result(**over):
    base = {"warnings": [], "placed": [], "skipped": [], "filled": [],
            "expired": [], "errors": []}
    base.update(over)
    return base


def test_report_result_returns_zero_when_clean(capsys):
    assert report_result(_empty_result(placed=[1])) == 0
    assert "新掛單：[1]" in capsys.readouterr().out


def test_report_result_prints_errors_and_returns_nonzero(capsys):
    result = _empty_result(
        filled=[7],
        errors=["#8 ETHUSDT 處理掛單失敗：掛停損失敗（APIError(code=-4045)...），"
                "為避免裸倉已立刻市價平倉 #8"])

    code = report_result(result)

    out = capsys.readouterr().out
    assert code != 0                      # 排程/launchd 記得到這輪有問題
    assert "-4045" in out                 # 原因要看得到，不可只說「有錯誤」
    assert "#8" in out
    assert "本輪成交：[7]" in out          # 其他項目照樣回報，不因有錯就不印
