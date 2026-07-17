"""四角色辯論 — prompt 組裝、LLM 呼叫（注入）、結構化輸出解析。

輸出契約：每個角色的回覆末尾必須附一段 ```json fenced block。
程式只解析最後一段 JSON；前面的自然語言全文保留供人工閱讀與存檔，
不參與程式邏輯。解析失敗 → RoleOutputError，該輪直接判定提案失敗，
不產生半殘 proposal。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


class RoleOutputError(Exception):
    """角色輸出不符契約（缺 JSON 區塊 / 壞 JSON / 缺必要欄位）。"""


@dataclass
class RoleOutput:
    full_text: str   # 角色原始回覆全文（含論述），供人工閱讀與記憶存檔
    data: dict       # 解析出的結構化欄位


def extract_json_block(text: str, required_keys) -> dict:
    matches = _JSON_BLOCK.findall(text)
    if not matches:
        raise RoleOutputError("回覆缺少 ```json fenced block（輸出契約違反）")
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError as e:
        raise RoleOutputError(f"JSON 解析失敗: {e}") from e
    missing = set(required_keys) - set(data)
    if missing:
        raise RoleOutputError(f"缺少必要欄位: {sorted(missing)}")
    return data
