"""core.live_status 輕量即時監控資料層測試。"""
import subprocess
import sys

import pytest
from core.live_status import trade_stats, bot_live_status


def test_import_lean_no_backtest_deps():
    """在乾淨子行程 import core.live_status，並讓回測/最佳化肥依賴不可用 →
    仍能 import 成功。用子行程避免污染本行程的 sys.modules（模組綁定）。"""
    code = (
        "import sys, builtins;"
        "real=builtins.__import__;"
        "block={'optuna','vectorbt','matplotlib','backtest','run_optimize'};"
        "builtins.__import__=lambda n,*a,**k:(_ for _ in ()).throw(ImportError(n))"
        " if (n.split('.')[0] in block) else real(n,*a,**k);"
        "import core.live_status as m;"
        "assert hasattr(m,'bot_live_status');"
        "print('ok')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stderr


def test_trade_stats_long_short_split():
    rows = [  # newest-first
        {"side": "exit_tp", "price": 110, "qty": 1, "pnl": 10},
        {"side": "entry", "price": 100, "qty": 1, "pnl": 0},
        {"side": "exit_sl", "price": 95, "qty": 1, "pnl": -5},
        {"side": "entry_short", "price": 100, "qty": 1, "pnl": 0},
    ]
    s = trade_stats(rows)
    assert s["long_trades"] == 1 and s["long_wins"] == 1 and s["long_pnl"] == 10
    assert s["short_trades"] == 1 and s["short_wins"] == 0 and s["short_pnl"] == -5


def test_bot_live_status_shape_and_stats(monkeypatch):
    def fake_read(limit=50, strategy=None, symbol=None, db_path=None, **kw):
        return [
            {"side": "exit_tp", "price": 110, "qty": 1, "pnl": 10, "ts": "2026-07-01 01:00:00"},
            {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-01 00:00:00"},
        ]
    monkeypatch.setattr("core.live_status.read_trades_db", fake_read)
    state = {"mode": "futures", "in_position": True, "direction": 1,
             "entry_price": 100.0, "cash": 5010.0, "base": 1.0, "last_price": 108.0,
             "symbol": "BTCUSDT", "interval": "15m", "poll": 60,
             "last_decision": {"target": 1}}
    out = bot_live_status(state, "fib_channel", "BTCUSDT", "15m")
    assert out["symbol"] == "BTCUSDT" and out["strategy"] == "fib_channel"
    assert out["in_position"] is True and out["direction"] == 1
    assert out["realized_pnl"] == 10 and out["total_trades"] == 1 and out["win_trades"] == 1
    assert out["unrealized_pnl"] == 8.0        # (108-100)*1
    assert out["last_decision"] == {"target": 1}
    assert "long_trades" in out and "sharpe" in out and "recent_trades" in out


def test_bot_live_status_short_unrealized(monkeypatch):
    monkeypatch.setattr("core.live_status.read_trades_db", lambda **kw: [])
    state = {"mode": "futures", "in_position": True, "direction": -1,
             "entry_price": 100.0, "cash": 5000.0, "base": 2.0, "last_price": 95.0}
    out = bot_live_status(state, "s", "BTCUSDT", "15m")
    assert out["unrealized_pnl"] == 10.0        # 空單 (100-95)*2 = +10


# ── 完善 bot 數據（2026-07-05）：期望值/獲利因子/平均賺虧/最大連虧/平均持倉時長 ──
def test_trade_stats_expectancy_profit_factor_and_streak():
    rows = [  # newest-first：多 +10、多 -5、多 -3、多 +6（時間正序為 +6,-3,-5,+10）
        {"side": "exit_tp", "price": 110, "qty": 1, "pnl": 10, "ts": "2026-07-04 12:00:00"},
        {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-04 08:00:00"},
        {"side": "exit_sl", "price": 95, "qty": 1, "pnl": -5, "ts": "2026-07-03 12:00:00"},
        {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-03 08:00:00"},
        {"side": "exit_sl", "price": 97, "qty": 1, "pnl": -3, "ts": "2026-07-02 12:00:00"},
        {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-02 08:00:00"},
        {"side": "exit_tp", "price": 106, "qty": 1, "pnl": 6, "ts": "2026-07-01 12:00:00"},
        {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-01 00:00:00"},
    ]
    s = trade_stats(rows)
    assert s["expectancy"] == 2.0                      # (6-3-5+10)/4
    assert s["profit_factor"] == 2.0                   # (6+10)/(3+5)
    assert s["avg_win"] == 8.0                         # (6+10)/2
    assert s["avg_loss"] == -4.0                       # (-3-5)/2
    assert s["max_consec_losses"] == 2                 # -3,-5 連續兩筆
    assert s["avg_hold_hours"] == 6.0                  # (12+4+4+4)/4 = 24/4 小時


def test_trade_stats_new_fields_none_when_no_trades():
    s = trade_stats([])
    assert s["expectancy"] is None
    assert s["profit_factor"] is None
    assert s["avg_win"] is None and s["avg_loss"] is None
    assert s["max_consec_losses"] == 0
    assert s["avg_hold_hours"] is None


def test_trade_stats_profit_factor_none_when_no_losses():
    rows = [
        {"side": "exit_tp", "price": 110, "qty": 1, "pnl": 10, "ts": "2026-07-01 04:00:00"},
        {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-01 00:00:00"},
    ]
    s = trade_stats(rows)
    assert s["profit_factor"] is None                  # 無虧損 → 無法計（∞ 不是合法 JSON）
    assert s["expectancy"] == 10.0


def test_bot_live_status_exposes_new_stats(monkeypatch):
    def fake_read(limit=50, strategy=None, symbol=None, db_path=None, **kw):
        return [
            {"side": "exit_tp", "price": 110, "qty": 1, "pnl": 10, "ts": "2026-07-01 06:00:00"},
            {"side": "entry", "price": 100, "qty": 1, "pnl": 0, "ts": "2026-07-01 00:00:00"},
        ]
    monkeypatch.setattr("core.live_status.read_trades_db", fake_read)
    out = bot_live_status({"mode": "futures", "cash": 5000.0}, "s", "BTCUSDT", "4h")
    for key in ("expectancy", "profit_factor", "avg_win", "avg_loss",
                "max_consec_losses", "avg_hold_hours"):
        assert key in out, f"bot_live_status 缺 {key}"
