"""run_ai_desk_once 測試 — 只測純函式 prepare_df（設索引+丟未收盤根），不碰網路。"""
import os
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
