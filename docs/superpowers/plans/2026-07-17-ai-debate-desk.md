# ai_desk（AI 分析辯論台）Phase 1 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依 `docs/superpowers/specs/2026-07-17-ai-debate-desk-design.md`，打造獨立套件 `ai_desk/`：LLM 四角色辯論（技術分析→多方→空方→裁判）產生交易提案，經既有風控官夾限 + 人工核准狀態機，僅限 Binance Futures Testnet。

**Architecture:** 新頂層套件 `ai_desk/` 與 `core/` 並存、零修改既有程式。LLM 呼叫透過 `llm_call: Callable[[str], str]` 依賴注入，所有膠水邏輯可離線測試。記憶層 = JSON Lines；核准層 = SQLite 狀態機；風控直接呼叫既有 `core.risk_officer.RiskOfficer`。

**Tech Stack:** Python 3.12（uv venv）、pandas、pytest、anthropic SDK（新依賴，只進 `requirements-ai.txt`，不進 `requirements.txt`——避免改動 9 台生產 bot 的 Docker 映像）。

## Global Constraints

- 僅限 Binance Futures **Testnet**，虛擬資金（spec 不可退讓原則 1）。
- AI 只產生 `TradeProposal`，永不直接執行；執行接線（approved → futures_execution_engineer）屬 Phase 2，**本計畫不含**。
- 不修改 `core/`、`run_once.py`、`run_multi_futures.py`、`requirements.txt`、任何既有檔案（spec 原則 3、5）。
- 禁止任何歷史回測程式碼或宣稱（spec 原則 4）。
- 全程 TDD：先寫失敗測試，跑過再 commit；commit 訊息用中文（repo 慣例）。
- 測試不得呼叫任何真實網路/API（anthropic 用注入假 client；K 線用合成資料）。
- Phase 1 範圍：BTCUSDT + 4h 單一組合；無 Telegram/儀表板；核准走 CLI。

---

## 檔案結構總覽

| 檔案 | 動作 | 職責 |
|---|---|---|
| `ai_desk/__init__.py` | Create | 空套件標記 |
| `ai_desk/briefing.py` | Create | 指標 → LLM 可讀簡報（純函式） |
| `ai_desk/memory.py` | Create | ThesisMemory（JSON Lines 讀寫） |
| `ai_desk/roles.py` | Create | JSON 解析器 + 四角色 prompt/呼叫 |
| `ai_desk/llm_client.py` | Create | 唯一真實呼叫 Anthropic API 之處 |
| `ai_desk/proposal.py` | Create | TradeProposal + 風控夾限 |
| `ai_desk/approval.py` | Create | SQLite 核准狀態機 + `__main__` CLI |
| `ai_desk/desk.py` | Create | run_one_cycle 編排（全依賴注入） |
| `run_ai_desk_once.py` | Create | 單輪進入點（仿 run_once.py 慣例） |
| `requirements-ai.txt` | Create | `-r requirements.txt` + `anthropic>=0.40` |
| `tests/test_ai_desk_*.py` | Create | 各模組測試 |

執行環境注意：repo 測試以 `python -m pytest`（現有 venv）執行。每個測試檔開頭沿用既有慣例：

```python
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

---

### Task 1: 套件骨架 + `briefing.py`（市場簡報）

**Files:**
- Create: `ai_desk/__init__.py`（空檔）
- Create: `ai_desk/briefing.py`
- Create: `requirements-ai.txt`
- Test: `tests/test_ai_desk_briefing.py`

**Interfaces:**
- Consumes: `core.signal_engineer.enrich(df)`（附加 ema_fast/ema_slow/rsi/atr/zscore/fib_pos/fib_382/fib_618 欄位）、`core.signal_engineer.htf_trend(df)`（回傳 +1/-1/0 int Series）。兩者都要求 df 有 DatetimeIndex 與 open/high/low/close/volume 欄位。
- Produces: `MarketBriefing` dataclass、`build_market_briefing(df, symbol, interval) -> MarketBriefing`、`format_briefing(b) -> str`。Task 8（desk.py）與 Task 9（進入點）依賴這三者。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_briefing.py
"""ai_desk.briefing 測試 — 合成資料驗證簡報欄位與 NaN 防護，不碰網路。"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.briefing import MarketBriefing, build_market_briefing, format_briefing


def make_df(n=300, seed=7):
    """合成 4h OHLCV 隨機漫步，DatetimeIndex。"""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    vol = rng.uniform(100, 200, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def test_build_briefing_fields():
    df = make_df()
    b = build_market_briefing(df, "BTCUSDT", "4h")
    assert isinstance(b, MarketBriefing)
    assert b.symbol == "BTCUSDT" and b.interval == "4h"
    assert b.close == pytest.approx(float(df["close"].iloc[-1]))
    assert b.as_of == str(df.index[-1])
    # 指標皆為有限 float
    for v in (b.ema_fast, b.ema_slow, b.rsi, b.atr, b.zscore, b.fib_pos):
        assert np.isfinite(v)
    assert 0.0 <= b.rsi <= 100.0
    assert b.htf_trend in (-1, 0, 1)
    assert len(b.recent_closes) == 6


def test_build_briefing_rejects_short_data():
    df = make_df(n=100)
    with pytest.raises(ValueError, match="不足"):
        build_market_briefing(df, "BTCUSDT", "4h")


def test_format_briefing_contains_facts():
    df = make_df()
    b = build_market_briefing(df, "BTCUSDT", "4h")
    text = format_briefing(b)
    assert "BTCUSDT" in text and "4h" in text
    assert f"{b.close:.2f}" in text
    assert "RSI" in text
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_briefing.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ai_desk'`）

- [ ] **Step 3: 最小實作**

建立空檔 `ai_desk/__init__.py`，建立 `requirements-ai.txt`：

