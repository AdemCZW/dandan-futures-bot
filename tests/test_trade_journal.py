"""TradeJournal 的 pytest 測試。

只讀 core/trade_journal.py，不修改 source。所有 IO 用 tmp_path。
"""
from __future__ import annotations

import csv
import sqlite3

import pytest

from core.trade_journal import (
    TradeJournal, _COLUMNS, read_trades_db, implied_open_position,
)


def _read_csv_rows(csv_path):
    with open(csv_path, newline="") as fh:
        return list(csv.reader(fh))


def test_log_inserts_row_with_correct_values(tmp_path):
    db_path = str(tmp_path / "trades.db")
    with TradeJournal(db_path=db_path, run_id="RID1", mode="paper",
                      symbol="BTCUSDT", strategy="zscore") as j:
        rec = j.log(side="BUY", price=100.5, qty=0.25, pnl=12.5, equity=1000.0,
                    ts="2026-01-01T00:00:00Z")

    # 回傳的 dict 欄位值正確
    assert rec["side"] == "BUY"
    assert rec["price"] == 100.5
    assert rec["qty"] == 0.25
    assert rec["pnl"] == 12.5
    assert rec["equity"] == 1000.0
    assert rec["ts"] == "2026-01-01T00:00:00Z"
    assert rec["run_id"] == "RID1"
    assert rec["mode"] == "paper"
    assert rec["symbol"] == "BTCUSDT"
    assert rec["strategy"] == "zscore"

    # SQLite 內確有該列，且各欄位與寫入一致
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f"SELECT {','.join(_COLUMNS)} FROM trades")
        rows = cur.fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    stored = dict(zip(_COLUMNS, rows[0]))
    assert stored["side"] == "BUY"
    assert stored["price"] == 100.5
    assert stored["qty"] == 0.25
    assert stored["pnl"] == 12.5
    assert stored["equity"] == 1000.0
    assert stored["ts"] == "2026-01-01T00:00:00Z"
    assert stored["run_id"] == "RID1"
    assert stored["mode"] == "paper"
    assert stored["symbol"] == "BTCUSDT"
    assert stored["strategy"] == "zscore"


def test_tail_returns_oldest_to_newest(tmp_path):
    db_path = str(tmp_path / "trades.db")
    with TradeJournal(db_path=db_path, run_id="RID", mode="paper") as j:
        j.log(side="A", price=1.0)
        j.log(side="B", price=2.0)
        j.log(side="C", price=3.0)
        j.log(side="D", price=4.0)

        # tail 全部：由舊到新
        rows = j.tail(10)
        sides = [dict(zip(_COLUMNS, r))["side"] for r in rows]
        assert sides == ["A", "B", "C", "D"]

        # tail(n) 取最近 n 筆，但回傳仍由舊到新
        last_two = j.tail(2)
        sides_two = [dict(zip(_COLUMNS, r))["side"] for r in last_two]
        assert sides_two == ["C", "D"]


def test_csv_header_written_once_on_open(tmp_path):
    db_path = str(tmp_path / "trades.db")
    csv_path = str(tmp_path / "trades.csv")

    with TradeJournal(db_path=db_path, csv_path=csv_path, run_id="RID") as j:
        j.log(side="BUY", price=10.0, qty=1.0)
        j.log(side="SELL", price=11.0, qty=1.0)

    rows = _read_csv_rows(csv_path)
    # 1 行表頭 + 2 行資料
    assert rows[0] == _COLUMNS
    assert len(rows) == 3
    assert rows[1][_COLUMNS.index("side")] == "BUY"
    assert rows[2][_COLUMNS.index("side")] == "SELL"


