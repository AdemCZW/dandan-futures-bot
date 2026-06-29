"""手動平倉代理 — service.close_position 純函式測試（注入 opener，免真連網）。"""
from webapp.backend import service


class _FakeResp:
    def __init__(self, body): self._body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._body


def test_close_position_no_url_returns_error():
    out = service.close_position("", "tok")
    assert out["ok"] is False


def test_close_position_no_token_returns_error():
    out = service.close_position("http://bot", "")
    assert out["ok"] is False                      # 儀表板未設 CLOSE_TOKEN → 停用


def test_close_position_posts_with_token_header():
    captured = {}

    def fake_opener(req, timeout=10):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["token"] = req.headers.get("X-close-token")   # urllib 標題化 header key
        return _FakeResp(b'{"ok":true,"queued":true}')

    out = service.close_position("http://bot", "secret", opener=fake_opener)
    assert out == {"ok": True, "queued": True}
    assert captured["url"] == "http://bot/close"
    assert captured["method"] == "POST"
    assert captured["token"] == "secret"


def test_close_position_network_error_returns_error():
    def boom(req, timeout=10):
        raise OSError("connection refused")

    out = service.close_position("http://bot", "secret", opener=boom)
    assert out["ok"] is False
    assert "失敗" in out["msg"]