```
-r requirements.txt
anthropic>=0.40
```

建立 `ai_desk/briefing.py`：

```python
"""市場簡報 — 把 signal_engineer 算好的指標轉成給 LLM 讀的結構化事實。

刻意不把原始 OHLCV 丟給 LLM：模型自己「算」指標既不可靠也浪費 token，
所有數字都來自 core.signal_engineer 已驗證過的因果計算。純函式、不碰網路。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from core import signal_engineer as se

MIN_BARS = 200  # 指標暖機下限（ema26/rsi14/zscore50/fib50 皆綽綽有餘）


@dataclass
class MarketBriefing:
    symbol: str
    interval: str
    as_of: str            # 最後一根已收盤 K 棒的時間
    close: float
    ema_fast: float       # EMA12
    ema_slow: float       # EMA26
    rsi: float
    atr: float
    zscore: float
    fib_pos: float        # 0=區間低點, 1=區間高點
    fib_382: float
    fib_618: float
    htf_trend: int        # 日線趨勢 +1/-1/0（已 shift、無前視）
    recent_closes: list   # 最近 6 根收盤價


def build_market_briefing(df: pd.DataFrame, symbol: str, interval: str) -> MarketBriefing:
    """df：已收盤 K 棒（DatetimeIndex + open/high/low/close/volume）。

    任一關鍵指標為 NaN → 拋 ValueError（寧可整輪失敗，不餵 LLM 髒資料）。
    """
    if len(df) < MIN_BARS:
        raise ValueError(f"K 棒不足（{len(df)} 根），指標暖機至少需要 {MIN_BARS} 根")
    enriched = se.enrich(df)
    enriched["htf_trend"] = se.htf_trend(df)
    last = enriched.iloc[-1]
    for name in ("ema_fast", "ema_slow", "rsi", "atr", "zscore", "fib_pos"):
        if math.isnan(float(last[name])):
            raise ValueError(f"指標 {name} 為 NaN（暖機不足或資料異常），拒絕產生簡報")
    return MarketBriefing(
        symbol=symbol,
        interval=interval,
        as_of=str(df.index[-1]),
        close=float(last["close"]),
        ema_fast=float(last["ema_fast"]),
        ema_slow=float(last["ema_slow"]),
        rsi=float(last["rsi"]),
        atr=float(last["atr"]),
        zscore=float(last["zscore"]),
        fib_pos=float(last["fib_pos"]),
        fib_382=float(last["fib_382"]),
        fib_618=float(last["fib_618"]),
        htf_trend=int(last["htf_trend"]),
        recent_closes=[float(x) for x in df["close"].iloc[-6:]],
    )


def format_briefing(b: MarketBriefing) -> str:
    """給 LLM 的純文字簡報。只陳述事實，不帶方向暗示。"""
    trend_txt = {1: "多頭", -1: "空頭", 0: "中性/暖機中"}[b.htf_trend]
    recent = " → ".join(f"{x:.2f}" for x in b.recent_closes)
    return (
        f"標的：{b.symbol}（{b.interval} 週期）\n"
        f"資料時間：{b.as_of}（最後一根已收盤 K 棒）\n"
        f"收盤價：{b.close:.2f}\n"
        f"EMA12：{b.ema_fast:.2f}／EMA26：{b.ema_slow:.2f}\n"
        f"RSI(14)：{b.rsi:.1f}\n"
        f"ATR(14)：{b.atr:.2f}\n"
        f"收盤價 Z 分數(50)：{b.zscore:.2f}\n"
        f"Fib 區間位置：{b.fib_pos:.3f}（0=區間低點, 1=區間高點）\n"
        f"Fib 38.2% 水位：{b.fib_382:.2f}／61.8% 水位：{b.fib_618:.2f}\n"
        f"日線趨勢：{trend_txt}\n"
        f"最近 6 根收盤：{recent}"
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_briefing.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/ requirements-ai.txt tests/test_ai_desk_briefing.py
git commit -m "feat(ai_desk): 套件骨架 + 市場簡報層（指標→LLM可讀事實，NaN防護）"
```

---

### Task 2: `memory.py`（判斷記憶）

**Files:**
- Create: `ai_desk/memory.py`
- Test: `tests/test_ai_desk_memory.py`

**Interfaces:**
- Consumes: 無（純檔案 IO）
- Produces: `ThesisMemory(dir_path, symbol, interval)`，方法 `append(entry: dict) -> None`、`load(n: int = 5) -> list[dict]`、`format_for_prompt(n: int = 5) -> str`。entry 必要鍵：`ts, direction, confidence, rationale_summary, price_at_decision`。Task 8 依賴。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_memory.py
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_memory.py -v`
Expected: FAIL（`ImportError: cannot import name 'ThesisMemory'`）

- [ ] **Step 3: 最小實作**

```python
# ai_desk/memory.py
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
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_memory.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/memory.py tests/test_ai_desk_memory.py
git commit -m "feat(ai_desk): 判斷記憶層（JSON Lines，下一輪注入prompt面對上輪結果）"
```

---

### Task 3: `roles.py` — JSON 輸出解析器

**Files:**
- Create: `ai_desk/roles.py`（本 task 只做解析器；四角色在 Task 4 同檔追加）
- Test: `tests/test_ai_desk_roles.py`

**Interfaces:**
- Consumes: 無
- Produces: `RoleOutputError(Exception)`、`extract_json_block(text: str, required_keys) -> dict`。Task 4 依賴。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_roles.py
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_roles.py -v`
Expected: FAIL（`ModuleNotFoundError` 或 `ImportError`）

- [ ] **Step 3: 最小實作**

