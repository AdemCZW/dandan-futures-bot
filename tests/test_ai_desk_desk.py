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


def test_on_progress_fires_once_per_role_in_order(deps):
    df = make_df()
    judge = ('{"direction": 0, "confidence": 0.3, "entry": 0, "stop": 0, '
             '"take_profit": 0, "rationale": "觀望"}')
    events = []
    run_one_cycle(df, "BTCUSDT", "4h", llm_call=scripted_llm(judge),
                  on_progress=lambda role, text: events.append((role, text)),
                  **deps)
    roles = [e[0] for e in events]
    assert roles == ["analyst", "bull", "bear", "judge"]   # 依序各回報一次
    texts = {e[0]: e[1] for e in events}
    assert "結構論述" in texts["analyst"]                   # 回報的是角色全文
    assert "空方論述" in texts["bear"]


def test_on_progress_defaults_to_noop(deps):
    """不傳 on_progress 時行為不變（向後相容）。"""
    df = make_df()
    judge = ('{"direction": 0, "confidence": 0.3, "entry": 0, "stop": 0, '
             '"take_profit": 0, "rationale": "觀望"}')
    r = run_one_cycle(df, "BTCUSDT", "4h", llm_call=scripted_llm(judge), **deps)
    assert set(r.debate) == {"analyst", "bull", "bear", "judge"}


def test_records_llm_model_on_proposal(deps):
    """提案要留下當時用的模型（llm_call 有 .model 就記下來），否則樣本無法分組比較。"""
    df = make_df()
    price = float(df["close"].iloc[-1])
    judge = (f'{{"direction": 1, "confidence": 0.7, "entry": {price:.2f}, '
             f'"stop": {price * 0.95:.2f}, "take_profit": {price * 1.1:.2f}, '
             f'"rationale": "多方勝"}}')
    llm = scripted_llm(judge)
    llm.model = "claude-opus-4-8"               # 仿 ClaudeCliClient 帶 .model
    r = run_one_cycle(df, "BTCUSDT", "4h", llm_call=llm, **deps)
    row = deps["approval_store"].get(r.proposal_id)
    assert row["model"] == "claude-opus-4-8"
