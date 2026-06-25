"""TDD：測試網重置偵測（testnet reset detection）。

幣安測試網每月清帳。detect_testnet_reset() 偵測帳戶餘額異常大幅下滑，
區分「正常回撤」與「交易所清帳」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bot_state import detect_testnet_reset


class TestDetectTestnetReset:

    def test_returns_false_when_no_last_balance(self):
        """last_balance=0 代表首次啟動，沒有基準，不觸發重置。"""
        assert detect_testnet_reset(current=5000.0, last=0.0) is False

    def test_returns_false_on_normal_drawdown(self):
        """正常虧損（跌 20%）不應被當成重置。"""
        assert detect_testnet_reset(current=4000.0, last=5000.0) is False

    def test_returns_true_on_90pct_drop(self):
        """餘額掉 90% 以上 → 測試網清帳。"""
        assert detect_testnet_reset(current=100.0, last=5000.0) is True

    def test_returns_true_when_balance_goes_to_zero(self):
        """餘額歸零（幣安測試網重置後余額可能為 0）。"""
        assert detect_testnet_reset(current=0.0, last=5000.0) is True

    def test_returns_false_when_last_balance_too_small(self):
        """last_balance < min_ref（預設 200 USDT）時不偵測，避免測試初期假警報。"""
        assert detect_testnet_reset(current=10.0, last=50.0) is False

    def test_custom_threshold(self):
        """drop_pct 可調：0.5 代表跌 50% 就觸發。"""
        assert detect_testnet_reset(current=2000.0, last=5000.0, drop_pct=0.50) is True
        assert detect_testnet_reset(current=3000.0, last=5000.0, drop_pct=0.50) is False

    def test_custom_min_ref(self):
        """min_ref 可調：小帳號可以用較低門檻。"""
        assert detect_testnet_reset(current=10.0, last=150.0, min_ref=100.0) is True

    def test_exact_threshold_boundary(self):
        """剛好在閾值上（跌 90.0%）→ True；少一分（跌 89.9%）→ False。"""
        assert detect_testnet_reset(current=500.0, last=5000.0, drop_pct=0.90) is True
        assert detect_testnet_reset(current=501.0, last=5000.0, drop_pct=0.90) is False
