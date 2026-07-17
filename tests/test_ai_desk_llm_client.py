"""ai_desk.llm_client 測試 — 兩個 client 都注入假物件，不碰網路/CLI/API。"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_desk.llm_client import AnthropicLLMClient, ClaudeCliClient


# ── 訂閱 CLI 版（Phase 1 預設）────────────────────────────
def test_cli_client_invokes_claude_p_and_strips_output():
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return "  回覆內容  \n"

    out = ClaudeCliClient(runner=fake_runner)("測試 prompt")
    assert out == "回覆內容"                              # 前後空白被 strip
    assert captured["args"][:2] == ["claude", "-p"]
    assert captured["args"][-1] == "測試 prompt"          # prompt 為最後一個參數


def test_cli_client_passes_model_flag_when_set():
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return "x"

    ClaudeCliClient(model="claude-opus-4-8", runner=fake_runner)("hi")
    assert "--model" in captured["args"]
    assert "claude-opus-4-8" in captured["args"]


def test_cli_client_missing_binary_raises_clear_error():
    def fake_runner(args):
        raise FileNotFoundError()

    with pytest.raises(RuntimeError, match="claude CLI"):
        ClaudeCliClient(runner=fake_runner)("hi")


def test_cli_client_nonzero_exit_raises_clear_error_with_stderr():
    def fake_runner(args):
        raise subprocess.CalledProcessError(returncode=1, cmd=["claude"],
                                            stderr="not logged in")

    with pytest.raises(RuntimeError, match="not logged in"):
        ClaudeCliClient(runner=fake_runner)("hi")


def test_cli_client_timeout_raises_clear_error():
    def fake_runner(args):
        raise subprocess.TimeoutExpired(cmd=["claude"], timeout=180)

    with pytest.raises(RuntimeError, match="逾時|timeout"):
        ClaudeCliClient(runner=fake_runner)("hi")


# ── API 版（Phase 2 排程用）──────────────────────────────
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


def test_api_client_passes_prompt_and_returns_text():
    fake = FakeAnthropic()
    out = AnthropicLLMClient(client=fake)("測試 prompt")
    assert out == "回覆內容"
    assert fake.messages.kwargs["messages"] == [{"role": "user", "content": "測試 prompt"}]
    assert fake.messages.kwargs["model"] == "claude-sonnet-5"
    assert fake.messages.kwargs["max_tokens"] == 2000


def test_api_client_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        AnthropicLLMClient()