```python
# ai_desk/roles.py
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
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_roles.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/roles.py tests/test_ai_desk_roles.py
git commit -m "feat(ai_desk): 角色輸出JSON契約解析器（缺欄位/壞JSON明確拋錯）"
```

---

### Task 4: `roles.py` — 四個辯論角色

**Files:**
- Modify: `ai_desk/roles.py`（追加四個 run_* 函式）
- Test: `tests/test_ai_desk_roles.py`（追加測試）

**Interfaces:**
- Consumes: Task 3 的 `extract_json_block` / `RoleOutput`；`llm_call: Callable[[str], str]` 由呼叫方注入。
- Produces（Task 8 依賴，簽名逐字）:
  - `run_technical_analyst(briefing_text: str, llm_call) -> RoleOutput`（data 必含 `structure_summary`）
  - `run_bull_researcher(analysis_text: str, memory_text: str, llm_call) -> RoleOutput`（data 必含 `points`）
  - `run_bear_researcher(analysis_text: str, bull_text: str, memory_text: str, llm_call) -> RoleOutput`（data 必含 `rebuttals`、`points`）
  - `run_trader_judge(analysis_text: str, bull_text: str, bear_text: str, llm_call) -> RoleOutput`（data 必含 `direction`、`confidence`、`entry`、`stop`、`take_profit`、`rationale`）

- [ ] **Step 1: 追加失敗測試**

在 `tests/test_ai_desk_roles.py` 追加：

```python
from ai_desk.roles import (
    run_bear_researcher,
    run_bull_researcher,
    run_technical_analyst,
    run_trader_judge,
)


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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_roles.py -v`
Expected: 新增 5 個測試 FAIL（`ImportError: cannot import name 'run_technical_analyst'`），原 4 個仍 PASS

- [ ] **Step 3: 實作四角色**

在 `ai_desk/roles.py` 追加：

```python
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
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_roles.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/roles.py tests/test_ai_desk_roles.py
git commit -m "feat(ai_desk): 四辯論角色（技術分析→多方→空方→裁判，全繁中prompt+JSON契約）"
```

---

### Task 5: `llm_client.py`（Anthropic API 呼叫器）

**Files:**
- Create: `ai_desk/llm_client.py`
- Test: `tests/test_ai_desk_llm_client.py`

**Interfaces:**
- Consumes: `anthropic` SDK（延遲 import——沒裝也不影響其他模組測試）
- Produces: `AnthropicLLMClient(model="claude-sonnet-5", max_tokens=2000, client=None)`，可呼叫物件 `__call__(prompt: str) -> str`。Task 9 依賴。`client` 參數供測試注入假物件。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_llm_client.py
"""ai_desk.llm_client 測試 — 注入假 client，不呼叫真實 API。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.llm_client import AnthropicLLMClient


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs

        class Resp:
            content = [FakeBlock("回覆內容")]

        return Resp()


class FakeAnthropic:
    def __init__(self):
        self.messages = FakeMessages()


def test_call_passes_prompt_and_returns_text():
    fake = FakeAnthropic()
    llm = AnthropicLLMClient(client=fake)
    out = llm("測試 prompt")
    assert out == "回覆內容"
    assert fake.messages.kwargs["messages"] == [{"role": "user", "content": "測試 prompt"}]
    assert fake.messages.kwargs["model"] == "claude-sonnet-5"
    assert fake.messages.kwargs["max_tokens"] == 2000


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicLLMClient()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_llm_client.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ai_desk.llm_client'`）

- [ ] **Step 3: 最小實作**

```python
# ai_desk/llm_client.py
"""唯一真正呼叫 Anthropic API 的地方。

按用量計費（與 Claude Max 訂閱分開的帳）。需要 env: ANTHROPIC_API_KEY。
測試時注入 client 參數即可完全離線。
"""
from __future__ import annotations

import os


class AnthropicLLMClient:
    def __init__(self, model: str = "claude-sonnet-5", max_tokens: int = 2000,
                 client=None):
        if client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "缺少 ANTHROPIC_API_KEY 環境變數。"
                    "ai_desk 直接呼叫 Anthropic API（按用量計費，與 Claude Max 訂閱分開），"
                    "請到 console.anthropic.com 取得 API key 後 export。"
                )
            import anthropic  # 延遲 import：沒裝 SDK 不影響其他模組
            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self.max_tokens = max_tokens

    def __call__(self, prompt: str) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_llm_client.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/llm_client.py tests/test_ai_desk_llm_client.py
git commit -m "feat(ai_desk): Anthropic API 呼叫器（延遲import、缺key明確報錯、可注入假client）"
```

---

### Task 6: `proposal.py`（TradeProposal + 風控夾限）

**Files:**
- Create: `ai_desk/proposal.py`
- Test: `tests/test_ai_desk_proposal.py`

**Interfaces:**
- Consumes: `core.risk_officer.RiskOfficer`（既有）：`check_entry(equity, price, ts, direction=1, atr=None, kelly_pct=None) -> RiskDecision`、`position_size(equity, price, stop_price, kelly_pct=None) -> float`；`core.risk_officer.RiskDecision(allow, quantity, reason)`；`config.Config`（測試 fixture 用）。
- Produces（Task 7/8 依賴）:
  - `TradeProposal` dataclass：`symbol: str, ts: str, direction: int, confidence: float, entry: float, stop: float, take_profit: float, rationale: str`
  - `proposal_from_judge(data: dict, symbol: str, ts: str) -> TradeProposal`（驗證失敗拋 `ValueError`）
  - `clamp_with_risk_officer(proposal, officer, equity, atr=None) -> RiskDecision`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_proposal.py
"""ai_desk.proposal 測試 — 提案驗證 + 與既有 RiskOfficer 的夾限整合。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from core.risk_officer import RiskOfficer

from ai_desk.proposal import TradeProposal, clamp_with_risk_officer, proposal_from_judge


