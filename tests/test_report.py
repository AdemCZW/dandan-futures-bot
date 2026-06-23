"""core/report.py 的測試：HTML 報表自包含、含內嵌圖與績效卡片。"""
import os
from dataclasses import dataclass, field

import pandas as pd

from core.report import build_report


@dataclass
class FakeResult:
    """提供 build_report 需要的最小介面。"""
    equity_curve: pd.Series
    trades: list = field(default_factory=list)
    total_return: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    sharpe: float = 0.0


def _result():
    idx = pd.date_range("2026-06-01", periods=10, freq="h")
    eq = pd.Series([100.0 + i for i in range(10)], index=idx)
    trades = [
        {"ts": idx[3], "side": "entry", "price": 103.0, "qty": 0.5, "pnl": 0.0, "dir": 1},
        {"ts": idx[6], "side": "exit_signal", "price": 106.0, "qty": 0.5, "pnl": 1.5, "dir": 1},
    ]
    return FakeResult(equity_curve=eq, trades=trades, total_return=0.06,
                      max_drawdown=-0.02, win_rate=0.5, sharpe=1.1)


def test_build_report_creates_self_contained_html(tmp_path):
    out = tmp_path / "report.html"
    path = build_report(_result(), title="t", out=str(out))
    assert path == str(out)
    assert out.exists() and out.stat().st_size > 0
    html = out.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html       # 圖內嵌
    assert "Total return" in html                  # 績效卡片
    assert "<table" in html and "exit_signal" in html  # 交易表


def test_build_report_handles_no_trades(tmp_path):
    r = _result()
    r.trades = []
    out = tmp_path / "empty.html"
    build_report(r, title="empty", out=str(out))
    html = out.read_text(encoding="utf-8")
    assert "無已平倉交易" in html


def test_build_report_writes_only_to_target(tmp_path):
    out = tmp_path / "r.html"
    build_report(_result(), out=str(out))
    # 只應產生指定的輸出檔（圖存在系統暫存、不落在 tmp_path 目錄）
    assert [p.name for p in tmp_path.iterdir()] == ["r.html"]
