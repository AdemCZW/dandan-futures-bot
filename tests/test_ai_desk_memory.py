"""ai_desk.memory 測試 — JSON Lines 讀寫回合、截斷、prompt 格式化。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.memory import ThesisMemory


def entry(i):
    return {
        "ts": f"2026-07-{10 + i:02d}T08:00:00Z",
        "direction": 1 if i % 2 == 0 else -1,
        "confidence": 0.5 + i * 0.05,
        "rationale_summary": f"第{i}輪判斷",
        "price_at_decision": 60000.0 + i,
    }


def test_roundtrip(tmp_path):
    m = ThesisMemory(str(tmp_path), "BTCUSDT", "4h")
    m.append(entry(0))
    m.append(entry(1))
    got = m.load()
    assert len(got) == 2
    assert got[0]["rationale_summary"] == "第0輪判斷"
    assert got[1]["direction"] == -1


def test_load_truncates_to_last_n(tmp_path):
    m = ThesisMemory(str(tmp_path), "BTCUSDT", "4h")
    for i in range(8):
        m.append(entry(i))
    got = m.load(n=3)
    assert len(got) == 3
    assert got[-1]["rationale_summary"] == "第7輪判斷"
    assert got[0]["rationale_summary"] == "第5輪判斷"


def test_append_rejects_missing_keys(tmp_path):
    m = ThesisMemory(str(tmp_path), "BTCUSDT", "4h")
    with pytest.raises(ValueError, match="缺少"):
        m.append({"ts": "2026-07-17", "direction": 1})


def test_format_for_prompt(tmp_path):
    m = ThesisMemory(str(tmp_path), "BTCUSDT", "4h")
    assert "無歷史" in m.format_for_prompt()
    m.append(entry(0))
    text = m.format_for_prompt()
    assert "第0輪判斷" in text and "60000" in text


# ── 真實結果回饋（2026-08-07）─────────────────────────────
#
# 問題：format_for_prompt 過去只顯示「當時判斷 + 當時價」，模型會自己拿
# 「當時價 vs 現價」的價差推論上次對不對——但這個推論方式是錯的：可能先反彈
# 觸及停損才下跌，紙上結算是虧的。改成附上真實結算結果（觸及停損/停利/
# 未成交/仍浮動），不再讓模型自己用價差瞎猜。

import pandas as pd

from ai_desk.memory import attach_outcomes


def _klines(rows):
    """rows: [(ts, open, high, low, close), ...]"""
    idx = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame({"open": [r[1] for r in rows], "high": [r[2] for r in rows],
                         "low": [r[3] for r in rows], "close": [r[4] for r in rows]},
                        index=idx)


def _approval_row(ts, direction=-1, entry=100.0, stop=105.0, take_profit=90.0, qty=1.0):
    return {"ts": ts, "direction": direction, "entry": entry, "stop": stop,
            "take_profit": take_profit, "qty": qty}


def test_attach_outcomes_marks_stopped_trade():
    entries = [entry(0)]
    entries[0]["ts"] = "2026-08-01 00:00:00"
    rows = [_approval_row("2026-08-01 00:00:00")]           # 空單 entry=100 stop=105 tp=90
    klines = _klines([
        ("2026-08-01 00:00:00", 99, 100, 98, 99),            # 觸發進場
        ("2026-08-01 04:00:00", 99, 106, 99, 105),           # 觸及停損
    ])
    out = attach_outcomes(entries, rows, klines)
    assert out[0]["outcome"]["state"] == "stopped"
    assert out[0]["outcome"]["pnl"] < 0


def test_attach_outcomes_marks_target_trade():
    entries = [entry(0)]
    entries[0]["ts"] = "2026-08-01 00:00:00"
    rows = [_approval_row("2026-08-01 00:00:00")]
    klines = _klines([
        ("2026-08-01 00:00:00", 99, 100, 98, 99),
        ("2026-08-01 04:00:00", 95, 96, 89, 90),             # 觸及停利
    ])
    out = attach_outcomes(entries, rows, klines)
    assert out[0]["outcome"]["state"] == "target"
    assert out[0]["outcome"]["pnl"] > 0


def test_attach_outcomes_no_matching_approval_row_leaves_outcome_none():
    """觀望（direction=0）從不進核准佇列，查無對應紀錄——不可假造結果。"""
    entries = [entry(0)]
    entries[0]["ts"] = "2026-08-01 00:00:00"
    out = attach_outcomes(entries, [], _klines([("2026-08-01 00:00:00", 99, 100, 98, 99)]))
    assert out[0]["outcome"] is None


def test_attach_outcomes_never_raises_on_empty_klines():
    entries = [entry(0)]
    entries[0]["ts"] = "2026-08-01 00:00:00"
    rows = [_approval_row("2026-08-01 00:00:00")]
    out = attach_outcomes(entries, rows, _klines([]))
    assert out[0]["outcome"]["state"] == "no_data"


def test_format_for_prompt_shows_real_outcome_not_just_price_delta(tmp_path):
    m = ThesisMemory(str(tmp_path), "BTCUSDT", "4h")
    e = entry(0)
    e["ts"] = "2026-08-01 00:00:00"
    m.append(e)
    rows = [_approval_row("2026-08-01 00:00:00", direction=1, entry=100, stop=95, take_profit=110)]
    klines = _klines([
        ("2026-08-01 00:00:00", 100, 101, 99, 100),
        ("2026-08-01 04:00:00", 100, 100, 94, 95),           # 多單觸及停損
    ])
    text = m.format_for_prompt(approval_rows=rows, klines=klines)
    assert "停損" in text


def test_format_for_prompt_without_outcome_args_unchanged(tmp_path):
    """不傳 approval_rows/klines 時行為與現況逐位元相同（向後相容）。"""
    m = ThesisMemory(str(tmp_path), "BTCUSDT", "4h")
    m.append(entry(0))
    assert m.format_for_prompt() == m.format_for_prompt(approval_rows=None, klines=None)
