"""交易日誌 — 把每筆進出場留底（PostgreSQL 或 SQLite fallback）。

為什麼需要：幣安測試網約每月重置一次，餘額/持倉/掛單都會清空。
若把長期績效綁在測試網餘額上，重置後就什麼都不剩。這個模組把每一筆
成交獨立寫進資料庫，不受測試網重置影響。

後端選擇（自動偵測）：
  有 DATABASE_URL env var → PostgreSQL（Railway 雲端）
  無 DATABASE_URL         → SQLite（本地開發）

也可當小工具用，查看最近交易：
    python -m core.trade_journal            # 列出最近 20 筆
    python -m core.trade_journal 50 trades.db
"""
from __future__ import annotations
import csv
import os
import sqlite3
from datetime import datetime, timezone

_COLUMNS = ["logged_at", "ts", "run_id", "mode", "symbol",
            "strategy", "side", "price", "qty", "pnl", "equity"]

_DATABASE_URL = os.getenv("DATABASE_URL")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _pg_connect():
    import psycopg2
    return psycopg2.connect(_DATABASE_URL)


class TradeJournal:
    """把交易寫進 PostgreSQL（Railway）或 SQLite（本地）與 CSV（可選）。"""

    def __init__(self, db_path: str = "trades.db", csv_path: str | None = None, *,
                 run_id: str | None = None, mode: str = "live",
                 symbol: str = "", strategy: str = ""):
        self.db_path  = db_path
        self.csv_path = csv_path
        self.run_id   = run_id or _default_run_id()
        self.mode     = mode
        self.symbol   = symbol
        self.strategy = strategy
        self._pg      = bool(_DATABASE_URL)

        if self._pg:
            self._conn = _pg_connect()
        else:
            self._conn = sqlite3.connect(db_path)

        self._init_db()
        if csv_path:
            self._init_csv()

    def _init_db(self) -> None:
        if self._pg:
            cur = self._conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id        SERIAL PRIMARY KEY,
                    logged_at TEXT,
                    ts        TEXT,
                    run_id    TEXT,
                    mode      TEXT,
                    symbol    TEXT,
                    strategy  TEXT,
                    side      TEXT,
                    price     DOUBLE PRECISION,
                    qty       DOUBLE PRECISION,
                    pnl       DOUBLE PRECISION,
                    equity    DOUBLE PRECISION
                )
            """)
            self._conn.commit()
        else:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS trades (
                       id        INTEGER PRIMARY KEY AUTOINCREMENT,
                       logged_at TEXT,
                       ts        TEXT,
                       run_id    TEXT,
                       mode      TEXT,
                       symbol    TEXT,
                       strategy  TEXT,
                       side      TEXT,
                       price     REAL,
                       qty       REAL,
                       pnl       REAL,
                       equity    REAL
                   )"""
            )
            self._conn.commit()

    def _init_csv(self) -> None:
        if not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0:
            with open(self.csv_path, "a", newline="") as fh:
                csv.writer(fh).writerow(_COLUMNS)

    def _pg_write(self, sql: str, vals: list) -> bool:
        """PG 寫入，連線層級錯誤時重連重試一次。回傳 True=成功、False=重試後仍失敗。

        F1（高，2026-07-04 風控審查）：Railway PG 會重啟/閒置逾時。原本 log() 用
        建構時建立的 self._conn，連線死掉後直接拋例外，導致進場流程在「已於交易所
        開倉」後、「掛保護性停損」前中斷 → 交易所裸倉、無 journal/state → 幽靈持倉。
        改成：連線層級錯誤（OperationalError/InterfaceError）時關舊連線、重連、重試；
        重試後仍失敗則印警告並回 False（不拋例外），讓呼叫端的 _place_protective /
        _save 仍能執行——最壞情況只是漏一筆 journal 列（CSV/state 仍有、重啟可對帳）。
        """
        import psycopg2
        for attempt in (1, 2):
            try:
                cur = self._conn.cursor()
                cur.execute(sql, vals)
                self._conn.commit()
                return True
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                try:
                    self._conn.close()
                except Exception:                       # noqa: BLE001 — 死連線 close 也可能拋，忽略
                    pass
                if attempt == 2:
                    print(f"[TradeJournal] PG 寫入重連後仍失敗，略過此筆（{e}）")
                    return False
                try:
                    self._conn = _pg_connect()          # 重連，下一輪重試
                except Exception as e2:                 # noqa: BLE001 — 重連本身失敗（DB 全滅）
                    print(f"[TradeJournal] PG 重連失敗（{e2}）")
                    return False
        return False

    def log(self, side: str, price: float, qty: float = 0.0,
            pnl: float = 0.0, equity: float | None = None, ts=None) -> dict:
        """記錄一筆交易事件。回傳寫入的 row dict。"""
        rec = {
            "logged_at": _utc_now(),
            "ts":        _utc_now() if ts is None else str(ts),
            "run_id":    self.run_id,
            "mode":      self.mode,
            "symbol":    self.symbol,
            "strategy":  self.strategy,
            "side":      side,
            "price":     float(price),
            "qty":       float(qty),
            "pnl":       float(pnl),
            "equity":    None if equity is None else float(equity),
        }
        vals = [rec[c] for c in _COLUMNS]
        if self._pg:
            ph = ",".join(["%s"] * len(_COLUMNS))
            self._pg_write(
                f"INSERT INTO trades ({','.join(_COLUMNS)}) VALUES ({ph})", vals
            )
        else:
            self._conn.execute(
                f"INSERT INTO trades ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(_COLUMNS))})",
                vals,
            )
            self._conn.commit()

        if self.csv_path:
            with open(self.csv_path, "a", newline="") as fh:
                csv.writer(fh).writerow(vals)
        return rec

    def log_trades(self, trades: list[dict]) -> int:
        for t in trades:
            self.log(side=t.get("side", ""), price=t.get("price", 0.0),
                     qty=t.get("qty", 0.0), pnl=t.get("pnl", 0.0), ts=t.get("ts"))
        return len(trades)

    def tail(self, n: int = 20) -> list[tuple]:
        cols = ",".join(_COLUMNS)
        if self._pg:
            cur = self._conn.cursor()
            cur.execute(f"SELECT {cols} FROM trades ORDER BY id DESC LIMIT %s", (n,))
            rows = cur.fetchall()
        else:
            cur = self._conn.execute(
                f"SELECT {cols} FROM trades ORDER BY id DESC LIMIT ?", (n,)
            )
            rows = cur.fetchall()
        return list(reversed(rows))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TradeJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ── 共用查詢函式（供 service.py / run_live_futures.py 使用）────────────────

