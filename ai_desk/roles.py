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


_ANALYST_PROMPT = """你是加密貨幣合約市場的技術分析師。以下是由程式計算好的市場事實簡報（所有數字皆為因果計算、無前視）：

{briefing}

請客觀描述目前的市場結構（趨勢狀態、動能、關鍵水位、波動狀況）。
規則：
- 只描述結構，禁止給出做多/做空/觀望的方向性建議。
- 只根據簡報中的數字，不要臆測簡報以外的資訊。
- 全程使用繁體中文。

回覆末尾必須附上一段 ```json fenced block，格式：
```json
{{"structure_summary": "一句話總結市場結構"}}
```"""

_BULL_PROMPT = """你是多方研究員。你的任務是根據技術分析，提出「最強的看多論點」。

技術分析師的市場結構分析：
{analysis}

過去幾輪的判斷記錄（含當時價格，可對照後續實際走勢檢討）：
{memory}

規則：
- 列出 2-4 點最強看多論點，每點必須引用分析中的具體事實。
- 若歷史記錄顯示先前判斷錯誤，明確承認並說明這次為何不同。
- 全程使用繁體中文。

回覆末尾必須附上 ```json fenced block，格式：
```json
{{"points": ["論點一", "論點二"]}}
```"""

_BEAR_PROMPT = """你是空方研究員。你的任務是直接反駁多方論點，並提出「最強的看空論點」。

技術分析師的市場結構分析：
{analysis}

多方研究員的論點（你必須逐點回應）：
{bull}

過去幾輪的判斷記錄：
{memory}

規則：
- 先逐點反駁多方論點（rebuttals），再列出 2-4 點獨立的看空論點（points）。
- 每點必須引用分析中的具體事實。
- 全程使用繁體中文。

回覆末尾必須附上 ```json fenced block，格式：
```json
{{"rebuttals": ["對多方論點一的反駁"], "points": ["看空論點一"]}}
```"""

_JUDGE_PROMPT = """你是交易員兼裁判。你讀完了技術分析與多空雙方的完整辯論，現在要做最終判斷。

技術分析：
{analysis}

多方論點：
{bull}

空方論點與反駁：
{bear}

規則：
- 明確評判哪一方論點較強、為什麼。
- 若雙方論點都不夠強，direction 給 0（觀望）——觀望是合法且常常正確的選擇。
- direction 非 0 時，entry/stop/take_profit 必須是具體價格：
  做多（1）：stop 低於 entry；做空（-1）：stop 高於 entry。
- confidence 介於 0 到 1。
- 這只是「提案」，會經過確定性風控與人工核准，不會直接成交。
- 全程使用繁體中文。

回覆末尾必須附上 ```json fenced block，格式：
```json
{{"direction": 1, "confidence": 0.65, "entry": 63500.0, "stop": 62800.0,
  "take_profit": 65000.0, "rationale": "一句話總結判斷依據"}}
```"""


def _run(prompt: str, llm_call, required_keys) -> RoleOutput:
    text = llm_call(prompt)
    data = extract_json_block(text, required_keys)
    return RoleOutput(full_text=text, data=data)


def run_technical_analyst(briefing_text: str, llm_call) -> RoleOutput:
    return _run(_ANALYST_PROMPT.format(briefing=briefing_text),
                llm_call, ["structure_summary"])


def run_bull_researcher(analysis_text: str, memory_text: str, llm_call) -> RoleOutput:
    return _run(_BULL_PROMPT.format(analysis=analysis_text, memory=memory_text),
                llm_call, ["points"])


def run_bear_researcher(analysis_text: str, bull_text: str, memory_text: str,
                        llm_call) -> RoleOutput:
    return _run(_BEAR_PROMPT.format(analysis=analysis_text, bull=bull_text,
                                    memory=memory_text),
                llm_call, ["rebuttals", "points"])


def run_trader_judge(analysis_text: str, bull_text: str, bear_text: str,
                     llm_call) -> RoleOutput:
    return _run(_JUDGE_PROMPT.format(analysis=analysis_text, bull=bull_text,
                                     bear=bear_text),
                llm_call,
                ["direction", "confidence", "entry", "stop",
                 "take_profit", "rationale"])
