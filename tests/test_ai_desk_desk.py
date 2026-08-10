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


def test_cycle_passes_fib_pos_to_risk_clamp(deps, monkeypatch):
    """desk 必須把簡報的 fib_pos 傳進風控夾限。

    沒接線的話，進場區位閘門開啟後會永遠收到 None、把所有提案都擋掉
    （缺值視為不通過），變成靜默停擺而不是有紀律的過濾。
    """
    import ai_desk.desk as desk_mod
    from ai_desk.briefing import build_market_briefing

    captured = {}
    real_clamp = desk_mod.clamp_with_risk_officer

    def spy(proposal, officer, equity, atr=None, fib_pos=None):
        captured["fib_pos"] = fib_pos
        return real_clamp(proposal, officer, equity, atr=atr, fib_pos=fib_pos)

    monkeypatch.setattr(desk_mod, "clamp_with_risk_officer", spy)

    df = make_df()
    llm = scripted_llm('{"direction": -1, "confidence": 0.6, "entry": 100.0, '
                       '"stop": 105.0, "take_profit": 90.0, "rationale": "測試"}')
    run_one_cycle(df, "BTCUSDT", "4h", llm_call=llm, **deps)

    expected = build_market_briefing(df, "BTCUSDT", "4h").fib_pos
    assert captured["fib_pos"] == pytest.approx(expected)


def test_cycle_records_judge_hedge_count(deps):
    """desk 要把裁判的警告語數量一併存進提案，供日後與進場區位一併檢驗。"""
    df = make_df()
    judge_body = ('此處做空已遲到、不宜追空，現價進場等於接刀，R/R 極差。\n'
                  '```json\n{"direction": -1, "confidence": 0.6, "entry": 100.0, '
                  '"stop": 105.0, "take_profit": 90.0, "rationale": "測試"}\n```')
    replies = ['結構。\n```json\n{"structure_summary": "震盪"}\n```',
               '多方。\n```json\n{"points": ["A"]}\n```',
               '空方。\n```json\n{"rebuttals": ["A"], "points": ["B"]}\n```',
               judge_body]
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return replies[len(calls) - 1]

    result = run_one_cycle(df, "BTCUSDT", "4h", llm_call=llm, **deps)
    assert result.proposal_id is not None
    row = deps["approval_store"].get(result.proposal_id)
    assert row["judge_hedges"] >= 3          # 遲到 + 不宜追 + 接刀 + 賠率差


def test_cycle_injects_real_outcomes_into_memory_prompt(deps):
    """desk 必須把 approval_store + 真實 K 線傳給記憶格式化，讓提示詞裡出現真實
    結算結果，而不是只能靠模型自己拿「當時價 vs 現價」瞎猜上次對不對。"""
    df = make_df()
    memory = deps["memory"]
    store = deps["approval_store"]

    # 先塞一筆「過去的判斷」+ 對應的核准紀錄，模擬上一輪已經進了佇列
    past_ts = str(df.index[100])
    memory.append({"ts": past_ts, "direction": -1, "confidence": 0.5,
                   "rationale_summary": "上一輪判斷", "price_at_decision": 100.0})
    from ai_desk.proposal import TradeProposal
    # entry 設在遠低於後續 K 線最高價之下，確保一定成交且觸及停損（df 是隨機遊走，
    # 用寬鬆的停損/停利避免這裡本身受隨機性影響——只是要讓 attach_outcomes 有東西可查）
    hi = float(df["high"].iloc[100:].max())
    lo = float(df["low"].iloc[100:].min())
    p = TradeProposal("BTCUSDT", ts=past_ts, direction=-1, confidence=0.5,
                      entry=hi, stop=hi + 1, take_profit=lo, rationale="r")
    store.add(p, qty=1.0, debate_full_text="全文")

    captured = {}
    real_llm = scripted_llm('{"direction": 0, "confidence": 0.5, "entry": null, '
                            '"stop": null, "take_profit": null, "rationale": "觀望"}')

    def spy_llm(prompt):
        if "上一輪判斷" in prompt:
            captured["prompt"] = prompt
        return real_llm(prompt)

    run_one_cycle(df, "BTCUSDT", "4h", llm_call=spy_llm, **deps)

    assert "prompt" in captured, "記憶文字沒有被注入任何一輪的 prompt"
    assert "結果" in captured["prompt"]
