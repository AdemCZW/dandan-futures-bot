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


def test_all_returns_every_proposal_regardless_of_status(store):
    a = store.add(make_proposal(), 0.05, "全文A")
    b = store.add(make_proposal(direction=1), 0.06, "全文B")
    store.approve(a)
    store.reject(b)
    rows = store.all()
    assert [r["id"] for r in rows] == [a, b]           # 含已核准與已拒絕
    assert {r["status"] for r in rows} == {"approved", "rejected"}


def test_add_records_model(store):
    pid = store.add(make_proposal(), 0.05, "全文", model="claude-opus-4-8")
    assert store.get(pid)["model"] == "claude-opus-4-8"


def test_add_without_model_is_none(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    assert store.get(pid)["model"] is None


def test_migrates_old_db_missing_model_column(tmp_path):
    """舊資料庫沒有 model 欄位 → 開啟時自動補上，既有列讀得到（值為 None）。"""
    import sqlite3
    db = str(tmp_path / "old.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE ai_desk_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, ts TEXT NOT NULL,
        direction INTEGER NOT NULL, confidence REAL NOT NULL, entry REAL NOT NULL,
        stop REAL NOT NULL, take_profit REAL NOT NULL, qty REAL NOT NULL,
        rationale TEXT NOT NULL, debate_full_text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, decided_at TEXT)""")
    con.execute("INSERT INTO ai_desk_proposals (symbol,ts,direction,confidence,entry,stop,"
                "take_profit,qty,rationale,debate_full_text,created_at) "
                "VALUES ('BTCUSDT','t',-1,0.5,100,110,80,1.0,'舊','全文','2026-07-17T00:00:00+00:00')")
    con.commit(); con.close()

    s = ApprovalStore(db)                       # 開啟時應自動 migrate
    rows = s.all()
    assert len(rows) == 1
    assert rows[0]["model"] is None             # 舊樣本查不回模型，誠實留空
