"""LLM 呼叫器 — 兩種後端，同一個 __call__(prompt) -> str 介面。

ClaudeCliClient（Phase 1 預設）：叫本機已登入的 claude CLI（claude -p），走 Max
  訂閱額度，不額外計費；但只能在有登入的本機跑、且吃訂閱用量上限，僅適合低頻手動試跑。
AnthropicLLMClient（Phase 2 排程）：直接打 Anthropic API，按 token 計費，
  可在 Railway 等無登入伺服器跑。

兩者都可注入假物件（runner / client）完全離線測試。
"""
from __future__ import annotations

import os
import subprocess


DEFAULT_CLI_MODEL = "claude-opus-4-8"
"""釘死辯論用的模型。

不可改成「跟著 CLI 當下的 /model 走」——那會讓不同時期的提案偷偷用到不同模型，
前瞻樣本混在一起就無法做有效的統計驗證（而且不會有任何警告）。
要換模型是一個明確的決策，換了之後前後樣本必須分開統計。
"""


DEFAULT_CLI_TIMEOUT = 600
"""claude CLI 單次呼叫的預設逾時（秒）。

實測（2026-07-23，Opus 4.8）：四角色中最短的技術分析角色就要約 55 秒；裁判角色
的 prompt 要吃下前三個角色的完整論述，長度與推理複雜度都高出數倍。原本 180 秒
太緊，會在正常使用中誤殺尚在推理的呼叫。可用 AI_DESK_CLI_TIMEOUT 覆寫。
"""


def _env_binary() -> str:
    """從 AI_DESK_CLAUDE_BIN 讀 claude 執行檔路徑；未設 → 裸名稱 "claude"。

    為什麼需要：launchd（排程）執行時的 PATH 極簡（約 /usr/bin:/bin:/usr/sbin:/sbin），
    不會載入 shell profile，因此找不到裝在 /opt/homebrew/bin 的 claude。
    實際踩過：排程連續多輪 exit 1，log 顯示 No such file or directory: 'claude'。
    """
    return os.getenv("AI_DESK_CLAUDE_BIN", "").strip() or "claude"


def _env_timeout() -> int:
    """從 AI_DESK_CLI_TIMEOUT 讀逾時秒數；未設或無法解析 → 預設值。"""
    raw = os.getenv("AI_DESK_CLI_TIMEOUT", "").strip()
    try:
        return int(raw) if raw else DEFAULT_CLI_TIMEOUT
    except ValueError:
        return DEFAULT_CLI_TIMEOUT


class ClaudeCliClient:
    """透過本機 claude CLI 呼叫（訂閱額度，非 API 計費）。

    prompt 以最後一個 positional 參數傳入（用 subprocess list，無 shell 逸出問題）；
    輸出取 stdout 並 strip。runner 可注入以利離線測試。
    model 預設釘死 DEFAULT_CLI_MODEL；傳 None 才會退回 CLI 自己的預設（不建議）。
    """

    def __init__(self, binary: str | None = None, model: str | None = DEFAULT_CLI_MODEL,
                 timeout: int | None = None, runner=None):
        self.binary = binary if binary is not None else _env_binary()
        self.model = model
        self.timeout = timeout if timeout is not None else _env_timeout()
        self._runner = runner or self._default_runner

    def _default_runner(self, args) -> str:
        import subprocess
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=self.timeout, check=True)
        return result.stdout

    def __call__(self, prompt: str) -> str:
        args = [self.binary, "-p"]
        if self.model:
            args += ["--model", self.model]
        args.append(prompt)
        try:
            out = self._runner(args)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"找不到 {self.binary} CLI。ClaudeCliClient 需要本機已安裝並登入的 "
                "claude CLI（走 Max 訂閱）。排程（launchd/cron）環境的 PATH 極簡、"
                "找不到 /opt/homebrew/bin 下的執行檔時，請設環境變數 "
                "AI_DESK_CLAUDE_BIN 指向絕對路徑。"
                "若要在無登入的伺服器跑，請改用 AnthropicLLMClient。"
            ) from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise RuntimeError(
                f"{self.binary} CLI 執行失敗（exit code {e.returncode}）。"
                f"請確認本機已登入 claude CLI。"
                + (f" stderr: {stderr}" if stderr else "")
            ) from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"{self.binary} CLI 呼叫逾時（超過 {self.timeout} 秒）。"
                "Opus 跑長篇辯論本來就慢（實測單一角色可達數分鐘），"
                "可設環境變數 AI_DESK_CLI_TIMEOUT 加大（單位：秒）。"
            ) from e
        return out.strip()


class AnthropicLLMClient:
    """直接呼叫 Anthropic API（按用量計費，與 Max 訂閱分開的帳）。需 env: ANTHROPIC_API_KEY。"""

    def __init__(self, model: str = "claude-sonnet-5", max_tokens: int = 2000,
                 client=None):
        if client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise RuntimeError(
                    "缺少 ANTHROPIC_API_KEY 環境變數。AnthropicLLMClient 直接呼叫 "
                    "Anthropic API（按用量計費，與 Claude Max 訂閱分開）。"
                    "Phase 1 本機試跑建議改用 ClaudeCliClient（走訂閱、免額外計費）。"
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