@pytest.fixture
def officer():
    return RiskOfficer(Config())


def judge_data(**over):
    d = {"direction": 1, "confidence": 0.6, "entry": 100.0, "stop": 95.0,
         "take_profit": 110.0, "rationale": "測試"}
    d.update(over)
    return d


def test_valid_long_proposal():
    p = proposal_from_judge(judge_data(), "BTCUSDT", "2026-07-17T08:00:00Z")
    assert p.direction == 1 and p.entry == 100.0 and p.symbol == "BTCUSDT"


def test_long_stop_must_be_below_entry():
    with pytest.raises(ValueError, match="停損"):
        proposal_from_judge(judge_data(stop=105.0), "BTCUSDT", "t")


def test_short_stop_must_be_above_entry():
    with pytest.raises(ValueError, match="停損"):
        proposal_from_judge(judge_data(direction=-1, stop=95.0, take_profit=90.0),
                            "BTCUSDT", "t")


def test_bad_direction_and_confidence_rejected():
    with pytest.raises(ValueError):
        proposal_from_judge(judge_data(direction=2), "BTCUSDT", "t")
    with pytest.raises(ValueError):
        proposal_from_judge(judge_data(confidence=1.5), "BTCUSDT", "t")


def test_direction_zero_skips_price_validation():
    p = proposal_from_judge(judge_data(direction=0, entry=0, stop=0, take_profit=0),
                            "BTCUSDT", "t")
    assert p.direction == 0


def test_clamp_flat_proposal_not_allowed(officer):
    p = proposal_from_judge(judge_data(direction=0, entry=0, stop=0, take_profit=0),
                            "BTCUSDT", "t")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0)
    assert d.allow is False and "觀望" in d.reason


def test_clamp_uses_conservative_qty(officer):
    """AI 停損距離(5%)比預設固定停損(2%)寬 → AI 停損算出的 qty 較小 → 取較小者。"""
    p = proposal_from_judge(judge_data(), "BTCUSDT", "2026-07-17T08:00:00Z")
    d = clamp_with_risk_officer(p, officer, equity=10_000.0)
    assert d.allow is True
    qty_ai = officer.position_size(10_000.0, 100.0, 95.0)
    assert d.quantity == pytest.approx(qty_ai)


def test_clamp_respects_circuit_breaker(officer):
    """單日虧損熔斷觸發 → 不管 AI 信心多高一律拒絕。"""
    officer.check_entry(10_000.0, 100.0, "2026-07-17T00:00:00Z")   # 建立當日基準
    p = proposal_from_judge(judge_data(confidence=0.99), "BTCUSDT",
                            "2026-07-17T08:00:00Z")
    d = clamp_with_risk_officer(p, officer, equity=9_000.0)        # 當日 -10%
    assert d.allow is False and "熔斷" in d.reason
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_proposal.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ai_desk.proposal'`）

- [ ] **Step 3: 最小實作**

```python
# ai_desk/proposal.py
"""交易提案 — AI 裁判輸出的結構化提案 + 既有風控官夾限。

不重新實作任何風控規則：熔斷/清算守衛/倉位上限全部走既有
core.risk_officer.RiskOfficer（與規則策略同一套、同一實例邏輯）。
AI 的信心分數對風控沒有任何影響力。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.risk_officer import RiskDecision, RiskOfficer


@dataclass
class TradeProposal:
    symbol: str
    ts: str
    direction: int        # 1 多 / -1 空 / 0 觀望
    confidence: float     # 0~1（僅供人工核准參考，不影響風控）
    entry: float
    stop: float
    take_profit: float
    rationale: str


def proposal_from_judge(data: dict, symbol: str, ts: str) -> TradeProposal:
    """裁判 JSON → TradeProposal。驗證失敗拋 ValueError（該輪提案作廢）。"""
    direction = int(data["direction"])
    if direction not in (-1, 0, 1):
        raise ValueError(f"direction 必須是 -1/0/1，收到 {direction}")
    confidence = float(data["confidence"])
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence 必須在 0~1，收到 {confidence}")
    entry = float(data["entry"])
    stop = float(data["stop"])
    take_profit = float(data["take_profit"])
    if direction == 1 and not stop < entry:
        raise ValueError(f"多單停損({stop})必須低於進場價({entry})")
    if direction == -1 and not stop > entry:
        raise ValueError(f"空單停損({stop})必須高於進場價({entry})")
    return TradeProposal(symbol=symbol, ts=ts, direction=direction,
                         confidence=confidence, entry=entry, stop=stop,
                         take_profit=take_profit,
                         rationale=str(data["rationale"]))


def clamp_with_risk_officer(proposal: TradeProposal, officer: RiskOfficer,
                            equity: float, atr=None) -> RiskDecision:
    """既有風控官夾限：熔斷/清算守衛照走，倉位取「AI 停損」與「風控停損」
    兩種算法中較保守（較小）者。AI 信心分數不參與任何計算。"""
    if proposal.direction == 0:
        return RiskDecision(False, 0.0, "AI 建議觀望，不進場")
    gate = officer.check_entry(equity, proposal.entry, proposal.ts,
                               direction=proposal.direction, atr=atr)
    if not gate.allow:
        return gate
    qty_ai_stop = officer.position_size(equity, proposal.entry, proposal.stop)
    qty = min(gate.quantity, qty_ai_stop)
    if qty <= 0:
        return RiskDecision(False, 0.0, "風控算出倉位為 0")
    return RiskDecision(True, qty, "ok（倉位取 AI 停損與風控停損較保守者）")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_proposal.py -v`
Expected: 8 passed

