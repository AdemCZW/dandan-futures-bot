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


# ── 執行編排：place_entry / handle_placed ──────────────────
from ai_desk.approval import ApprovalStore  # noqa: E402
from ai_desk.executor import handle_placed, place_entry  # noqa: E402
from ai_desk.proposal import TradeProposal  # noqa: E402


class FakeFuturesClient:
    """假交易所 client：只實作 executor 會用到的方法，含 testnet URI。"""

    def __init__(self):
        self.created_orders = []
        self.canceled = []

    def _create_futures_api_uri(self, path):
        return f"https://testnet.binancefuture.com/fapi/v1/{path}"

    def futures_create_order(self, **params):
        self.created_orders.append(params)
        return {"orderId": f"OID-{len(self.created_orders)}", **params}


class FakeEngine:
    """假執行引擎：符合 FuturesExecutionEngineer 的公開介面，不碰網路。"""

    def __init__(self, symbol, position_amt=0.0, order_status="NEW",
                place_stop_should_fail=False):
        self.symbol = symbol
        self.client = FakeFuturesClient()
        self._position_amt = position_amt
        self._order_status = order_status
        self._place_stop_should_fail = place_stop_should_fail
        self.stop_calls = []
        self.tp_calls = []
        self.close_calls = []
        self.cancel_calls = []

    def round_qty(self, q):
        return f"{q:.3f}"

    def round_price(self, p):
        return f"{p:.2f}"

    def position_amt(self):
        return self._position_amt

    def get_order(self, order_id):
        return {"orderId": order_id, "status": self._order_status}

    def place_stop(self, direction, price):
        if self._place_stop_should_fail:
            raise RuntimeError("模擬掛停損失敗（如 -4130）")
        self.stop_calls.append((direction, price))

    def place_take_profit(self, direction, price):
        self.tp_calls.append((direction, price))

    def close(self, qty, direction):
        self.close_calls.append((qty, direction))

    def cancel_order(self, order_id):
        self.cancel_calls.append(order_id)


def _fresh_store(tmp_path):
    return ApprovalStore(str(tmp_path / "exec.db"))


def _approved_row(store, symbol="ETHUSDT", direction=-1):
    p = TradeProposal(symbol, "t", direction, 0.6, 1855.0, 1877.0, 1808.0, "測試")
    pid = store.add(p, 1.5, "全文")
    store.approve(pid)
    return store.get(pid)