def test_csv_header_not_duplicated_on_reopen(tmp_path):
    db_path = str(tmp_path / "trades.db")
    csv_path = str(tmp_path / "trades.csv")

    # 第一次開檔並寫入
    with TradeJournal(db_path=db_path, csv_path=csv_path, run_id="RID1") as j:
        j.log(side="BUY", price=10.0, qty=1.0)

    # 重開同一個 CSV（同 db），再寫一筆
    with TradeJournal(db_path=db_path, csv_path=csv_path, run_id="RID2") as j:
        j.log(side="SELL", price=11.0, qty=1.0)

    rows = _read_csv_rows(csv_path)
    # 表頭只該出現一次
    assert rows[0] == _COLUMNS
    header_count = sum(1 for r in rows if r == _COLUMNS)
    assert header_count == 1
    # 1 表頭 + 2 資料列
    assert len(rows) == 3
    assert rows[1][_COLUMNS.index("side")] == "BUY"
    assert rows[2][_COLUMNS.index("side")] == "SELL"


def test_log_trades_returns_batch_count(tmp_path):
    db_path = str(tmp_path / "trades.db")
    trades = [
        {"side": "BUY", "price": 1.0, "qty": 0.5, "pnl": 0.0, "ts": "t1"},
        {"side": "SELL", "price": 2.0, "qty": 0.5, "pnl": 1.0, "ts": "t2"},
        {"side": "BUY", "price": 3.0},
    ]
    with TradeJournal(db_path=db_path, run_id="RID") as j:
        n = j.log_trades(trades)
        assert n == 3

        # DB 內確實寫入 3 筆
        cur = j._conn.execute("SELECT COUNT(*) FROM trades")
        assert cur.fetchone()[0] == 3

        # 順序與內容正確（由舊到新）
        rows = j.tail(10)
        dicts = [dict(zip(_COLUMNS, r)) for r in rows]
        assert [d["side"] for d in dicts] == ["BUY", "SELL", "BUY"]
        assert [d["price"] for d in dicts] == [1.0, 2.0, 3.0]
        # 未提供的欄位採預設值
        assert dicts[2]["qty"] == 0.0
        assert dicts[2]["pnl"] == 0.0


class TestReadTradesSymbolFilter:
    """同策略不同 symbol 共用同一張表時，symbol 過濾確保各 bot 只看到自己的紀錄。

    問題背景：Bot1（fib_channel + BTCUSDT）與 Bot2（fib_channel + SOLUSDT）
    共用同一個 DB，僅用 strategy 過濾時兩台撈到同一批紀錄。
    """

    def _seed(self, db_path):
        """寫入同策略、兩種 symbol 的紀錄。"""
        with TradeJournal(db_path=db_path, run_id="R", mode="live_futures_testnet",
                          symbol="BTCUSDT", strategy="fib_channel") as j:
            j.log(side="entry", price=60000.0, qty=0.01)
            j.log(side="exit_signal", price=61000.0, qty=0.01, pnl=10.0)
        with TradeJournal(db_path=db_path, run_id="R", mode="live_futures_testnet",
                          symbol="SOLUSDT", strategy="fib_channel") as j:
            j.log(side="entry", price=68.0, qty=10.0)
            j.log(side="exit_signal", price=69.0, qty=10.0, pnl=10.0)
            j.log(side="entry_short", price=70.0, qty=10.0)

    def test_filter_by_symbol_returns_only_that_symbol(self, tmp_path):
        db_path = str(tmp_path / "trades.db")
        self._seed(db_path)
        btc = read_trades_db(limit=50, strategy="fib_channel",
                             symbol="BTCUSDT", db_path=db_path)
        assert len(btc) == 2
        assert all(r["symbol"] == "BTCUSDT" for r in btc), \
            "BTCUSDT 過濾不該撈到 SOLUSDT 的紀錄"

    def test_other_symbol_isolated(self, tmp_path):
        db_path = str(tmp_path / "trades.db")
        self._seed(db_path)
        sol = read_trades_db(limit=50, strategy="fib_channel",
                             symbol="SOLUSDT", db_path=db_path)
        assert len(sol) == 3
        assert all(r["symbol"] == "SOLUSDT" for r in sol)

    def test_no_symbol_returns_all(self, tmp_path):
        """未給 symbol 時維持舊行為（只用 strategy 過濾，全撈）。"""
        db_path = str(tmp_path / "trades.db")
        self._seed(db_path)
        allrows = read_trades_db(limit=50, strategy="fib_channel", db_path=db_path)
        assert len(allrows) == 5, "未指定 symbol 應回傳該策略全部紀錄"


