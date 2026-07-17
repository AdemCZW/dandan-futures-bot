"""ai_desk.roles 測試 — JSON 解析器 + 四角色 prompt 組裝（假 llm_call，不碰網路）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.roles import RoleOutputError, extract_json_block


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
