"""ai_desk.webview 測試 — 注入假 cycle_fn + 同步 spawn + tmp DB，不碰網路/CLI/LLM。"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.risk_officer import RiskDecision

from ai_desk.approval import ApprovalStore
from ai_desk.desk import CycleResult
from ai_desk.proposal import TradeProposal
from ai_desk.webview import create_app


def fake_cycle(symbol, interval, on_progress):
    """假的一輪：依序回報四角色，回傳一個做空提案。"""
    for role in ("analyst", "bull", "bear", "judge"):
        on_progress(role, f"{role} 的完整論述")
    proposal = TradeProposal(symbol=symbol, ts="2026-07-17T08:00:00Z",
                             direction=-1, confidence=0.6, entry=63200.0,
                             stop=64150.0, take_profit=61300.0, rationale="測試提案")
    risk = RiskDecision(True, 0.047, "ok")
    return CycleResult(proposal=proposal, risk=risk, proposal_id=1, debate={})


def sync_spawn(fn):
    """測試用：同步執行，讓 POST 回來時 run 已完成。"""
    fn()


@pytest.fixture
def client(tmp_path):
    db = str(tmp_path / "p.db")
    app = create_app(store_path=db, cycle_fn=fake_cycle, spawn=sync_spawn)
    c = TestClient(app)
    c._db = db
    return c


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ai_desk" in r.text.lower()


def test_run_lifecycle_reports_roles_and_proposal(client):
    r = client.post("/api/run", json={"symbol": "BTCUSDT", "interval": "4h"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    got = client.get(f"/api/run/{run_id}").json()
    assert got["status"] == "done"
    assert set(got["roles"]) == {"analyst", "bull", "bear", "judge"}
    assert "空方" not in got["roles"]["bear"]              # 內容是我們注入的假文
    assert got["roles"]["bear"] == "bear 的完整論述"
    assert got["proposal"]["direction"] == -1
    assert got["proposal"]["entry"] == 63200.0
    assert got["risk"]["allow"] is True
    assert got["proposal_id"] == 1


def test_run_unknown_id_404(client):
    assert client.get("/api/run/nope").status_code == 404


def test_pending_then_approve(client):
    store = ApprovalStore(client._db)
    p = TradeProposal("BTCUSDT", "t", -1, 0.6, 63200.0, 64150.0, 61300.0, "測試")
    pid = store.add(p, 0.05, "四角色全文")

    lst = client.get("/api/pending").json()
    assert len(lst) == 1 and lst[0]["id"] == pid

    assert client.post(f"/api/approve/{pid}").json()["ok"] is True
    assert client.get("/api/pending").json() == []          # 核准後離開待辦

    # 重複核准 → 400
    assert client.post(f"/api/approve/{pid}").status_code == 400


def test_reject_removes_from_pending(client):
    store = ApprovalStore(client._db)
    p = TradeProposal("ETHUSDT", "t", 1, 0.7, 3000.0, 2900.0, 3200.0, "測試")
    pid = store.add(p, 0.5, "全文")
    assert client.post(f"/api/reject/{pid}").json()["ok"] is True
    assert client.get("/api/pending").json() == []
