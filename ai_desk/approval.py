"""人工核准閘門 + 訂單生命週期 — 借用 OpenAlice「Trading as Git」概念。

狀態機：
    pending ──► approved ──► placed ──► filled ──► closed_stop
        └────► rejected        │                └► closed_target
                               │                └► closed_manual
                               └────────────────► expired（掛單逾時未成交）

人工核准是硬閘門：只有 approved 能進入 placed（真的掛單）。每次轉移都是單一
guarded UPDATE，來源狀態不符即拋錯，因此不可能重複核准或跳過關卡。

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
    model TEXT,
    exchange_order_id TEXT,
    filled_at TEXT,
    closed_at TEXT,
    realized_pnl REAL,
    judge_hedges INTEGER
)
"""

_COLS = ["id", "symbol", "ts", "direction", "confidence", "entry", "stop",
         "take_profit", "qty", "rationale", "debate_full_text", "status",
         "created_at", "decided_at", "model",
         "exchange_order_id", "filled_at", "closed_at", "realized_pnl",
         "judge_hedges"]


# 舊資料庫可能缺這些欄位，開啟時逐一補上（順序即新增順序）
_ADDED_COLUMNS = [
    ("model", "TEXT"),
    ("exchange_order_id", "TEXT"),
    ("filled_at", "TEXT"),
    ("closed_at", "TEXT"),
    ("realized_pnl", "REAL"),
    ("judge_hedges", "INTEGER"),
]

# Phase 2 訂單生命週期：
#   pending → approved → placed → filled → closed_stop/closed_target/closed_manual
#                          └────────────────────────────────► expired（掛單逾時未成交）
CLOSED_STATES = frozenset({"closed_stop", "closed_target", "closed_manual"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalStore:
    def __init__(self, db_path: str = "ai_desk_proposals.db"):
        self.db_path = db_path
        with self._conn() as c:
            c.execute(_SCHEMA)
            # 舊資料庫缺欄位 → 逐一補上（既有列留 NULL，代表當時沒記錄、不假裝知道）
            cols = {r[1] for r in c.execute("PRAGMA table_info(ai_desk_proposals)")}
            for name, decl in _ADDED_COLUMNS:
                if name not in cols:
                    c.execute(f"ALTER TABLE ai_desk_proposals ADD COLUMN {name} {decl}")

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def add(self, proposal: TradeProposal, qty: float,
            debate_full_text: str, model: str | None = None,
            judge_hedges: int | None = None) -> int:
        """model：產生這筆提案的 LLM 模型。留 None 代表未記錄（舊樣本），不可假裝知道。

        judge_hedges：裁判提了幾種自我警告（見 ai_desk.hedge_signal）。純觀察欄位，
        不影響任何執行邏輯。同樣地，沒給就留 None（未記錄），不可用 0 冒充
        「真的一個警告語都沒有」——兩者的統計意義完全不同。
        """
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_desk_proposals "
                "(symbol, ts, direction, confidence, entry, stop, take_profit,"
                " qty, rationale, debate_full_text, status, created_at, model,"
                " judge_hedges) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
                (proposal.symbol, proposal.ts, proposal.direction,
                 proposal.confidence, proposal.entry, proposal.stop,
                 proposal.take_profit, qty, proposal.rationale,
                 debate_full_text, _utc_now(), model, judge_hedges))
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

    def _transition(self, pid: int, from_status: str, to_status: str,
                    extra: dict | None = None) -> None:
        """單一 UPDATE 同時驗證來源狀態並寫入欄位（原子性，防重複轉移）。"""
        sets = ["status = ?", "decided_at = ?"]
        vals = [to_status, _utc_now()]
        for k, v in (extra or {}).items():
            sets.append(f"{k} = ?")
            vals.append(v)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE ai_desk_proposals SET {', '.join(sets)} "
                "WHERE id = ? AND status = ?",
                (*vals, pid, from_status))
            if cur.rowcount != 1:
                raise ValueError(
                    f"提案 id={pid} 不在 {from_status} 狀態，無法轉為 {to_status}")

    def approve(self, pid: int) -> None:
        self._transition(pid, "pending", "approved")

    def reject(self, pid: int) -> None:
        """撤回一筆提案。pending（從未核准）或 approved（核准後過期太久還沒掛單）皆可撤回；
        placed 之後已經在交易所掛出委託，不可再用 reject（改用 mark_expired/mark_closed）。"""
        row = self.get(pid)
        if row["status"] not in ("pending", "approved"):
            raise ValueError(
                f"提案 id={pid} 狀態為 {row['status']}，不可撤回"
                "（僅 pending/approved 可 reject；已掛單請用 mark_expired/mark_closed）")
        self._transition(pid, row["status"], "rejected")

    # ── Phase 2 訂單生命週期 ──────────────────────────────
    def mark_placed(self, pid: int, exchange_order_id: str) -> None:
        """已在交易所掛出限價進場單。只有 approved 能進來——人工核准是硬閘門。"""
        self._transition(pid, "approved", "placed",
                         extra={"exchange_order_id": str(exchange_order_id)})

    def mark_filled(self, pid: int) -> None:
        """限價單成交。"""
        self._transition(pid, "placed", "filled", extra={"filled_at": _utc_now()})

    def mark_expired(self, pid: int) -> None:
        """掛單逾時未成交、已撤單。"""
        self._transition(pid, "placed", "expired", extra={"closed_at": _utc_now()})

    def mark_closed(self, pid: int, state: str, realized_pnl: float | None) -> None:
        """部位平倉。state 必須是 CLOSED_STATES 之一。

        realized_pnl=None：無法可靠算出（例如裸倉緊急平倉），留 NULL 待對帳補正，
        不可用 0.0 這種假數字冒充「無損益」。
        """
        if state not in CLOSED_STATES:
            raise ValueError(f"平倉狀態必須是 {sorted(CLOSED_STATES)}，收到 {state}")
        pnl = None if realized_pnl is None else float(realized_pnl)
        self._transition(pid, "filled", state,
                         extra={"closed_at": _utc_now(), "realized_pnl": pnl})

    def by_status(self, *statuses: str) -> list:
        """依狀態查詢（可多個）。"""
        if not statuses:
            return []
        marks = ", ".join("?" for _ in statuses)
        return self._rows(f"status IN ({marks})", tuple(statuses))


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
