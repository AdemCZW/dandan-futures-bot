"""判斷記憶 — 每個 symbol×interval 一份 JSON Lines 檔。

借用 OpenAlice 持久筆記概念：把每輪的判斷寫下來，下一輪注入 prompt，
強迫模型面對「上次我說 X，之後價格實際走了 Y」。這是 context 注入，
不是模型訓練。
"""
from __future__ import annotations

import json
import os

from ai_desk.outcome import evaluate_outcome

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

    def format_for_prompt(self, n: int = 5, approval_rows=None, klines=None) -> str:
        """approval_rows/klines 提供時，附上真實結算結果（見 attach_outcomes）；
        不提供時行為與舊版逐位元相同（向後相容，預設不查結果）。
        """
        entries = self.load(n)
        if not entries:
            return "（無歷史判斷記錄——這是第一輪）"
        if approval_rows is not None and klines is not None:
            entries = attach_outcomes(entries, approval_rows, klines)
        dir_txt = {1: "看多", -1: "看空", 0: "觀望"}
        lines = [
            f"- {e['ts']} {dir_txt.get(e['direction'], '?')}"
            f"（信心 {e['confidence']:.2f}，當時價 {e['price_at_decision']:.2f}）："
            f"{e['rationale_summary']}{_outcome_suffix(e.get('outcome'))}"
            for e in entries
        ]
        return "\n".join(lines)


def _outcome_suffix(outcome: dict | None) -> str:
    if outcome is None:
        return ""
    state = outcome["state"]
    if state == "no_data":
        return ""
    label = {"stopped": "已觸及停損", "target": "已觸及停利",
             "unfilled": "未成交（限價單未觸發）", "open": "目前浮動未平倉"}[state]
    pnl_txt = f"（紙上損益 {outcome['pnl']:+.2f}）" if state != "unfilled" else ""
    return f" → 結果：{label}{pnl_txt}"


def attach_outcomes(entries: list, approval_rows: list, klines) -> list:
    """為記憶項附上真實結算結果，取代讓模型自己拿「當時價 vs 現價」的價差瞎猜。

    為什麼需要這個：純粹用價差推論「上次判斷對不對」是錯的——例如空單可能先
    反彈觸及停損、之後價格才續跌，起訖價差看起來「判斷是對的」，但那筆交易
    紙上結算其實是虧的。實測辯論文字裡模型確實會這樣自我確認方向（見
    docs/超能力.../dandan-ai-desk.md 的記憶回饋分析）。

    只有方向性（direction != 0）且風控放行的提案才會進 approval_rows；觀望的
    提案查無對應紀錄，outcome 留 None——不可假造一個結果出來誤導模型。
    以 ts 為聯集鍵（同一 symbol×interval 檔案內 ts 天然唯一）。
    """
    by_ts = {r["ts"]: r for r in approval_rows}
    out = []
    for e in entries:
        enriched = dict(e)
        row = by_ts.get(e["ts"])
        if row is None:
            enriched["outcome"] = None
        else:
            # 只看提案成立之後的 K 線——否則可能誤把提案「之前」剛好穿越過
            # entry 價位的舊 K 棒當成本輪成交，判出不存在的結果。
            since = klines[klines.index >= row["ts"]] if len(klines) else klines
            enriched["outcome"] = evaluate_outcome(
                direction=row["direction"], entry=row["entry"], stop=row["stop"],
                take_profit=row["take_profit"], qty=row["qty"], klines=since)
        out.append(enriched)
    return out
