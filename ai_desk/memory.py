"""判斷記憶 — 每個 symbol×interval 一份 JSON Lines 檔。

借用 OpenAlice 持久筆記概念：把每輪的判斷寫下來，下一輪注入 prompt，
強迫模型面對「上次我說 X，之後價格實際走了 Y」。這是 context 注入，
不是模型訓練。
"""
from __future__ import annotations

import json
import os

REQUIRED_KEYS = frozenset(
    {"ts", "direction", "confidence", "rationale_summary", "price_at_decision"}
)


class ThesisMemory:
    def __init__(self, dir_path: str, symbol: str, interval: str):
        os.makedirs(dir_path, exist_ok=True)
        self.path = os.path.join(dir_path, f"{symbol}_{interval}.jsonl")

    def append(self, entry: dict) -> None:
        missing = REQUIRED_KEYS - set(entry)
        if missing:
            raise ValueError(f"記憶項缺少必要欄位: {sorted(missing)}")
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def load(self, n: int = 5) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        return [json.loads(ln) for ln in lines[-n:]]

    def format_for_prompt(self, n: int = 5) -> str:
        entries = self.load(n)
        if not entries:
            return "（無歷史判斷記錄——這是第一輪）"
        dir_txt = {1: "看多", -1: "看空", 0: "觀望"}
        lines = [
            f"- {e['ts']} {dir_txt.get(e['direction'], '?')}"
            f"（信心 {e['confidence']:.2f}，當時價 {e['price_at_decision']:.2f}）："
            f"{e['rationale_summary']}"
            for e in entries
        ]
        return "\n".join(lines)
