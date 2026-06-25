"""Repo-level pytest conftest.

確保 repo 根目錄在 sys.path（pythonpath = . in pytest.ini 也會處理，這裡保險多加一層）。
vectorbt / optuna 未安裝時自動跳過依賴它們的測試檔，CI 不需要安裝重型套件。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

collect_ignore = []

try:
    import vectorbt  # noqa: F401
    import optuna    # noqa: F401
except ImportError:
    collect_ignore += [
        "tests/test_vbt_optimize.py",
    ]
