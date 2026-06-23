"""交易日誌 — 把每筆進出場留底（SQLite + 可選 CSV）。

為什麼需要：幣安測試網約每月重置一次，餘額/持倉/掛單都會清空。
若把長期績效綁在測試網餘額上，重置後就什麼都不剩。這個模組把每一筆
成交獨立寫進本機 SQLite（與可選的 CSV），不受測試網重置影響。

只用 Python 標準庫（sqlite3 + csv），不增加任何相依套件。

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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_run_id() -> str:
    # 用啟動時間當 run_id，方便把同一次執行的交易歸在一起
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class TradeJournal:
    """把交易寫進 SQLite（必備）與 CSV（可選）。可當 context manager 用。"""

    def __init__(self, db_path: str = "trades.db", csv_path: str | None = None, *,
                 run_id: str | None = None, mode: str = "live",
                 symbol: str = "", strategy: str = ""):
        self.db_path = db_path
        self.csv_path = csv_path
        self.run_id = run_id or _default_run_id()
        self.mode = mode
        self.symbol = symbol
        self.strategy = strategy
        self._conn = sqlite3.connect(db_path)
        self._init_db()
        if csv_path:
            self._init_csv()

    def _init_db(self) -> None:
        # WAL + busy_timeout：提升併發容忍度（例如 run_live 與 run_backtest 同時寫入時）
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
        # 檔案不存在或空的才寫表頭
        if not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0:
            with open(self.csv_path, "a", newline="") as fh:
                csv.writer(fh).writerow(_COLUMNS)

    def log(self, side: str, price: float, qty: float = 0.0,
            pnl: float = 0.0, equity: float | None = None, ts=None) -> dict:
        """記錄一筆交易事件。回傳寫入的 row dict。"""
        rec = {
            "logged_at": _utc_now(),
            "ts": _utc_now() if ts is None else str(ts),
            "run_id": self.run_id,
            "mode": self.mode,
            "symbol": self.symbol,
            "strategy": self.strategy,
            "side": side,
            "price": float(price),
            "qty": float(qty),
            "pnl": float(pnl),
            "equity": None if equity is None else float(equity),
        }
        self._conn.execute(
            f"INSERT INTO trades ({','.join(_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_COLUMNS))})",
            [rec[c] for c in _COLUMNS],
        )
        self._conn.commit()
        if self.csv_path:
            with open(self.csv_path, "a", newline="") as fh:
                csv.writer(fh).writerow([rec[c] for c in _COLUMNS])
        return rec

    def log_trades(self, trades: list[dict]) -> int:
        """批次傾印回測產生的交易清單（dict 需含 side/price，pnl/qty/ts 可選）。"""
        for t in trades:
            self.log(side=t.get("side", ""), price=t.get("price", 0.0),
                     qty=t.get("qty", 0.0), pnl=t.get("pnl", 0.0), ts=t.get("ts"))
        return len(trades)

    def tail(self, n: int = 20) -> list[tuple]:
        cur = self._conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM trades ORDER BY id DESC LIMIT ?", (n,)
        )
        return list(reversed(cur.fetchall()))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "TradeJournal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _print_tail(n: int = 20, db_path: str = "trades.db") -> None:
    if not os.path.exists(db_path):
        print(f"找不到資料庫 {db_path}（還沒有任何交易留底）。")
        return
    j = TradeJournal(db_path=db_path)
    rows = j.tail(n)
    j.close()
    if not rows:
        print(f"{db_path} 裡還沒有交易。")
        return
    print(f"=== {db_path} 最近 {len(rows)} 筆 ===")
    print(f"{'ts':<20}{'mode':<13}{'strategy':<14}{'side':<12}"
          f"{'price':>12}{'qty':>14}{'pnl':>12}")
    for r in rows:
        d = dict(zip(_COLUMNS, r))
        print(f"{d['ts'][:19]:<20}{d['mode']:<13}{d['strategy']:<14}{d['side']:<12}"
              f"{d['price']:>12.2f}{d['qty']:>14.6f}{d['pnl']:>12.2f}")


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    path = sys.argv[2] if len(sys.argv) > 2 else "trades.db"
    _print_tail(n, path)
