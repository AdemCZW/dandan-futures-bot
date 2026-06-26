"""跨 bot 投資組合風控 — 共用 DB 控制同向暴露上限。

三台 bot 共用同一個 PostgreSQL（或 SQLite fallback）。
開倉前呼叫 check_exposure()；開倉後呼叫 upsert_position()；平倉後呼叫 clear_position()。

bot identity = (strategy, symbol)：兩台 bot 可跑同一策略但不同標的（如 fib_channel
分別跑 BTC / SOL），須用 symbol 一併區分，否則會共用同一列互相覆蓋。

所有 DB 操作統一走 cursor（sqlite3 與 psycopg2 都支援 conn.cursor().execute()），
並對外吞掉所有 DB 例外（fail-open：風控出錯絕不讓交易主流程中斷）。
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

_DATABASE_URL = os.getenv("DATABASE_URL")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS open_positions (
    strategy   TEXT,
    symbol     TEXT,
    direction  INTEGER,
    qty        REAL,
    price      REAL,
    notional   REAL,
    updated_at TEXT,
    PRIMARY KEY (strategy, symbol)
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
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(_CREATE_SQL)
                self._migrate_to_composite_key(conn, cur)
        except Exception:
            pass

    def _migrate_to_composite_key(self, conn, cur) -> None:
        """把舊版單一主鍵 (strategy) 的 open_positions 遷移成 (strategy, symbol)。

        舊表讓兩台同策略 bot 共用一列，互相覆蓋。資料是暫態（每次開倉重寫），
        遷移時保留現有列、用 (strategy, symbol) 去重後重建。任何錯誤都吞掉
        （最壞情況丟失暫態追蹤，bot 下次開倉會重新寫入），絕不讓風控初始化中斷。
        """
        try:
            if self._primary_key_cols(cur) == ["strategy", "symbol"]:
                return                                  # 已是新結構
            cur.execute(
                "SELECT strategy, symbol, direction, qty, price, notional, updated_at "
                "FROM open_positions"
            )
            seen, deduped = set(), []
            for r in cur.fetchall():                     # 同 (strategy, symbol) 留最後一筆
                key = (r[0], r[1])
                if key in seen:
                    continue
                seen.add(key); deduped.append(tuple(r))
            cur.execute("DROP TABLE open_positions")
            cur.execute(_CREATE_SQL)
            ph = self._ph()
            if deduped:
                cur.executemany(
                    f"INSERT INTO open_positions "
                    f"(strategy, symbol, direction, qty, price, notional, updated_at) "
                    f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                    deduped,
                )
        except Exception:
            pass

    def _primary_key_cols(self, cur) -> list[str]:
        """回傳 open_positions 目前主鍵的欄位名（依序）。"""
        if self._url:
            cur.execute(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = 'open_positions'::regclass AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)"
            )
            return [r[0] for r in cur.fetchall()]
        # SQLite：PRAGMA table_info 的第 6 欄（pk）>0 代表主鍵順序
        cur.execute("PRAGMA table_info(open_positions)")
        pk = [(row[5], row[1]) for row in cur.fetchall() if row[5] > 0]
        return [name for _, name in sorted(pk)]

    # ── 公開 API ──────────────────────────────────────────────────────────

    def upsert_position(self, strategy: str, symbol: str, direction: int,
                        qty: float, price: float) -> None:
        """寫入（或更新）bot 的開倉資訊。identity = (strategy, symbol)。"""
        from datetime import datetime, timezone
        notional = abs(qty * price)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ph = self._ph()
        sql = f"""
            INSERT INTO open_positions (strategy, symbol, direction, qty, price, notional, updated_at)
            VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})
            ON CONFLICT(strategy, symbol) DO UPDATE SET
                direction=excluded.direction,
                qty=excluded.qty, price=excluded.price,
                notional=excluded.notional, updated_at=excluded.updated_at
        """
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, (strategy, symbol, direction, qty, price, notional, now))
        except Exception:
            pass

    def clear_position(self, strategy: str, symbol: Optional[str] = None) -> None:
        """平倉後移除該 bot 的持倉記錄。

        symbol 指定時只刪該 (strategy, symbol)；未指定時刪該策略全部（向後相容）。
        """
        ph = self._ph()
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                if symbol is None:
                    cur.execute(f"DELETE FROM open_positions WHERE strategy = {ph}",
                                (strategy,))
                else:
                    cur.execute(
                        f"DELETE FROM open_positions WHERE strategy = {ph} AND symbol = {ph}",
                        (strategy, symbol))
        except Exception:
            pass

    def get_positions(self) -> list[dict]:
        """回傳所有 bot 目前的開倉記錄。DB 出錯時回空 list（fail-open）。"""
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT strategy, symbol, direction, qty, price, notional "
                    "FROM open_positions"
                )
                rows = cur.fetchall()
            return [
                {"strategy": r[0], "symbol": r[1], "direction": r[2],
                 "qty": r[3], "price": r[4], "notional": r[5]}
                for r in rows
            ]
        except Exception:
            return []

    def check_exposure(
        self,
        own_strategy: str,
        direction: int,
        new_notional: float,
        max_notional: float,
        own_symbol: Optional[str] = None,
    ) -> tuple[bool, str]:
        """開倉前呼叫：檢查同向暴露是否超過上限。

        自己（identity = own_strategy + own_symbol）的舊持倉不計入同向暴露；
        own_symbol 未指定時退回只比對 strategy（向後相容）。
        其他 bot（含同策略但不同 symbol）的同向倉位都計入。
        回傳 (allow, reason)。DB 出錯時放行（fail-open）。
        """
        def _is_own(p) -> bool:
            if p["strategy"] != own_strategy:
                return False
            return own_symbol is None or p["symbol"] == own_symbol

        positions = self.get_positions()
        same_dir = sum(
            p["notional"]
            for p in positions
            if not _is_own(p) and p["direction"] == direction
        )
        total = same_dir + new_notional
        if total > max_notional:
            return False, (f"跨 bot 同向暴露超過上限：{total:.0f} > {max_notional:.0f} USDT")
        return True, "ok"