同時跑既有風控測試確認零影響：`python -m pytest tests/test_risk_officer.py -q`
Expected: 全數 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/proposal.py tests/test_ai_desk_proposal.py
git commit -m "feat(ai_desk): TradeProposal + 既有RiskOfficer夾限（熔斷照走、倉位取保守者）"
```

---

### Task 7: `approval.py`（人工核准狀態機 + CLI）

**Files:**
- Create: `ai_desk/approval.py`
- Create: `ai_desk/__main__helpers` 不需要——CLI 直接寫在 `approval.py` 的 `if __name__ == "__main__"` 區塊（沿用 `core/trade_journal.py` 的「模組即小工具」慣例）
- Test: `tests/test_ai_desk_approval.py`

**Interfaces:**
- Consumes: Task 6 的 `TradeProposal`
- Produces（Task 8/9 依賴）: `ApprovalStore(db_path: str = "ai_desk_proposals.db")`，方法：
  - `add(proposal: TradeProposal, qty: float, debate_full_text: str) -> int`（回傳 proposal id，狀態 pending）
  - `pending() -> list[dict]`
  - `approve(pid: int) -> None`／`reject(pid: int) -> None`（僅 pending 可轉，否則 `ValueError`）
  - `approved_unexecuted() -> list[dict]`
  - `mark_executed(pid: int) -> None`（僅 approved 可轉，否則 `ValueError`）
- CLI：`python -m ai_desk.approval`（列 pending）；`python -m ai_desk.approval <id> approve|reject`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_approval.py
"""ai_desk.approval 測試 — SQLite 狀態機：pending→approved/rejected→executed。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.approval import ApprovalStore
from ai_desk.proposal import TradeProposal


def make_proposal(direction=1):
    return TradeProposal(symbol="BTCUSDT", ts="2026-07-17T08:00:00Z",
                         direction=direction, confidence=0.6,
                         entry=63500.0, stop=62800.0, take_profit=65000.0,
                         rationale="測試提案")


@pytest.fixture
def store(tmp_path):
    return ApprovalStore(str(tmp_path / "test.db"))


def test_add_creates_pending(store):
    pid = store.add(make_proposal(), qty=0.05, debate_full_text="完整辯論全文")
    rows = store.pending()
    assert len(rows) == 1
    assert rows[0]["id"] == pid
    assert rows[0]["status"] == "pending"
    assert rows[0]["qty"] == pytest.approx(0.05)
    assert rows[0]["rationale"] == "測試提案"


def test_approve_moves_to_approved_unexecuted(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    assert store.pending() == []
    rows = store.approved_unexecuted()
    assert len(rows) == 1 and rows[0]["id"] == pid


def test_reject_removes_from_both_queues(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.reject(pid)
    assert store.pending() == []
    assert store.approved_unexecuted() == []


def test_cannot_approve_twice(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    with pytest.raises(ValueError, match="pending"):
        store.approve(pid)


def test_cannot_execute_pending(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    with pytest.raises(ValueError, match="approved"):
        store.mark_executed(pid)


def test_executed_leaves_unexecuted_queue(store):
    pid = store.add(make_proposal(), 0.05, "全文")
    store.approve(pid)
    store.mark_executed(pid)
    assert store.approved_unexecuted() == []


def test_debate_full_text_persisted(store):
    pid = store.add(make_proposal(), 0.05, "四角色完整辯論全文…")
    row = store.get(pid)
    assert "四角色完整辯論全文" in row["debate_full_text"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_approval.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ai_desk.approval'`）

- [ ] **Step 3: 最小實作**

```python
# ai_desk/approval.py
"""人工核准閘門 — 借用 OpenAlice「Trading as Git」概念的最簡版本。

狀態機：pending → approved | rejected；approved → executed。
只有 approved 的提案才可能被送進執行層（Phase 2 接線）。

CLI 小工具：
    python -m ai_desk.approval              # 列出待核准提案（含辯論全文）
    python -m ai_desk.approval 3 approve    # 核准 id=3
    python -m ai_desk.approval 3 reject     # 拒絕 id=3
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from ai_desk.proposal import TradeProposal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_desk_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    direction INTEGER NOT NULL,
    confidence REAL NOT NULL,
    entry REAL NOT NULL,
    stop REAL NOT NULL,
    take_profit REAL NOT NULL,
    qty REAL NOT NULL,
    rationale TEXT NOT NULL,
    debate_full_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    decided_at TEXT
)
"""

_COLS = ["id", "symbol", "ts", "direction", "confidence", "entry", "stop",
         "take_profit", "qty", "rationale", "debate_full_text", "status",
         "created_at", "decided_at"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalStore:
    def __init__(self, db_path: str = "ai_desk_proposals.db"):
        self.db_path = db_path
        with self._conn() as c:
            c.execute(_SCHEMA)

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def add(self, proposal: TradeProposal, qty: float,
            debate_full_text: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO ai_desk_proposals "
                "(symbol, ts, direction, confidence, entry, stop, take_profit,"
                " qty, rationale, debate_full_text, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (proposal.symbol, proposal.ts, proposal.direction,
                 proposal.confidence, proposal.entry, proposal.stop,
                 proposal.take_profit, qty, proposal.rationale,
                 debate_full_text, _utc_now()))
            return cur.lastrowid

    def _rows(self, where: str, args=()) -> list:
        with self._conn() as c:
            cur = c.execute(
                f"SELECT {', '.join(_COLS)} FROM ai_desk_proposals "
                f"WHERE {where} ORDER BY id", args)
            return [dict(zip(_COLS, r)) for r in cur.fetchall()]

    def get(self, pid: int) -> dict:
        rows = self._rows("id = ?", (pid,))
        if not rows:
            raise ValueError(f"找不到提案 id={pid}")
        return rows[0]

    def pending(self) -> list:
        return self._rows("status = 'pending'")

    def approved_unexecuted(self) -> list:
        return self._rows("status = 'approved'")

    def _transition(self, pid: int, from_status: str, to_status: str) -> None:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE ai_desk_proposals SET status = ?, decided_at = ? "
                "WHERE id = ? AND status = ?",
                (to_status, _utc_now(), pid, from_status))
            if cur.rowcount != 1:
                raise ValueError(
                    f"提案 id={pid} 不在 {from_status} 狀態，無法轉為 {to_status}")

    def approve(self, pid: int) -> None:
        self._transition(pid, "pending", "approved")

    def reject(self, pid: int) -> None:
        self._transition(pid, "pending", "rejected")

    def mark_executed(self, pid: int) -> None:
        self._transition(pid, "approved", "executed")


def _cli(argv):
    store = ApprovalStore()
    if len(argv) == 0:
        rows = store.pending()
        if not rows:
            print("（無待核准提案）")
            return
        dir_txt = {1: "做多", -1: "做空"}
        for r in rows:
            print("=" * 60)
            print(f"提案 #{r['id']}  {r['symbol']}  {dir_txt.get(r['direction'])}"
                  f"  信心 {r['confidence']:.2f}  數量 {r['qty']}")
            print(f"進場 {r['entry']}  停損 {r['stop']}  停利 {r['take_profit']}")
            print(f"依據：{r['rationale']}")
            print(f"--- 完整辯論 ---\n{r['debate_full_text']}")
        print("=" * 60)
        print("核准：python -m ai_desk.approval <id> approve")
        print("拒絕：python -m ai_desk.approval <id> reject")
    elif len(argv) == 2 and argv[1] in ("approve", "reject"):
        pid = int(argv[0])
        getattr(store, argv[1])(pid)
        print(f"提案 #{pid} 已{'核准' if argv[1] == 'approve' else '拒絕'}")
    else:
        print("用法：python -m ai_desk.approval [<id> approve|reject]")
        sys.exit(2)


if __name__ == "__main__":
    _cli(sys.argv[1:])
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_approval.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add ai_desk/approval.py tests/test_ai_desk_approval.py
git commit -m "feat(ai_desk): 人工核准狀態機+CLI（pending→approved/rejected→executed，辯論全文留底）"
```

