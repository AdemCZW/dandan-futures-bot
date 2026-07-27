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


def test_cli_client_pins_opus_by_default():
    """預設釘死模型，不再跟著 CLI 當下的 /model 飄（否則前瞻樣本會混到不同模型）。"""
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return "x"

    ClaudeCliClient(runner=fake_runner)("hi")
    assert "--model" in captured["args"]
    assert "claude-opus-4-8" in captured["args"]


def test_cli_default_timeout_is_generous_for_opus_debate(monkeypatch):
    """實測技術分析角色(最短的一個)就要 ~55s，裁判角色 prompt 更長更慢；
    180s 太緊會在真實使用中誤殺。預設放寬到 600s。"""
    monkeypatch.delenv("AI_DESK_CLI_TIMEOUT", raising=False)
    assert ClaudeCliClient(runner=lambda a: "x").timeout == 600


def test_cli_timeout_configurable_by_env(monkeypatch):
    monkeypatch.setenv("AI_DESK_CLI_TIMEOUT", "900")
    assert ClaudeCliClient(runner=lambda a: "x").timeout == 900


def test_cli_explicit_timeout_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("AI_DESK_CLI_TIMEOUT", "900")
    assert ClaudeCliClient(timeout=120, runner=lambda a: "x").timeout == 120


def test_cli_timeout_error_message_mentions_how_to_raise_it():
    """逾時的錯誤訊息要告訴使用者怎麼調，而不是只說失敗。"""
    import subprocess

    def slow_runner(args):
        raise subprocess.TimeoutExpired(cmd=args, timeout=600)

    with pytest.raises(RuntimeError, match="AI_DESK_CLI_TIMEOUT"):
        ClaudeCliClient(runner=slow_runner)("hi")


# ── binary 路徑（launchd 極簡 PATH 找不到 claude 的真實故障）────
def test_cli_binary_configurable_by_env(monkeypatch):
    """launchd 的 PATH 不含 /opt/homebrew/bin，裸 `claude` 會 FileNotFoundError。
    實際踩過：排程連續多輪全部 exit 1，log 顯示 No such file or directory: 'claude'。"""
    monkeypatch.setenv("AI_DESK_CLAUDE_BIN", "/opt/homebrew/bin/claude")
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return "x"

    ClaudeCliClient(runner=fake_runner)("hi")
    assert captured["args"][0] == "/opt/homebrew/bin/claude"


def test_cli_binary_defaults_to_bare_name(monkeypatch):
    monkeypatch.delenv("AI_DESK_CLAUDE_BIN", raising=False)
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return "x"

    ClaudeCliClient(runner=fake_runner)("hi")
    assert captured["args"][0] == "claude"


def test_cli_explicit_binary_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("AI_DESK_CLAUDE_BIN", "/opt/homebrew/bin/claude")
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return "x"

    ClaudeCliClient(binary="/custom/claude", runner=fake_runner)("hi")
    assert captured["args"][0] == "/custom/claude"


def test_missing_binary_error_mentions_env_override():
    """找不到 binary 時要提示可用環境變數指定絕對路徑（排程場景的解法）。"""
    def missing(args):
        raise FileNotFoundError()

    with pytest.raises(RuntimeError, match="AI_DESK_CLAUDE_BIN"):
        ClaudeCliClient(runner=missing)("hi")
