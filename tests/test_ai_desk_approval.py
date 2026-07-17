"""ai_desk.approval 測試 — SQLite 狀態機：pending→approved/rejected→executed。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.approval import ApprovalStore
from ai_desk.proposal import TradeProposal


def make_proposal(direction=1):
    return TradeProposal(symbol="BTCUSDT", ts="2026-07-17T08:00:00Z",
                         direction=direction, confidence=0.6,
                         entry=63500.0, stop=62800.0, take_profit=65000.0,
                         rationale="測試提案")


@pytest.fixture
def store(tmp_path):
    return ApprovalStore(str(tmp_path / "test.db"))


def test_add_creates_pending(store):
    pid = store.add(make_proposal(), qty=0.05, debate_full_text="完整辯論全文")
    rows = store.pending()
    assert len(rows) == 1
    assert rows[0]["id"] == pid
    assert rows[0]["status"] == "pending"
    assert rows[0]["qty"] == pytest.approx(0.05)
    assert rows[0]["rationale"] == "測試提案"


def test_approve_moves_to_approved_unexecuted(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    assert store.pending() == []
    rows = store.approved_unexecuted()
    assert len(rows) == 1 and rows[0]["id"] == pid


def test_reject_removes_from_both_queues(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.reject(pid)
    assert store.pending() == []
    assert store.approved_unexecuted() == []


def test_cannot_approve_twice(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    with pytest.raises(ValueError, match="pending"):
        store.approve(pid)


def test_cannot_execute_pending(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    with pytest.raises(ValueError, match="approved"):
        store.mark_executed(pid)


def test_executed_leaves_unexecuted_queue(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    store.mark_executed(pid)
    assert store.approved_unexecuted() == []


def test_debate_full_text_persisted(store):
    pid = store.add(make_proposal(), 0.05, "四角色完整辯論全文…")
    row = store.get(pid)
    assert "四角色完整辯論全文" in row["debate_full_text"]
