"""run_ai_desk_once 測試 — 只測純函式 prepare_df（設索引+丟未收盤根），不碰網路。"""
import os
import re
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_ai_desk_once import prepare_df


def test_prepare_df_sets_index_and_drops_open_bar():
    n = 10
    idx = pd.date_range("2026-07-01", periods=n, freq="4h", name="open_time")
    raw = pd.DataFrame({"open": np.ones(n), "high": np.ones(n), "low": np.ones(n),
                        "close": np.ones(n), "volume": np.ones(n)}, index=idx)
    df = prepare_df(raw)
    assert len(df) == n - 1                          # 最後一根（未收盤）被丟掉
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index[-1] == idx[-2]


def test_auto_approve_enabled_defaults_to_false(monkeypatch):
    from run_ai_desk_once import auto_approve_enabled
    monkeypatch.delenv("AI_DESK_AUTO_APPROVE", raising=False)
    assert auto_approve_enabled() is False


def test_auto_approve_enabled_requires_exact_true(monkeypatch):
    from run_ai_desk_once import auto_approve_enabled
    monkeypatch.setenv("AI_DESK_AUTO_APPROVE", "1")
    assert auto_approve_enabled() is False
    monkeypatch.setenv("AI_DESK_AUTO_APPROVE", "true")
    assert auto_approve_enabled() is True
    monkeypatch.setenv("AI_DESK_AUTO_APPROVE", "TRUE")
    assert auto_approve_enabled() is True


def test_fetch_live_price_returns_float():
    from run_ai_desk_once import fetch_live_price

    class FakeClient:
        def futures_symbol_ticker(self, symbol):
            return {"symbol": symbol, "price": "63205.70"}

    assert fetch_live_price(FakeClient(), "BTCUSDT") == 63205.70


def test_fetch_live_price_returns_none_on_failure_not_crash():
    """抓現價失敗只是少了附註，不該讓整輪分析掛掉（排程無人值守時尤其重要）。"""
    from run_ai_desk_once import fetch_live_price

    class BoomClient:
        def futures_symbol_ticker(self, symbol):
            raise RuntimeError("網路斷線")

    assert fetch_live_price(BoomClient(), "BTCUSDT") is None


# ── 真實帳戶淨值（取代寫死的 EQUITY_FOR_SIZING = 10_000）──────────
#
# 實測帳戶只有約 4458，卻拿 10,000 去算倉位大小，導致每筆名目大了 2.3 倍，
# 可用保證金常被榨乾。改成跟 run_live_futures.py 既有作法一致：
# 讀 futures_account_balance() 裡的 USDT 餘額（core/futures_execution_engineer.py
# 的 balance() 方法就是這樣寫的，這裡只是不需要整個 engine 就能單獨呼叫）。

def test_fetch_account_equity_returns_real_balance():
    from run_ai_desk_once import fetch_account_equity

    class FakeClient:
        def futures_account_balance(self):
            return [{"asset": "BNB", "balance": "0.00100000"},
                    {"asset": "USDT", "balance": "4458.08000000"}]

    assert fetch_account_equity(FakeClient()) == 4458.08


def test_fetch_account_equity_missing_asset_raises_not_silent_zero():
    """找不到資產寧可拋錯，也不要靜默回 0（0 會被誤讀成『真的沒錢』而不是『查詢失敗』）。"""
    from run_ai_desk_once import fetch_account_equity

    class FakeClient:
        def futures_account_balance(self):
            return [{"asset": "BNB", "balance": "0.001"}]

    with pytest.raises(ValueError, match="USDT"):
        fetch_account_equity(FakeClient())


def test_no_hardcoded_equity_constant_left():
    """EQUITY_FOR_SIZING 這個寫死常數必須整個移除，不留著當死碼或後備值。"""
    import run_ai_desk_once
    assert not hasattr(run_ai_desk_once, "EQUITY_FOR_SIZING")


def test_kline_limit_is_enough_for_ma200():
    """MA200 需 200 天 = 1200 根 4h；抓太少會讓該層永遠「資料不足」。"""
    import re
    for path in ("run_ai_desk_once.py", "ai_desk/webview.py"):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for m in re.finditer(r"fetch_klines\([^)]*limit=(\d+)[^)]*interval|"
                             r"fetch_klines\(\s*\w+,\s*symbol,\s*interval,\s*limit=(\d+)", src):
            got = int(m.group(1) or m.group(2))
            assert got >= 1200, f"{path} 抓 {got} 根，不足以算 MA200（需 1200）"


# ── Client 建構不得做強制 ping（2026-08-17）─────────────────
#
# 實際事故：記憶汙染修復後的 62 輪排程裡，25 輪（40%）是「跑起來後失敗」而非
# AI 判斷觀望。根因是 python-binance 1.0.37 的 Client.__init__ 預設 ping=True，
# 建構時強制做一次健康檢查；而 testnet=True 時那個 ping 打的是【現貨】測試網
# testnet.binance.vision——ai_desk 用的是【合約】測試網 testnet.binancefuture.com，
# 完全不需要現貨主機。那個 DNS 間歇性解析失敗時，整輪就 exit 1。
#
# 排程完整性本身是好的（應跑 30 輪、實跑 30 輪、零漏跑），純粹是被這個無關的
# 健康檢查殺掉。ping=False 建構耗時 0.000s、且 _create_futures_api_uri 仍正確。

_AI_DESK_CLIENT_FILES = ("run_ai_desk_once.py", "run_ai_desk_execute.py",
                         "ai_desk/webview.py")


def _client_constructions(src: str):
    """找出所有 binance Client(...) 的建構呼叫（含跨行），排除註解與假物件。"""
    out = []
    for m in re.finditer(r"(?<![A-Za-z_.])Client\s*\(", src):
        start = m.start()
        line_start = src.rfind("\n", 0, start) + 1
        if src[line_start:start].lstrip().startswith("#"):
            continue
        # 從左括號往後配對到對應的右括號
        i = src.index("(", start)
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(src[start:j + 1])
                    break
    return out


def test_every_ai_desk_client_disables_startup_ping():
    """ai_desk 執行路徑上每個 Client( 都必須帶 ping=False。

    漏掉任何一個，那一輪就多一個「跟本輪要做的事無關、卻能殺掉整輪」的
    網路依賴。新增 Client 建構時這條測試會提醒你補上。
    """
    missing = []
    for rel in _AI_DESK_CLIENT_FILES:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        for call in _client_constructions(src):
            if "ping=False" not in call.replace(" ", ""):
                missing.append(f"{rel}: {call[:70]}")
    assert not missing, "以下 Client 建構沒有 ping=False：\n" + "\n".join(missing)


def test_ping_false_client_still_targets_futures_testnet():
    """關掉 ping 不可影響合約測試網路由——這是 assert_testnet 的唯一可信依據。"""
    from binance.client import Client
    c = Client("dummy_key", "dummy_secret", testnet=True, ping=False)
    assert "testnet" in str(c._create_futures_api_uri("order"))