---

### Task 8: `desk.py`（單輪編排）

**Files:**
- Create: `ai_desk/desk.py`
- Test: `tests/test_ai_desk_desk.py`

**Interfaces:**
- Consumes: Task 1 `build_market_briefing`/`format_briefing`；Task 2 `ThesisMemory`；Task 4 四個 `run_*` 角色；Task 6 `proposal_from_judge`/`clamp_with_risk_officer`；Task 7 `ApprovalStore`。
- Produces（Task 9 依賴）:
  - `CycleResult` dataclass：`proposal: TradeProposal, risk: RiskDecision, proposal_id: int | None, debate: dict`（debate 鍵：`analyst/bull/bear/judge`，值為各角色全文）
  - `run_one_cycle(df, symbol, interval, *, llm_call, risk_officer, equity, memory, approval_store) -> CycleResult`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_desk.py
"""ai_desk.desk 整合測試 — 全依賴注入，一輪跑完：簡報→辯論→提案→風控→pending→記憶。"""
import os
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from core.risk_officer import RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.desk import CycleResult, run_one_cycle
from ai_desk.memory import ThesisMemory


def make_df(n=300, seed=7):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="4h")
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    close = np.maximum(close, 1.0)
    return pd.DataFrame(
        {"open": np.roll(close, 1), "high": close + 0.5, "low": close - 0.5,
         "close": close, "volume": rng.uniform(100, 200, n)},
        index=idx,
    )


def scripted_llm(judge_json):
    """依呼叫順序回覆四角色的假 llm_call。"""
    replies = [
        '結構論述。\n```json\n{"structure_summary": "震盪"}\n```',
        '多方論述。\n```json\n{"points": ["論點A"]}\n```',
        '空方論述。\n```json\n{"rebuttals": ["反駁A"], "points": ["論點B"]}\n```',
        f'裁決論述。\n```json\n{judge_json}\n```',
    ]
    calls = []

    def call(prompt):
        calls.append(prompt)
        return replies[len(calls) - 1]

    call.calls = calls
    return call


@pytest.fixture
def deps(tmp_path):
    return {
        "risk_officer": RiskOfficer(Config()),
        "equity": 10_000.0,
        "memory": ThesisMemory(str(tmp_path / "mem"), "BTCUSDT", "4h"),
        "approval_store": ApprovalStore(str(tmp_path / "p.db")),
    }


def test_full_cycle_directional_proposal_lands_in_pending(deps):
    df = make_df()
    price = float(df["close"].iloc[-1])
    judge = (f'{{"direction": 1, "confidence": 0.7, "entry": {price:.2f}, '
             f'"stop": {price * 0.95:.2f}, "take_profit": {price * 1.1:.2f}, '
             f'"rationale": "多方勝"}}')
    llm = scripted_llm(judge)
    r = run_one_cycle(df, "BTCUSDT", "4h", llm_call=llm, **deps)
    assert isinstance(r, CycleResult)
    assert len(llm.calls) == 4                      # 四角色各一次呼叫
    assert r.proposal.direction == 1
    assert r.risk.allow is True
    assert r.proposal_id is not None
    pend = deps["approval_store"].pending()
    assert len(pend) == 1 and pend[0]["id"] == r.proposal_id
    assert "結構論述" in pend[0]["debate_full_text"]    # 全文留底
    assert set(r.debate) == {"analyst", "bull", "bear", "judge"}
    # 記憶已寫入
    mem = deps["memory"].load()
    assert len(mem) == 1 and mem[0]["direction"] == 1


