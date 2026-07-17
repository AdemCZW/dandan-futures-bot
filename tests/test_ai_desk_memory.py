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
