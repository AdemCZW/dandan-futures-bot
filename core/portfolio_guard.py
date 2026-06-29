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
from contextlib import contextmanager
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

# OPT-05：組合層淨值表。每台 bot 記自己當前淨值與運行期峰值，供組合級回撤熔斷彙總。
_CREATE_EQUITY_SQL = """
CREATE TABLE IF NOT EXISTS portfolio_equity (
    strategy   TEXT,
    symbol     TEXT,
    equity     REAL,
    peak       REAL,
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

    @contextmanager
    def _conn(self):
        """連線 context manager：離開時 commit 並【關閉】連線。

        psycopg2 的 `with conn:` 只 commit/rollback、不關閉 TCP 連線；PortfolioGuard 是長壽物件
        （每台 bot 一個、跑數天），原本每次呼叫新開連線又不關 → Railway Postgres 連線緩慢洩漏、
        最終 too many connections 拖垮 TradeJournal 寫入與儀表板查詢。此處 finally 確保關閉。
        """
        if self._url:
            import psycopg2
            conn = psycopg2.connect(self._url)
        else:
            conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def _ph(self) -> str:
        return "%s" if self._url else "?"

    def _init_db(self) -> None:
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(_CREATE_SQL)
                cur.execute(_CREATE_EQUITY_SQL)
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
        symbol_cap: Optional[float] = None,
        corr_symbols: Optional[list] = None,
    ) -> tuple[bool, str]:
        """開倉前呼叫：檢查同向暴露是否超過上限。

        自己（identity = own_strategy + own_symbol）的舊持倉不計入同向暴露；
        own_symbol 未指定時退回只比對 strategy（向後相容）。
        其他 bot（含同策略但不同 symbol）的同向倉位都計入。

        OPT-13 集中度子上限（symbol_cap 為 None 時全部略過，向後相容）：
          - 預設：同一 own_symbol 的他台同向名目 + new ≤ symbol_cap（三台 SOL 真正被擋）。
          - corr_symbols 有給且 own_symbol 在其中：把整個相關性桶（如 SOL+ETH）的他台
            同向名目一起加總比對 symbol_cap（高相關標的視為近乎同一風險源）。
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
            # 全組合同向暴露超限（先比這個；下方還有集中度子上限）
            return False, (f"跨 bot 同向暴露超過上限：{total:.0f} > {max_notional:.0f} USDT")

        # ── 集中度子上限：per-symbol 或相關性桶 ──
        if symbol_cap is not None and own_symbol is not None:
            if corr_symbols and own_symbol in corr_symbols:
                bucket = set(corr_symbols)
                label = "+".join(sorted(s.replace("USDT", "") for s in bucket))
            else:
                bucket = {own_symbol}
                label = own_symbol.replace("USDT", "")
            bucket_dir = sum(
                p["notional"]
                for p in positions
                if not _is_own(p) and p["direction"] == direction and p["symbol"] in bucket
            )
            bucket_total = bucket_dir + new_notional
            if bucket_total > symbol_cap:
                return False, (f"{label} 同向集中度超過子上限："
                               f"{bucket_total:.0f} > {symbol_cap:.0f} USDT")

        return True, "ok"

    # ── OPT-05 組合層淨值/回撤 kill-switch ──────────────────────────────────

    def upsert_equity(self, strategy: str, symbol: str, equity: float) -> None:
        """寫入該 bot 當前淨值，並維護運行期峰值（peak = max(舊 peak, 當前淨值)）。

        on_bar_close 每根呼叫。identity = (strategy, symbol)，與持倉表一致。fail-open。
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ph = self._ph()
        sql = f"""
            INSERT INTO portfolio_equity (strategy, symbol, equity, peak, updated_at)
            VALUES ({ph},{ph},{ph},{ph},{ph})
            ON CONFLICT(strategy, symbol) DO UPDATE SET
                equity=excluded.equity,
                peak=CASE WHEN excluded.equity > portfolio_equity.peak
                          THEN excluded.equity ELSE portfolio_equity.peak END,
                updated_at=excluded.updated_at
        """
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute(sql, (strategy, symbol, equity, equity, now))
        except Exception:
            pass

    def portfolio_drawdown(self) -> tuple[float, float, float]:
        """回傳 (組合現值總和, 組合峰值總和, 回撤比例)。

        回撤 = (現值總和 − 峰值總和) / 峰值總和（≤0）。各 bot 峰值在不同時點 → 峰值總和
        略高於真實同時峰值，使回撤偏保守（早一點觸發），對 kill-switch 是安全方向。
        無資料 → (0, 0, 0)。
        """
        try:
            with self._conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT equity, peak FROM portfolio_equity")
                rows = cur.fetchall()
        except Exception:
            return 0.0, 0.0, 0.0
        if not rows:
            return 0.0, 0.0, 0.0
        eq = sum(float(r[0]) for r in rows)
        peak = sum(float(r[1]) for r in rows)
        dd = (eq - peak) / peak if peak > 0 else 0.0
        return eq, peak, dd

    def check_portfolio_drawdown(self, max_dd: float) -> tuple[bool, str]:
        """開倉前呼叫：組合層回撤超過 max_dd 就拒絕新倉（只擋進場，不碰既有倉）。

        max_dd<=0 → 停用（一律放行）。無資料/DB 出錯 → 放行（fail-open）。
        """
        if max_dd <= 0:
            return True, "ok"
        eq, peak, dd = self.portfolio_drawdown()
        if peak <= 0:
            return True, "ok"
        if dd <= -max_dd:
            return False, (f"組合層回撤熔斷：總淨值 {eq:.0f} 自峰值 {peak:.0f} "
                           f"回落 {abs(dd) * 100:.1f}% ≥ {max_dd * 100:.0f}%，暫停新倉")
        return True, "ok"