def test_flat_proposal_skips_approval_but_writes_memory(deps):
    df = make_df()
    judge = ('{"direction": 0, "confidence": 0.3, "entry": 0, "stop": 0, '
             '"take_profit": 0, "rationale": "雙方都不夠強"}')
    r = run_one_cycle(df, "BTCUSDT", "4h", llm_call=scripted_llm(judge), **deps)
    assert r.proposal.direction == 0
    assert r.risk.allow is False
    assert r.proposal_id is None
    assert deps["approval_store"].pending() == []
    assert len(deps["memory"].load()) == 1          # 觀望也要記憶


def test_second_cycle_sees_first_cycle_memory(deps):
    df = make_df()
    judge = ('{"direction": 0, "confidence": 0.3, "entry": 0, "stop": 0, '
             '"take_profit": 0, "rationale": "第一輪觀望"}')
    run_one_cycle(df, "BTCUSDT", "4h", llm_call=scripted_llm(judge), **deps)
    llm2 = scripted_llm(judge)
    run_one_cycle(df, "BTCUSDT", "4h", llm_call=llm2, **deps)
    assert "第一輪觀望" in llm2.calls[1]             # 多方 prompt 含上輪記憶
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_desk.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'ai_desk.desk'`）

- [ ] **Step 3: 最小實作**

```python
# ai_desk/desk.py
"""單輪編排 — 簡報 → 四角色辯論 → 提案 → 風控夾限 → pending → 記憶。

所有外部依賴（llm_call / risk_officer / memory / approval_store）注入，
本模組不 import anthropic、不碰網路。AI 永遠只提案：這裡的產出最遠只到
approval_store 的 pending 狀態，執行接線屬 Phase 2。
"""
from __future__ import annotations

from dataclasses import dataclass

from core.risk_officer import RiskDecision, RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.briefing import build_market_briefing, format_briefing
from ai_desk.memory import ThesisMemory
from ai_desk.proposal import TradeProposal, clamp_with_risk_officer, proposal_from_judge
from ai_desk.roles import (
    run_bear_researcher,
    run_bull_researcher,
    run_technical_analyst,
    run_trader_judge,
)


@dataclass
class CycleResult:
    proposal: TradeProposal
    risk: RiskDecision
    proposal_id: int | None   # 進了 pending 才有值
    debate: dict              # {"analyst"/"bull"/"bear"/"judge": 全文}


def run_one_cycle(df, symbol: str, interval: str, *,
                  llm_call, risk_officer: RiskOfficer, equity: float,
                  memory: ThesisMemory,
                  approval_store: ApprovalStore) -> CycleResult:
    briefing = build_market_briefing(df, symbol, interval)
    briefing_text = format_briefing(briefing)
    memory_text = memory.format_for_prompt()

    analyst = run_technical_analyst(briefing_text, llm_call)
    bull = run_bull_researcher(analyst.full_text, memory_text, llm_call)
    bear = run_bear_researcher(analyst.full_text, bull.full_text,
                               memory_text, llm_call)
    judge = run_trader_judge(analyst.full_text, bull.full_text,
                             bear.full_text, llm_call)

    proposal = proposal_from_judge(judge.data, symbol, briefing.as_of)
    risk = clamp_with_risk_officer(proposal, risk_officer, equity,
                                   atr=briefing.atr)

    debate = {"analyst": analyst.full_text, "bull": bull.full_text,
              "bear": bear.full_text, "judge": judge.full_text}

    proposal_id = None
    if risk.allow:
        full_text = "\n\n".join(
            f"【{k}】\n{v}" for k, v in debate.items())
        proposal_id = approval_store.add(proposal, risk.quantity, full_text)

    memory.append({
        "ts": proposal.ts,
        "direction": proposal.direction,
        "confidence": proposal.confidence,
        "rationale_summary": proposal.rationale,
        "price_at_decision": briefing.close,
    })
    return CycleResult(proposal=proposal, risk=risk,
                       proposal_id=proposal_id, debate=debate)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_desk.py -v`
Expected: 3 passed

再跑全套確認零回歸：`python -m pytest tests/ -q`
Expected: 全數 passed（含既有測試）

- [ ] **Step 5: Commit**

```bash
git add ai_desk/desk.py tests/test_ai_desk_desk.py
git commit -m "feat(ai_desk): 單輪編排（簡報→四角色辯論→提案→風控→pending→記憶，全依賴注入）"
```

---

### Task 9: `run_ai_desk_once.py`（進入點）+ 首次真實試跑

**Files:**
- Create: `run_ai_desk_once.py`（repo 根目錄，仿 `run_once.py` 慣例）
- Test: `tests/test_ai_desk_entrypoint.py`

**Interfaces:**
- Consumes: `core.market_analyst.fetch_klines(client, symbol, interval, limit=500, futures=False) -> pd.DataFrame`（回傳含 `open_time` 欄位的 DataFrame，需 `set_index("open_time")`；最後一根是未收盤 K 棒，需丟棄）；Task 5 `AnthropicLLMClient`；Task 8 `run_one_cycle`。
- Produces: `prepare_df(raw: pd.DataFrame) -> pd.DataFrame`（設索引 + 丟未收盤根，抽成純函式以利測試）、`main()` 進入點。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_ai_desk_entrypoint.py
"""run_ai_desk_once 測試 — 只測純函式 prepare_df（設索引+丟未收盤根），不碰網路。"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from run_ai_desk_once import prepare_df


def test_prepare_df_sets_index_and_drops_open_bar():
    n = 10
    raw = pd.DataFrame({
        "open_time": pd.date_range("2026-07-01", periods=n, freq="4h"),
        "open": np.ones(n), "high": np.ones(n), "low": np.ones(n),
        "close": np.ones(n), "volume": np.ones(n),
    })
    df = prepare_df(raw)
    assert len(df) == n - 1                          # 最後一根（未收盤）被丟掉
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index[-1] == raw["open_time"].iloc[-2]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python -m pytest tests/test_ai_desk_entrypoint.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'run_ai_desk_once'`）

