"""ai_desk.roles 測試 — JSON 解析器 + 四角色 prompt 組裝（假 llm_call，不碰網路）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.roles import RoleOutputError, extract_json_block
from ai_desk.roles import (
    run_bear_researcher,
    run_bull_researcher,
    run_technical_analyst,
    run_trader_judge,
)


def test_extracts_last_fenced_json():
    text = (
        "前面是一大段自然語言論述。\n"
        '```json\n{"foo": 1}\n```\n'
        "中間還有話。\n"
        '```json\n{"direction": 1, "note": "最後一段才算"}\n```\n'
    )
    data = extract_json_block(text, required_keys=["direction"])
    assert data["direction"] == 1
    assert data["note"] == "最後一段才算"


def test_missing_block_raises():
    with pytest.raises(RoleOutputError, match="fenced"):
        extract_json_block("只有自然語言，沒有 JSON。", required_keys=[])


def test_bad_json_raises():
    with pytest.raises(RoleOutputError, match="解析失敗"):
        extract_json_block('```json\n{壞掉的json}\n```', required_keys=[])


def test_missing_required_key_raises():
    with pytest.raises(RoleOutputError, match="direction"):
        extract_json_block('```json\n{"confidence": 0.5}\n```',
                           required_keys=["direction", "confidence"])


def fake_llm(reply):
    """回傳固定回覆的假 llm_call，並記錄收到的 prompt。"""
    calls = []

    def call(prompt):
        calls.append(prompt)
        return reply

    call.calls = calls
    return call


def test_technical_analyst_passes_briefing_and_parses():
    llm = fake_llm('市場結構論述。\n```json\n{"structure_summary": "區間震盪"}\n```')
    out = run_technical_analyst("收盤價：63500.00", llm)
    assert "63500.00" in llm.calls[0]          # 簡報有進 prompt
    assert "客觀" in llm.calls[0]               # prompt 要求客觀、不帶方向
    assert out.data["structure_summary"] == "區間震盪"
    assert "市場結構論述" in out.full_text


def test_bull_gets_analysis_and_memory():
    llm = fake_llm('多方論述。\n```json\n{"points": ["支撐守住"]}\n```')
    out = run_bull_researcher("分析文", "上輪記憶文", llm)
    assert "分析文" in llm.calls[0] and "上輪記憶文" in llm.calls[0]
    assert out.data["points"] == ["支撐守住"]


def test_bear_gets_bull_argument_to_rebut():
    llm = fake_llm('空方論述。\n```json\n{"rebuttals": ["支撐已破"], "points": ["量能萎縮"]}\n```')
    out = run_bear_researcher("分析文", "多方論點全文", "記憶文", llm)
    assert "多方論點全文" in llm.calls[0]       # 空方必須看到多方論點才能反駁
    assert out.data["rebuttals"] == ["支撐已破"]


def test_judge_requires_full_proposal_fields():
    llm = fake_llm(
        '綜合判斷。\n```json\n'
        '{"direction": -1, "confidence": 0.6, "entry": 63500, '
        '"stop": 64500, "take_profit": 61500, "rationale": "空方論點較強"}\n```'
    )
    out = run_trader_judge("分析文", "多方文", "空方文", llm)
    assert out.data["direction"] == -1
    assert "多方文" in llm.calls[0] and "空方文" in llm.calls[0]


def test_judge_missing_stop_raises():
    llm = fake_llm('```json\n{"direction": 1, "confidence": 0.5, "entry": 1, '
                   '"take_profit": 2, "rationale": "x"}\n```')
    with pytest.raises(RoleOutputError, match="stop"):
        run_trader_judge("a", "b", "c", llm)
