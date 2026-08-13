"""Repo-level pytest conftest.

確保 repo 根目錄在 sys.path（pythonpath = . in pytest.ini 也會處理，這裡保險多加一層）。
vectorbt / optuna 未安裝時自動跳過依賴它們的測試檔，CI 不需要安裝重型套件。
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

collect_ignore = []

# 開發機 .env 裡的功能開關會被 config.py 的 load_dotenv() 讀進 os.environ，
# 汙染所有測試——實際踩過：.env 加了 AI_DESK_ZONE_GATE=true 之後，兩個跟閘門
# 無關的 desk 測試因為提案落在不利區而失敗。測試結果不該取決於誰的機器上開了
# 什麼旗標。這裡統一清掉，要測開啟行為的測試自己 monkeypatch.setenv 明確開。
_FEATURE_FLAGS = (
    "AI_DESK_ZONE_GATE",
    "AI_DESK_TARGET_GATE",
    "AI_DESK_AUTO_APPROVE",
    "AI_DESK_EXEC_ENABLED",
)


@pytest.fixture(autouse=True)
def _clear_feature_flags(monkeypatch):
    for name in _FEATURE_FLAGS:
        monkeypatch.delenv(name, raising=False)

try:
    import vectorbt  # noqa: F401
    import optuna    # noqa: F401
except ImportError:
    collect_ignore += [
        "tests/test_vbt_optimize.py",
    ]