def test_equity_none_is_null_in_db_and_empty_in_csv(tmp_path):
    db_path = str(tmp_path / "trades.db")
    csv_path = str(tmp_path / "trades.csv")

    with TradeJournal(db_path=db_path, csv_path=csv_path, run_id="RID") as j:
        rec = j.log(side="BUY", price=10.0, qty=1.0, equity=None)
        assert rec["equity"] is None

    # DB 存成 NULL
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT equity FROM trades")
        value = cur.fetchone()[0]
    finally:
        conn.close()
    assert value is None

    # CSV 存成空字串
    rows = _read_csv_rows(csv_path)
    eq_idx = _COLUMNS.index("equity")
    assert rows[0] == _COLUMNS  # 表頭
    assert rows[1][eq_idx] == ""


# ── implied_open_position：從交易紀錄推算「是否還有未平倉的 entry」──────────────
# 輸入為 read_trades_db 的格式（最新在最前），回傳未平倉 entry dict 或 None。

def _rows_newest_first(events):
    """把 (side, price, qty) 的時間正序列表轉成 read_trades_db 格式（最新在前）。"""
    return [{"side": s, "price": p, "qty": q} for (s, p, q) in reversed(events)]


def test_implied_open_none_when_entry_then_exit():
    rows = _rows_newest_first([
        ("entry", 100.0, 1.0),
        ("exit_tp", 110.0, 1.0),
    ])
    assert implied_open_position(rows) is None


def test_implied_open_returns_entry_when_unmatched():
    rows = _rows_newest_first([
        ("entry", 100.0, 1.0),
    ])
    op = implied_open_position(rows)
    assert op is not None
    assert op["side"] == "entry"
    assert op["price"] == 100.0
    assert op["qty"] == 1.0
    assert op["dir"] == 1


def test_implied_open_short_direction():
    rows = _rows_newest_first([
        ("entry_short", 100.0, 2.0),
    ])
    op = implied_open_position(rows)
    assert op["dir"] == -1
    assert op["side"] == "entry_short"


def test_implied_open_consecutive_entries_keeps_latest():
    """連續兩筆 entry 無 exit（資料缺漏）→ 以最後一筆為未平倉倉位。"""
    rows = _rows_newest_first([
        ("entry", 100.0, 1.0),
        ("entry", 105.0, 1.5),
    ])
    op = implied_open_position(rows)
    assert op["price"] == 105.0
    assert op["qty"] == 1.5


def test_implied_open_scale_out_reduces_qty():
    rows = _rows_newest_first([
        ("entry", 100.0, 2.0),
        ("scale_out", 105.0, 0.8),
    ])
    op = implied_open_position(rows)
    assert op["qty"] == pytest.approx(1.2)


def test_implied_open_scale_out_to_zero_closes():
    rows = _rows_newest_first([
        ("entry", 100.0, 1.0),
        ("scale_out", 105.0, 1.0),
    ])
    assert implied_open_position(rows) is None


def test_implied_open_empty_rows():
    assert implied_open_position([]) is None


def test_implied_open_full_cycle_then_reopen():
    """完整一回合後又開新倉 → 回傳新倉。"""
    rows = _rows_newest_first([
        ("entry", 100.0, 1.0),
        ("exit_sl", 95.0, 1.0),
        ("entry_short", 90.0, 2.0),
    ])
    op = implied_open_position(rows)
    assert op["dir"] == -1
    assert op["price"] == 90.0
