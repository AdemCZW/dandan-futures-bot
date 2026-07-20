"""ai_desk.executor 測試 — 安全護欄（白名單 / testnet 驗證），全離線。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.executor import allowed_symbols, assert_symbol_allowed, assert_testnet


# ── 幣種白名單 ─────────────────────────────────────────────
def test_default_whitelist_excludes_bot_symbols(monkeypatch):
    """未設定時保守預設 ETHUSDT，不含 bot 歷史用過的 BTC/SOL/DOT（避免搶同帳戶部位）。"""
    monkeypatch.delenv("AI_DESK_SYMBOLS", raising=False)
    s = allowed_symbols()
    assert s == frozenset({"ETHUSDT"})
    assert "BTCUSDT" not in s


def test_whitelist_parses_comma_list(monkeypatch):
    monkeypatch.setenv("AI_DESK_SYMBOLS", "ethusdt, BTCUSDT ,  linkusdt")
    assert allowed_symbols() == frozenset({"ETHUSDT", "BTCUSDT", "LINKUSDT"})


def test_empty_whitelist_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("AI_DESK_SYMBOLS", "   ")
    assert allowed_symbols() == frozenset({"ETHUSDT"})


def test_assert_symbol_allowed_passes(monkeypatch):
    monkeypatch.setenv("AI_DESK_SYMBOLS", "ETHUSDT")
    assert_symbol_allowed("ETHUSDT")          # 不應拋錯


def test_assert_symbol_allowed_rejects_outsider(monkeypatch):
    monkeypatch.setenv("AI_DESK_SYMBOLS", "ETHUSDT")
    with pytest.raises(ValueError, match="白名單"):
        assert_symbol_allowed("BTCUSDT")


def test_assert_symbol_allowed_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("AI_DESK_SYMBOLS", "ETHUSDT")
    assert_symbol_allowed("ethusdt")


# ── testnet 強制驗證 ────────────────────────────────────────
class FakeClient:
    def __init__(self, uri):
        self._uri = uri

    def _create_futures_api_uri(self, path):
        return self._uri


def test_assert_testnet_accepts_testnet_uri():
    assert_testnet(FakeClient("https://testnet.binancefuture.com/fapi/v1/order"))


def test_assert_testnet_rejects_mainnet_uri():
    """Client.FUTURES_URL 常數會顯示主網位址具誤導性，必須驗證實際組出的請求網址。"""
    with pytest.raises(RuntimeError, match="testnet"):
        assert_testnet(FakeClient("https://fapi.binance.com/fapi/v1/order"))


def test_assert_testnet_rejects_client_without_uri_builder():
    class Bare:
        pass

    with pytest.raises(RuntimeError, match="testnet"):
        assert_testnet(Bare())


# ── 限價進場單參數 ──────────────────────────────────────────
from ai_desk.executor import (  # noqa: E402
    ACTION_EXPIRE, ACTION_FILL, ACTION_PLACE, ACTION_SKIP_POSITION, ACTION_WAIT,
    decide_action, is_expired, limit_order_params,
)


def _rq(q):
    return f"{q:.3f}"


def _rp(p):
    return f"{p:.2f}"


def test_limit_params_short_is_sell_limit_gtc():
    p = limit_order_params("ETHUSDT", -1, 1.6172, 1855.0, round_qty=_rq, round_price=_rp)
    assert p["symbol"] == "ETHUSDT"
    assert p["side"] == "SELL"           # 空單掛賣出限價
    assert p["type"] == "LIMIT"
    assert p["timeInForce"] == "GTC"
    assert p["quantity"] == "1.617"      # 經過交易所精度處理
    assert p["price"] == "1855.00"


def test_limit_params_long_is_buy_limit():
    p = limit_order_params("ETHUSDT", 1, 2.0, 1800.0, round_qty=_rq, round_price=_rp)
    assert p["side"] == "BUY"


def test_limit_params_rejects_flat_direction():
    with pytest.raises(ValueError, match="方向"):
        limit_order_params("ETHUSDT", 0, 1.0, 100.0, round_qty=_rq, round_price=_rp)


# ── 掛單逾時 ────────────────────────────────────────────────
from datetime import datetime, timedelta, timezone  # noqa: E402

T0 = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


def test_not_expired_within_ttl():
    assert is_expired(T0.isoformat(), now=T0 + timedelta(hours=23), ttl_hours=24) is False


def test_expired_past_ttl():
    assert is_expired(T0.isoformat(), now=T0 + timedelta(hours=25), ttl_hours=24) is True


# ── 動作決策 ────────────────────────────────────────────────
def test_approved_with_no_position_places():
    assert decide_action(status="approved", has_position=False) == ACTION_PLACE


def test_approved_but_position_open_skips():
    """已有部位 → 不加碼、不反手。"""
    assert decide_action(status="approved", has_position=True) == ACTION_SKIP_POSITION


def test_placed_and_filled_marks_fill():
    assert decide_action(status="placed", has_position=True,
                         order_status="FILLED") == ACTION_FILL


def test_placed_still_open_within_ttl_waits():
    assert decide_action(status="placed", has_position=False, order_status="NEW",
                         placed_at=T0.isoformat(), now=T0 + timedelta(hours=2),
                         ttl_hours=24) == ACTION_WAIT


def test_placed_still_open_past_ttl_expires():
    assert decide_action(status="placed", has_position=False, order_status="NEW",
                         placed_at=T0.isoformat(), now=T0 + timedelta(hours=30),
                         ttl_hours=24) == ACTION_EXPIRE


def test_placed_but_order_gone_expires():
    """交易所說已撤/已過期 → 以交易所為準。"""
    for st in ("CANCELED", "EXPIRED", "REJECTED"):
        assert decide_action(status="placed", has_position=False,
                             order_status=st) == ACTION_EXPIRE


def test_other_statuses_wait():
    assert decide_action(status="pending", has_position=False) == ACTION_WAIT
    assert decide_action(status="filled", has_position=True) == ACTION_WAIT
