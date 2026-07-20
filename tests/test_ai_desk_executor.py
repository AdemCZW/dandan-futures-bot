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
