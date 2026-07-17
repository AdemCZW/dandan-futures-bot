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


class ClaudeCliClient:
    """透過本機 claude CLI 呼叫（訂閱額度，非 API 計費）。

    prompt 以最後一個 positional 參數傳入（用 subprocess list，無 shell 逸出問題）；
    輸出取 stdout 並 strip。runner 可注入以利離線測試。
    """

    def __init__(self, binary: str = "claude", model: str | None = None,
                 timeout: int = 180, runner=None):
        self.binary = binary
        self.model = model
        self.timeout = timeout
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
                "claude CLI（走 Max 訂閱）。若要在無登入的伺服器跑，請改用 AnthropicLLMClient。"
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
                "請確認網路狀況或稍後再試。"
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
