"""跨 bot 投資組合風控 — 共用 DB 控制同向暴露上限。

三台 bot 共用同一個 PostgreSQL（或 SQLite fallback）。
開倉前呼叫 check_exposure()；開倉後呼叫 upsert_position()；平倉後呼叫 clear_position()。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

_DATABASE_URL = os.getenv("DATABASE_URL")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS open_positions (
    strategy   TEXT PRIMARY KEY,
    symbol     TEXT,
    direction  INTEGER,
    qty        REAL,
    price      REAL,
    notional   REAL,
    updated_at TEXT
)
"""


class PortfolioGuard:
    def __init__(self, db_path: str = "trades.db", database_url: Optional[str] = None):
        self._url = database_url or _DATABASE_URL
        self._db_path = db_path
        self._init_db()

    # ── 內部 ──────────────────────────────────────────────────────────────

    def _conn(self):
        if self._url:
            import psycopg2
            return psycopg2.connect(self._url)
        return sqlite3.connect(self._db_path)

    def _ph(self) -> str:
        return "%s" if self._url else "?"

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_SQL)

    # ── 公開 API ──────────────────────────────────────────────────────────

    def upsert_position(self, strategy: str, symbol: str, direction: int,
                        qty: float, price: float) -> None:
        """寫入（或更新）bot 的開倉資訊。"""
        from datetime import datetime, timezone
        notional = abs(qty * price)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ph = self._ph()
        sql = f"""
            INSERT INTO open_positions (strategy, symbol, direction, qty, price, notional, updated_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})
            ON CONFLICT(strategy) DO UPDATE SET
                symbol=excluded.symbol, direction=excluded.direction,
                qty=excluded.qty, price=excluded.price,
                notional=excluded.notional, updated_at=excluded.updated_at
        """
        with self._conn() as conn:
            conn.execute(sql, (strategy, symbol, direction, qty, price, notional, now))

    def clear_position(self, strategy: str) -> None:
        """平倉後移除該 bot 的持倉記錄。"""
        ph = self._ph()
        with self._conn() as conn:
            conn.execute(f"DELETE FROM open_positions WHERE strategy = {ph}", (strategy,))

    def get_positions(self) -> list[dict]:
        """回傳所有 bot 目前的開倉記錄。"""
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT strategy, symbol, direction, qty, price, notional FROM open_positions"
            )
            rows = cur.fetchall()
        return [
            {"strategy": r[0], "symbol": r[1], "direction": r[2],
             "qty": r[3], "price": r[4], "notional": r[5]}
            for r in rows
        ]

    def check_exposure(
        self,
        own_strategy: str,
        direction: int,
        new_notional: float,
        max_notional: float,
    ) -> tuple[bool, str]:
        """開倉前呼叫：檢查同向暴露是否超過上限。

        own_strategy 的舊持倉（若有）不計入同向暴露（避免自身干擾）。
        回傳 (allow, reason)。
        """
        positions = self.get_positions()
        same_dir = sum(
            p["notional"]
            for p in positions
            if p["strategy"] != own_strategy and p["direction"] == direction
        )
        total = same_dir + new_notional
        if total > max_notional:
            return False, (f"跨 bot 同向暴露超過上限：{total:.0f} > {max_notional:.0f} USDT")
        return True, "ok"
