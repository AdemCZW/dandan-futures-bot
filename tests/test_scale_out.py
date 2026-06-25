"""Scale-out（部分獲利了結）邏輯測試。

測試範圍：
  - RiskOfficer.check_scale_out() — 純函式邏輯
  - BotState 狀態持久化含 scaled_out / entry_sl_dist
"""
import os
import tempfile

import pytest

from core.risk_officer import RiskOfficer
from core.bot_state import BotState
from config import Config


# ─── helpers ───────────────────────────────────────────────────────────────

def _cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _risk(**kw):
    return RiskOfficer(_cfg(**kw))


# ─── RiskOfficer.check_scale_out ─────────────────────────────────────────

class TestCheckScaleOut:
    def test_returns_false_before_threshold_long(self):
        risk = _risk()
        # entry=100, sl=95 → sl_dist=5, 0.5R=2.5; price=102 (profit=2 < 2.5)
        assert risk.check_scale_out(
            entry_price=100, current_price=102, sl_dist=5,
            direction=1, already_scaled=False
        ) is False

    def test_returns_true_at_half_r_long(self):
        risk = _risk()
        # profit=2.5 == 0.5R → trigger
        assert risk.check_scale_out(
            entry_price=100, current_price=102.5, sl_dist=5,
            direction=1, already_scaled=False
        ) is True

    def test_returns_true_above_half_r_long(self):
        risk = _risk()
        # profit=4 > 0.5R=2.5
        assert risk.check_scale_out(
            entry_price=100, current_price=104, sl_dist=5,
            direction=1, already_scaled=False
        ) is True

    def test_returns_false_before_threshold_short(self):
        risk = _risk()
        # entry=100, sl=105 → sl_dist=5; price=98 (profit=2 < 2.5)
        assert risk.check_scale_out(
            entry_price=100, current_price=98, sl_dist=5,
            direction=-1, already_scaled=False
        ) is False

    def test_returns_true_at_half_r_short(self):
        risk = _risk()
        # short profit=2.5 == 0.5R
        assert risk.check_scale_out(
            entry_price=100, current_price=97.5, sl_dist=5,
            direction=-1, already_scaled=False
        ) is True

    def test_returns_false_if_already_scaled(self):
        risk = _risk()
        # 即使浮盈 > R，already_scaled=True 時不再觸發
        assert risk.check_scale_out(
            entry_price=100, current_price=110, sl_dist=5,
            direction=1, already_scaled=True
        ) is False

    def test_returns_false_when_flat(self):
        risk = _risk()
        assert risk.check_scale_out(
            entry_price=100, current_price=110, sl_dist=5,
            direction=0, already_scaled=False
        ) is False

    def test_custom_scale_r(self):
        risk = _risk()
        # scale_r=1.0 → 只有 profit >= 1R 才觸發
        assert risk.check_scale_out(
            entry_price=100, current_price=104, sl_dist=5,
            direction=1, already_scaled=False, scale_r=1.0
        ) is False
        assert risk.check_scale_out(
            entry_price=100, current_price=105, sl_dist=5,
            direction=1, already_scaled=False, scale_r=1.0
        ) is True

    def test_zero_sl_dist_returns_false(self):
        risk = _risk()
        # sl_dist=0 邏輯上無意義，不應觸發
        assert risk.check_scale_out(
            entry_price=100, current_price=110, sl_dist=0,
            direction=1, already_scaled=False
        ) is False

    def test_loss_position_returns_false(self):
        risk = _risk()
        # 虧損中不應觸發
        assert risk.check_scale_out(
            entry_price=100, current_price=98, sl_dist=5,
            direction=1, already_scaled=False
        ) is False


# ─── BotState 含 scaled_out / entry_sl_dist ──────────────────────────────

class TestBotStateScaleOut:
    def test_default_values(self):
        st = BotState()
        assert st.scaled_out is False
        assert st.entry_sl_dist == 0.0

    def test_save_and_load_scaled_out(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as fh:
            path = fh.name
        try:
            st = BotState(scaled_out=True, entry_sl_dist=250.0,
                          entry_price=50000.0, direction=1)
            st.save(path)
            loaded = BotState.load(path)
            assert loaded.scaled_out is True
            assert loaded.entry_sl_dist == 250.0
        finally:
            os.unlink(path)

    def test_load_missing_fields_gets_defaults(self):
        """舊版狀態檔（沒有 scaled_out / entry_sl_dist）讀取時應回傳預設值，不崩潰。"""
        import json
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                         mode="w") as fh:
            json.dump({"in_position": True, "entry_price": 100.0,
                       "direction": 1, "sl": 95.0, "tp": 110.0}, fh)
            path = fh.name
        try:
            st = BotState.load(path)
            assert st.scaled_out is False
            assert st.entry_sl_dist == 0.0
        finally:
            os.unlink(path)
