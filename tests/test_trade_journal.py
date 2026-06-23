"""TradeJournal 的 pytest 測試。

只讀 core/trade_journal.py，不修改 source。所有 IO 用 tmp_path。
"""
from __future__ import annotations

import csv
import sqlite3

import pytest

from core.trade_journal import TradeJournal, _COLUMNS


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