# place_entry
def test_place_entry_submits_limit_and_marks_placed(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")

    place_entry(row, store, engine)

    assert len(engine.client.created_orders) == 1
    order = engine.client.created_orders[0]
    assert order["type"] == "LIMIT" and order["side"] == "SELL"
    updated = store.get(row["id"])
    assert updated["status"] == "placed"
    assert updated["exchange_order_id"] == "OID-1"


def test_place_entry_rejects_symbol_outside_whitelist(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DESK_SYMBOLS", "ETHUSDT")
    store = _fresh_store(tmp_path)
    row = _approved_row(store, symbol="BTCUSDT")
    engine = FakeEngine("BTCUSDT")

    with pytest.raises(ValueError, match="白名單"):
        place_entry(row, store, engine)
    assert engine.client.created_orders == []          # 沒有送出任何委託
    assert store.get(row["id"])["status"] == "approved"  # 狀態未被改動


def test_place_entry_rejects_non_testnet_client(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")
    engine.client._create_futures_api_uri = lambda path: f"https://fapi.binance.com/fapi/v1/{path}"

    with pytest.raises(RuntimeError, match="testnet"):
        place_entry(row, store, engine)
    assert engine.client.created_orders == []


# handle_placed — 成交 + 裸倉防護
def test_handle_placed_fill_places_stop_and_tp(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")
    place_entry(row, store, engine)
    row = store.get(row["id"])

    engine._position_amt = -1.5           # 成交後交易所顯示有空頭部位
    engine._order_status = "FILLED"
    action = handle_placed(row, store, engine)

    assert action == "fill"
    assert store.get(row["id"])["status"] == "filled"
    assert engine.stop_calls == [(-1, 1877.0)]
    assert engine.tp_calls == [(-1, 1808.0)]


def test_handle_placed_stop_failure_forces_market_close_never_naked(tmp_path):
    """掛停損失敗 → 絕不留裸倉：立刻市價平倉並記錄。"""
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT", place_stop_should_fail=True)
    place_entry(row, store, engine)
    row = store.get(row["id"])

    engine._position_amt = -1.5
    engine._order_status = "FILLED"

    with pytest.raises(RuntimeError, match="裸倉"):
        handle_placed(row, store, engine)

    assert engine.close_calls == [(1.5, -1)]            # 立刻市價平倉
    final = store.get(row["id"])
    assert final["status"] == "closed_manual"            # 不會停在 filled（裸倉）


def test_handle_placed_expired_cancels_and_marks(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")
    place_entry(row, store, engine)
    row = store.get(row["id"])

    engine._order_status = "NEW"
    from datetime import datetime, timedelta, timezone
    future = datetime.now(timezone.utc) + timedelta(hours=30)
    action = handle_placed(row, store, engine, now=future)

    assert action == "expire"
    assert engine.cancel_calls == [row["exchange_order_id"]]
    assert store.get(row["id"])["status"] == "expired"


def test_handle_placed_still_open_waits_and_touches_nothing(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")
    place_entry(row, store, engine)
    row = store.get(row["id"])

    engine._order_status = "NEW"
    action = handle_placed(row, store, engine)

    assert action == "wait"
    assert engine.cancel_calls == [] and engine.stop_calls == []
    assert store.get(row["id"])["status"] == "placed"


# ── 對帳：孤兒部位只告警、不接管 ─────────────────────────────
from ai_desk.executor import reconcile_orphan_positions  # noqa: E402


def test_reconcile_warns_on_untracked_position_does_not_touch(tmp_path):
    """交易所有白名單幣種的部位，但本地沒有 filled 中的提案在追蹤 → 只告警，不平倉不接管。"""
    store = _fresh_store(tmp_path)
    engine = FakeEngine("ETHUSDT", position_amt=2.0)
    warnings = reconcile_orphan_positions({"ETHUSDT": engine}, store)
    assert len(warnings) == 1 and "ETHUSDT" in warnings[0]
    assert engine.close_calls == []            # 沒有任何平倉動作


def test_reconcile_silent_when_position_matches_tracked_filled(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")
    place_entry(row, store, engine)
    row = store.get(row["id"])
    engine._position_amt = -1.5
    engine._order_status = "FILLED"
    handle_placed(row, store, engine)          # 現在有一筆 filled 追蹤這個部位

    warnings = reconcile_orphan_positions({"ETHUSDT": engine}, store)
    assert warnings == []


def test_reconcile_silent_when_no_position(tmp_path):
    store = _fresh_store(tmp_path)
    engine = FakeEngine("ETHUSDT", position_amt=0.0)
    assert reconcile_orphan_positions({"ETHUSDT": engine}, store) == []


# ── handle_placed 容忍「查無此單」（closePosition 單可能被自動撤銷查不到）──
def test_handle_placed_tolerates_order_not_found_as_expired(tmp_path):
    store = _fresh_store(tmp_path)
    row = _approved_row(store)
    engine = FakeEngine("ETHUSDT")
    place_entry(row, store, engine)
    row = store.get(row["id"])

    def _raise(order_id):
        raise RuntimeError("Order does not exist")
    engine.get_order = _raise

    action = handle_placed(row, store, engine)
    assert action == "expire"
    assert store.get(row["id"])["status"] == "expired"


# ── 一輪執行 pass：approved→掛單、placed→處理，全注入 engine ────
from ai_desk.executor import run_execution_pass  # noqa: E402


def test_pass_places_approved_and_handles_placed_together(tmp_path):
    store = _fresh_store(tmp_path)
    a = _approved_row(store, symbol="ETHUSDT")
    engine = FakeEngine("ETHUSDT")

    result = run_execution_pass(store, {"ETHUSDT": engine})

    assert store.get(a["id"])["status"] == "placed"
    assert result["placed"] == [a["id"]]
    assert result["warnings"] == []


def test_pass_skips_approved_when_symbol_already_has_position(tmp_path):
    store = _fresh_store(tmp_path)
    a = _approved_row(store, symbol="ETHUSDT")
    engine = FakeEngine("ETHUSDT", position_amt=1.0)   # 已有別的部位

    result = run_execution_pass(store, {"ETHUSDT": engine})

    assert store.get(a["id"])["status"] == "approved"   # 沒被動
    assert engine.client.created_orders == []
    assert result["skipped"] == [a["id"]]


def test_pass_processes_placed_proposal(tmp_path):
    store = _fresh_store(tmp_path)
    a = _approved_row(store, symbol="ETHUSDT")
    engine = FakeEngine("ETHUSDT")
    place_entry(a, store, engine)
    engine._position_amt = -1.5
    engine._order_status = "FILLED"

    result = run_execution_pass(store, {"ETHUSDT": engine})

    assert store.get(a["id"])["status"] == "filled"
    assert result["filled"] == [a["id"]]


def test_pass_ignores_proposals_outside_given_engines(tmp_path):
    """只處理有傳入 engine 的幣種，其餘（例如白名單外）完全不碰。"""
    store = _fresh_store(tmp_path)
    a = _approved_row(store, symbol="BTCUSDT")
    result = run_execution_pass(store, {"ETHUSDT": FakeEngine("ETHUSDT")})
    assert store.get(a["id"])["status"] == "approved"
    assert result["placed"] == [] and result["skipped"] == []