def read_trades_db(limit: int = 50, mode: str | None = None,
                   strategy: str | None = None, symbol: str | None = None,
                   db_path: str = "trades.db") -> list[dict]:
    """從 PostgreSQL 或 SQLite 讀取最近交易（最新在最前）。

    strategy + symbol 過濾確保每台 bot 只看到自己的紀錄（共用 PG 時必要）。
    兩台 bot 跑同一策略（如 fib_channel）但不同標的時，須一併用 symbol 區分。
    """
    cols = "ts, mode, symbol, strategy, side, price, qty, pnl"
    keys = ["ts", "mode", "symbol", "strategy", "side", "price", "qty", "pnl"]

    if _DATABASE_URL:
        try:
            conn  = _pg_connect()
            cur   = conn.cursor()
            conds: list[str] = []
            args:  list      = []
            if mode:
                conds.append("mode = %s");     args.append(mode)
            if strategy:
                conds.append("strategy = %s"); args.append(strategy)
            if symbol:
                conds.append("symbol = %s");   args.append(symbol)
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            cur.execute(f"SELECT {cols} FROM trades{where} ORDER BY id DESC LIMIT %s",
                        args + [limit])
            rows = [dict(zip(keys, r)) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception:
            return []
    else:
        if not os.path.exists(db_path):
            return []
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            conds_s: list[str] = []
            args_s:  list      = []
            if mode:
                conds_s.append("mode = ?");     args_s.append(mode)
            if strategy:
                conds_s.append("strategy = ?"); args_s.append(strategy)
            if symbol:
                conds_s.append("symbol = ?");   args_s.append(symbol)
            where_s = (" WHERE " + " AND ".join(conds_s)) if conds_s else ""
            rows = [dict(r) for r in conn.execute(
                f"SELECT {cols} FROM trades{where_s} ORDER BY id DESC LIMIT ?",
                args_s + [limit],
            ).fetchall()]
            conn.close()
            return rows
        except sqlite3.Error:
            return []


def implied_open_position(rows: list[dict], dust: float = 1e-9) -> dict | None:
    """從交易紀錄推算「目前是否仍有未平倉的 entry」。

    輸入為 read_trades_db 的格式（最新在最前，每列含 side/price/qty）。
    由舊到新走一遍，維護一個開倉狀態：
      · entry / entry_short → 開新倉（覆蓋既有，連續 entry 無 exit 時以最後一筆為準）
      · scale_out           → 遞減剩餘量，歸零即視為平倉
      · exit*               → 平倉、清空
    回傳未平倉 entry dict {side, price, qty, dir}（dir: 1 多 / -1 空）或 None。

    用途：重啟還原時，若 DB 顯示仍持倉但交易所已空手（狀態檔被 railway up 清掉、
    交易所端 STOP/TP 已觸發平倉但漏記），據此補記遺漏的平倉，避免該筆交易與損益消失。
    """
    open_pos = None
    for t in reversed(rows):                       # 轉時間正序（最舊→最新）
        side = t.get("side", "")
        if side in ("entry", "entry_short"):
            open_pos = {
                "side": side,
                "price": float(t.get("price", 0.0)),
                "qty": float(t.get("qty", 0.0)),
                "dir": 1 if side == "entry" else -1,
            }
        elif side == "scale_out":
            if open_pos is not None:
                open_pos["qty"] -= float(t.get("qty", 0.0))
                if open_pos["qty"] <= dust:
                    open_pos = None
        elif side.startswith("exit"):
            open_pos = None
    return open_pos


def _print_tail(n: int = 20, db_path: str = "trades.db") -> None:
    rows_raw = read_trades_db(limit=n, db_path=db_path)
    if not rows_raw:
        print(f"找不到交易紀錄（{'PostgreSQL' if _DATABASE_URL else db_path}）")
        return
    print(f"=== 最近 {len(rows_raw)} 筆 ===")
    print(f"{'ts':<20}{'mode':<13}{'strategy':<14}{'side':<12}"
          f"{'price':>12}{'qty':>14}{'pnl':>12}")
    for d in rows_raw:
        print(f"{str(d.get('ts',''))[:19]:<20}{str(d.get('mode','')):<13}"
              f"{str(d.get('strategy','')):<14}{str(d.get('side','')):<12}"
              f"{float(d.get('price',0)):>12.2f}{float(d.get('qty',0)):>14.6f}"
              f"{float(d.get('pnl',0)):>12.2f}")


if __name__ == "__main__":
    import sys
    n    = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    path = sys.argv[2] if len(sys.argv) > 2 else "trades.db"
    _print_tail(n, path)
