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


def test_cannot_place_pending(store):
    """未核准的提案不可掛單——人工核准是硬閘門。"""
    pid = store.add(make_proposal(), 0.05, "全文")
    with pytest.raises(ValueError, match="approved"):
        store.mark_placed(pid, "OID-1")


def test_placed_leaves_approved_queue(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    store.mark_placed(pid, "OID-1")
    assert store.approved_unexecuted() == []
    assert store.get(pid)["exchange_order_id"] == "OID-1"


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


# ── Phase 2 訂單生命週期 ────────────────────────────────────
def test_full_lifecycle_to_closed_target(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    store.mark_placed(pid, "OID-9")
    store.mark_filled(pid)
    assert store.get(pid)["status"] == "filled"
    assert store.get(pid)["filled_at"] is not None
    store.mark_closed(pid, "closed_target", realized_pnl=12.5)
    row = store.get(pid)
    assert row["status"] == "closed_target"
    assert row["realized_pnl"] == 12.5
    assert row["closed_at"] is not None


def test_placed_can_expire_when_never_filled(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid); store.mark_placed(pid, "OID-2")
    store.mark_expired(pid)
    assert store.get(pid)["status"] == "expired"


def test_cannot_fill_before_placed(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    with pytest.raises(ValueError, match="placed"):
        store.mark_filled(pid)


def test_cannot_close_before_filled(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid); store.mark_placed(pid, "OID-3")
    with pytest.raises(ValueError, match="filled"):
        store.mark_closed(pid, "closed_stop", realized_pnl=-5.0)


def test_mark_closed_rejects_unknown_state(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid); store.mark_placed(pid, "OID-4"); store.mark_filled(pid)
    with pytest.raises(ValueError, match="closed_"):
        store.mark_closed(pid, "closed_whatever", realized_pnl=0.0)


def test_by_status_filters(store):
    a = store.add(make_proposal(), 0.05, "全文")
    b = store.add(make_proposal(), 0.05, "全文")
    store.approve(a); store.mark_placed(a, "OID-5")
    assert [r["id"] for r in store.by_status("placed")] == [a]
    assert [r["id"] for r in store.by_status("pending")] == [b]
    assert sorted(r["id"] for r in store.by_status("placed", "pending")) == [a, b]


def test_reject_also_works_from_approved(store):
    """approved 但過期太久還沒掛單 → 允許撤回核准。"""
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    store.reject(pid)
    assert store.get(pid)["status"] == "rejected"


def test_reject_fails_from_placed(store):
    """已經掛出去的單不能用 reject 撤——那要走 mark_expired/mark_closed。"""
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    store.mark_placed(pid, "OID-1")
    with pytest.raises(ValueError):
        store.reject(pid)


# ── judge_hedges 觀察欄位（2026-08-06）────────────────────
#
# 純觀察指標：記錄裁判提了幾種顧慮，跟著新樣本一起累積，供日後與進場區位一併
# 檢驗。刻意不接進任何執行邏輯——見 ai_desk/hedge_signal.py 的三點理由。

def test_add_records_judge_hedges(tmp_path):
    store = ApprovalStore(str(tmp_path / "p.db"))
    p = TradeProposal("BTCUSDT", "t", -1, 0.6, 100.0, 105.0, 90.0, "測試")
    pid = store.add(p, 1.0, "全文", judge_hedges=3)
    assert store.get(pid)["judge_hedges"] == 3


def test_judge_hedges_defaults_to_none_when_not_given(tmp_path):
    """沒給就是 None（未記錄），不可用 0 冒充「真的沒有警告語」——
    與 model / realized_pnl 同一條「不假造已知」的慣例。"""
    store = ApprovalStore(str(tmp_path / "p.db"))
    p = TradeProposal("BTCUSDT", "t", -1, 0.6, 100.0, 105.0, 90.0, "測試")
    pid = store.add(p, 1.0, "全文")
    assert store.get(pid)["judge_hedges"] is None


def test_judge_hedges_column_added_to_existing_db(tmp_path):
    """舊資料庫開啟時自動補欄位（走既有 _ADDED_COLUMNS 遷移），不需手動改 schema。"""
    db = str(tmp_path / "old.db")
    store = ApprovalStore(db)
    p = TradeProposal("BTCUSDT", "t", -1, 0.6, 100.0, 105.0, 90.0, "測試")
    pid = store.add(p, 1.0, "全文")
    import sqlite3
    with sqlite3.connect(db) as c:                     # 模擬舊 schema：把欄位砍掉
        c.execute("ALTER TABLE ai_desk_proposals DROP COLUMN judge_hedges")
    reopened = ApprovalStore(db)                        # 重開應自動補回
    assert reopened.get(pid)["judge_hedges"] is None