- [ ] **Step 3: 最小實作**

```python
# run_ai_desk_once.py
"""ai_desk 單輪進入點 — 抓真實合約 K 線，跑一輪四角色辯論，提案進 pending。

Phase 1：本機手動執行、人工肉眼檢視辯論品質。不排程、不執行任何下單。
核准後的提案執行接線屬 Phase 2（本腳本只列印 approved 未執行清單提醒）。

用法：
    ANTHROPIC_API_KEY=sk-ant-... python run_ai_desk_once.py [SYMBOL] [INTERVAL]
    （預設 BTCUSDT 4h；K 線用幣安公開端點，不需交易金鑰）

費用注意：每輪 4 次 Anthropic API 呼叫，按用量計費（與 Claude Max 訂閱分開）。
"""
import os
import sys

import pandas as pd

from binance.client import Client
from config import Config
from core.market_analyst import fetch_klines
from core.risk_officer import RiskOfficer

from ai_desk.approval import ApprovalStore
from ai_desk.desk import run_one_cycle
from ai_desk.llm_client import AnthropicLLMClient
from ai_desk.memory import ThesisMemory

MEMORY_DIR = os.path.join("ai_desk", "memory")
EQUITY_FOR_SIZING = 10_000.0   # Phase 1 名目資金（測試網虛擬資金基準）


def prepare_df(raw: pd.DataFrame) -> pd.DataFrame:
    """fetch_klines 原始輸出 → DatetimeIndex + 丟掉最後一根未收盤 K 棒。"""
    return raw.set_index("open_time").iloc[:-1]


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "4h"

    llm = AnthropicLLMClient()                        # 缺 key 在這裡就報錯
    client = Client()                                 # 公開 K 線端點不需金鑰
    raw = fetch_klines(client, symbol, interval, limit=400, futures=True)
    df = prepare_df(raw)

    result = run_one_cycle(
        df, symbol, interval,
        llm_call=llm,
        risk_officer=RiskOfficer(Config()),
        equity=EQUITY_FOR_SIZING,
        memory=ThesisMemory(MEMORY_DIR, symbol, interval),
        approval_store=ApprovalStore(),
    )

    print("=" * 60)
    for role, text in result.debate.items():
        print(f"\n【{role}】\n{text}")
    print("=" * 60)
    p, r = result.proposal, result.risk
    dir_txt = {1: "做多", -1: "做空", 0: "觀望"}[p.direction]
    print(f"提案：{dir_txt}  信心 {p.confidence:.2f}")
    if p.direction != 0:
        print(f"進場 {p.entry}  停損 {p.stop}  停利 {p.take_profit}")
    print(f"風控：{'放行' if r.allow else '拒絕'}（{r.reason}）"
          + (f"  數量 {r.quantity:.6f}" if r.allow else ""))
    if result.proposal_id is not None:
        print(f"→ 已進入待核准佇列 #{result.proposal_id}；"
              f"檢視/核准：python -m ai_desk.approval")

    store = ApprovalStore()
    approved = store.approved_unexecuted()
    if approved:
        print(f"⚠ 有 {len(approved)} 筆已核准未執行的提案"
              f"（執行接線屬 Phase 2，目前需人工處理）")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `python -m pytest tests/test_ai_desk_entrypoint.py -v`
Expected: 1 passed

再跑全套：`python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 5: Commit**

```bash
git add run_ai_desk_once.py tests/test_ai_desk_entrypoint.py
git commit -m "feat(ai_desk): 單輪進入點 run_ai_desk_once.py（真實K線+四角色辯論+pending佇列）"
```

- [ ] **Step 6: 首次真實試跑（需使用者提供 ANTHROPIC_API_KEY，事前明確告知計費）**

先安裝依賴：`python -m pip install -r requirements-ai.txt`

向使用者確認取得 API key 並理解「按用量計費、與 Claude Max 訂閱分開」後：

Run: `ANTHROPIC_API_KEY=... python run_ai_desk_once.py BTCUSDT 4h`
Expected: 印出四角色完整辯論全文（繁體中文）、最終提案、風控結果；若方向非 0 且風控放行，`python -m ai_desk.approval` 可看到 pending 提案。

**這一步的產出（辯論全文品質）交給使用者肉眼評估——是 Phase 1 的核心驗收，不是自動化測試。**

- [ ] **Step 7: 記錄試跑結果**

把首輪真實辯論的觀察（品質、token 用量、實際費用）補記到 `docs/strategy_research_log.md`，commit：

```bash
git add docs/strategy_research_log.md
git commit -m "docs: ai_desk 首輪真實辯論試跑觀察（品質/token/費用）"
```

---

## Self-Review 記錄

- **Spec 覆蓋**：①簡報層=Task 1；②分析層=Task 3/4；③記憶層=Task 2；④風控閘門=Task 6；⑤核准閘門=Task 7；編排=Task 8；進入點=Task 9；`anthropic` 依賴=Task 1 的 `requirements-ai.txt`（進 requirements-ai 而非 requirements.txt，比 spec 原文更嚴格地滿足「與 9 台生產 bot 隔離」原則）。⑥執行層接線與 ⑦驗證層樣本統計屬 Phase 2/3，spec 的 rollout 表已明確排除於 Phase 1。
- **Placeholder 掃描**：無 TBD/TODO；每步含完整程式碼與預期輸出。
- **型別/簽名一致性**：`run_one_cycle` 的 keyword 參數、`RoleOutput.full_text/.data`、`ApprovalStore` 方法名、`TradeProposal` 欄位在 Task 6/7/8/9 間逐字一致。
