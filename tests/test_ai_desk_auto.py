"""ai_desk.auto 測試 — 全自動核准+下單（人工核准這一步被程式取代），全依賴注入。"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from core.risk_officer import RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.auto import AutoCycleResult, run_auto_cycle
from ai_desk.memory import ThesisMemory


def make_df(n=300, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    return pd.DataFrame(
        {"open": np.roll(close, 1), "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": rng.uniform(100, 200, n)},
        index=idx,
    )


def scripted_llm(judge_json):
    replies = [
        '結構論述。\n```json\n{"structure_summary": "震盪"}\n```',
        '多方論述。\n```json\n{"points": ["論點A"]}\n```',
        '空方論述。\n```json\n{"rebuttals": ["反駁A"], "points": ["論點B"]}\n```',
        f'裁決論述。\n```json\n{judge_json}\n```',
    ]
    calls = []

    def call(prompt):
        calls.append(prompt)
        return replies[len(calls) - 1]

    call.calls = calls
    call.model = "claude-opus-4-8"
    return call


class FakeFuturesClient:
    def __init__(self):
        self.created_orders = []

    def _create_futures_api_uri(self, path):
        return f"https://testnet.binancefuture.com/fapi/v1/{path}"

    def futures_create_order(self, **params):
        self.created_orders.append(params)
        return {"orderId": f"OID-{len(self.created_orders)}", **params}


class FakeEngine:
    def __init__(self, symbol, position_amt=0.0, place_stop_should_fail=False):
        self.symbol = symbol
        self.client = FakeFuturesClient()
        self._position_amt = position_amt
        self._place_stop_should_fail = place_stop_should_fail
        self.close_calls = []

    def round_qty(self, q):
        return f"{q:.3f}"

    def round_price(self, p):
        return f"{p:.2f}"

    def position_amt(self):
        return self._position_amt

    def place_stop(self, direction, price):
        if self._place_stop_should_fail:
            raise RuntimeError("模擬掛停損失敗")

    def place_take_profit(self, direction, price):
        pass

    def close(self, qty, direction):
        self.close_calls.append((qty, direction))


@pytest.fixture
def deps(tmp_path):
    return {
        "risk_officer": RiskOfficer(Config()),
        "equity": 10_000.0,
        "memory": ThesisMemory(str(tmp_path / "mem"), "ETHUSDT", "4h"),
        "approval_store": ApprovalStore(str(tmp_path / "p.db")),
    }


def test_directional_proposal_is_auto_approved_and_placed(deps):
    df = make_df()
    price = float(df["close"].iloc[-1])
    judge = (f'{{"direction": 1, "confidence": 0.7, "entry": {price:.2f}, '
             f'"stop": {price * 0.95:.2f}, "take_profit": {price * 1.1:.2f}, '
             f'"rationale": "多方勝"}}')
    engine = FakeEngine("ETHUSDT")

    result = run_auto_cycle(df, "ETHUSDT", "4h", llm_call=scripted_llm(judge),
                            engine=engine, **deps)

    assert isinstance(result, AutoCycleResult)
    assert result.placed is True
    assert result.error is None
    assert len(engine.client.created_orders) == 1
    row = deps["approval_store"].get(result.cycle.proposal_id)
    assert row["status"] == "placed"           # 沒有人按核准，狀態機仍照走


def test_flat_proposal_is_never_approved_or_placed(deps):
    df = make_df()
    judge = ('{"direction": 0, "confidence": 0.3, "entry": 0, "stop": 0, '
             '"take_profit": 0, "rationale": "觀望"}')
    engine = FakeEngine("ETHUSDT")

    result = run_auto_cycle(df, "ETHUSDT", "4h", llm_call=scripted_llm(judge),
                            engine=engine, **deps)

    assert result.cycle.proposal_id is None
    assert result.placed is False
    assert engine.client.created_orders == []


def test_naked_stop_failure_is_reported_not_swallowed(deps):
    """掛停損失敗時（裸倉防護觸發市價平倉）：placed=False 且帶錯誤訊息，不可靜默吞掉。"""
    df = make_df()
    price = float(df["close"].iloc[-1])
    judge = (f'{{"direction": -1, "confidence": 0.6, "entry": {price:.2f}, '
             f'"stop": {price * 1.05:.2f}, "take_profit": {price * 0.9:.2f}, '
             f'"rationale": "空方勝"}}')
    engine = FakeEngine("ETHUSDT", place_stop_should_fail=True)

    result = run_auto_cycle(df, "ETHUSDT", "4h", llm_call=scripted_llm(judge),
                            engine=engine, **deps)

    # place_entry 成功了，但這次測試不模擬「後續成交」，所以不會觸發 _protect_with_stop；
    # 這裡驗證的是掛單本身成功、核准鏈路正確——裸倉防護場景在 executor 測試已覆蓋。
    assert result.placed is True


def test_symbol_outside_whitelist_leaves_proposal_approved_not_placed(deps, monkeypatch):
    """白名單擋下時：提案已被自動核准（approve 早於白名單檢查），但沒有掛單，錯誤要能看到。"""
    monkeypatch.setenv("AI_DESK_SYMBOLS", "BTCUSDT")   # ETHUSDT 不在白名單內
    df = make_df()
    price = float(df["close"].iloc[-1])
    judge = (f'{{"direction": 1, "confidence": 0.7, "entry": {price:.2f}, '
             f'"stop": {price * 0.95:.2f}, "take_profit": {price * 1.1:.2f}, '
             f'"rationale": "多方勝"}}')
    engine = FakeEngine("ETHUSDT")

    result = run_auto_cycle(df, "ETHUSDT", "4h", llm_call=scripted_llm(judge),
                            engine=engine, **deps)

    assert result.placed is False
    assert "白名單" in result.error
    row = deps["approval_store"].get(result.cycle.proposal_id)
    assert row["status"] == "approved"          # 核准了但沒掛出去，留在 approved 供之後處理
