"""人工核准閘門 — 借用 OpenAlice「Trading as Git」概念的最簡版本。

狀態機：pending → approved | rejected；approved → executed。
只有 approved 的提案才可能被送進執行層（Phase 2 接線）。

CLI 小工具：
    python -m ai_desk.approval              # 列出待核准提案（含辯論全文）
    python -m ai_desk.approval 3 approve    # 核准 id=3
    python -m ai_desk.approval 3 reject     # 拒絕 id=3
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from ai_desk.proposal import TradeProposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_desk_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    direction INTEGER NOT NULL,
    confidence REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    take_profit REAL NOT NULL,
    qty REAL NOT NULL,
    rationale TEXT NOT NULL,
    debate_full_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    model TEXT
)
"""

_COLS = ["id", "symbol", "ts", "direction", "confidence", "entry", "stop",
         "take_profit", "qty", "rationale", "debate_full_text", "status",
         "created_at", "decided_at", "model"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalStore:
    def __init__(self, db_path: str = "ai_desk_proposals.db"):
        self.db_path = db_path
        with self._conn() as c:
            c.execute(_SCHEMA)
            # 舊資料庫沒有 model 欄位 → 補上（既有列留 NULL，代表當時沒記錄、查不回來）
            cols = [r[1] for r in c.execute("PRAGMA table_info(ai_desk_proposals)")]
            if "model" not in cols:
                c.execute("ALTER TABLE ai_desk_proposals ADD COLUMN model TEXT")

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def add(self, proposal: TradeProposal, qty: float,
            debate_full_text: str, model: str | None = None) -> int:
        """model：產生這筆提案的 LLM 模型。留 None 代表未記錄（舊樣本），不可假裝知道。"""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_desk_proposals "
                "(symbol, ts, direction, confidence, entry, stop, take_profit,"
                " qty, rationale, debate_full_text, status, created_at, model) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?)",
                (proposal.symbol, proposal.ts, proposal.direction,
                 proposal.confidence, proposal.entry, proposal.stop,
                 proposal.take_profit, qty, proposal.rationale,
                 debate_full_text, _utc_now(), model))
            return cur.lastrowid

    def _rows(self, where: str, args=()) -> list:
        with self._conn() as c:
            cur = c.execute(
                f"SELECT {', '.join(_COLS)} FROM ai_desk_proposals "
                f"WHERE {where} ORDER BY id", args)
            return [dict(zip(_COLS, r)) for r in cur.fetchall()]

    def get(self, pid: int) -> dict:
        rows = self._rows("id = ?", (pid,))
        if not rows:
            raise ValueError(f"找不到提案 id={pid}")
        return rows[0]

    def pending(self) -> list:
        return self._rows("status = 'pending'")

    def all(self) -> list:
        """全部提案（含已核准/已拒絕/已執行），供結果追蹤用。"""
        return self._rows("1 = 1")

    def approved_unexecuted(self) -> list:
        return self._rows("status = 'approved'")

    def _transition(self, pid: int, from_status: str, to_status: str) -> None:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE ai_desk_proposals SET status = ?, decided_at = ? "
                "WHERE id = ? AND status = ?",
                (to_status, _utc_now(), pid, from_status))
            if cur.rowcount != 1:
                raise ValueError(
                    f"提案 id={pid} 不在 {from_status} 狀態，無法轉為 {to_status}")

    def approve(self, pid: int) -> None:
        self._transition(pid, "pending", "approved")

    def reject(self, pid: int) -> None:
        self._transition(pid, "pending", "rejected")

    def mark_executed(self, pid: int) -> None:
        self._transition(pid, "approved", "executed")


def _cli(argv):
    store = ApprovalStore()
    if len(argv) == 0:
        rows = store.pending()
        if not rows:
            print("（無待核准提案）")
            return
        dir_txt = {1: "做多", -1: "做空"}
        for r in rows:
            print("=" * 60)
            print(f"提案 #{r['id']}  {r['symbol']}  {dir_txt.get(r['direction'])}"
                  f"  信心 {r['confidence']:.2f}  數量 {r['qty']}")
            print(f"進場 {r['entry']}  停損 {r['stop']}  停利 {r['take_profit']}")
            print(f"依據：{r['rationale']}")
            print(f"--- 完整辯論 ---\n{r['debate_full_text']}")
        print("=" * 60)
        print("核准：python -m ai_desk.approval <id> approve")
        print("拒絕：python -m ai_desk.approval <id> reject")
    elif len(argv) == 2 and argv[1] in ("approve", "reject"):
        pid = int(argv[0])
        getattr(store, argv[1])(pid)
        print(f"提案 #{pid} 已{'核准' if argv[1] == 'approve' else '拒絕'}")
    else:
        print("用法：python -m ai_desk.approval [<id> approve|reject]")
        sys.exit(2)


if __name__ == "__main__":
    _cli(sys.argv[1:])
