"""風控官 RiskOfficer 的 pytest 測試。

只測 core/risk_officer.py 的對外行為，不修改任何 source。
所有數值用確定性資料，方便明確 assert。
"""
import os
import sys

import pytest

# 確保可從專案根目錄匯入 config / core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from core.risk_officer import RiskOfficer


@pytest.fixture
def cfg():
    """用預設 Config（risk_per_trade=0.01, max_position_pct=0.30,
    stop_loss_pct=0.02, take_profit_pct=0.04, max_daily_loss_pct=0.05）。"""
    return Config()


@pytest.fixture
def officer(cfg):
    return RiskOfficer(cfg)


# ── position_size：多空同距離 → qty 相等（abs 對稱）──────────────
def test_position_size_long_short_symmetric(officer):
    equity = 10_000.0
    entry = 100.0
    # 停損距離相同（10），多單在下方、空單在上方
    long_stop = 90.0   # entry - 10
    short_stop = 110.0  # entry + 10

    qty_long = officer.position_size(equity, entry, long_stop)
    qty_short = officer.position_size(equity, entry, short_stop)

    # 距離相同 → 數量必須完全一致（per_unit_loss 用 abs）
    assert qty_long == qty_short

    # 此參數下風險法綁定（非 cap）：risk=10000*0.01=100, per_unit=10 → 10
    # cap = 10000*0.30/100 = 30，不綁定，所以結果應為風險法的 10
    assert qty_long == pytest.approx(10.0)


# ── max_position_pct 上限會綁住（風險法 qty 較大時取 cap）──────────
def test_position_size_capped_by_max_position_pct(officer, cfg):
    equity = 10_000.0
    entry = 100.0
    # 停損距離很小（0.1）→ 風險法算出很大的 qty，應被 cap 綁住
    near_stop = 99.9

    qty = officer.position_size(equity, entry, near_stop)

    qty_by_risk = (equity * cfg.risk_per_trade) / abs(entry - near_stop)
    qty_by_cap = (equity * cfg.max_position_pct) / entry

    # 確認這個情境下風險法確實 > cap，cap 才有意義
    assert qty_by_risk > qty_by_cap
    # 結果必須被 cap 綁住
    assert qty == pytest.approx(qty_by_cap)
    assert qty == pytest.approx(30.0)


def test_position_size_never_negative(officer):
    # 即使 price 等於 stop（per_unit_loss 退回 1e-9），qty 仍 >= 0
    qty = officer.position_size(10_000.0, 100.0, 100.0)
    assert qty >= 0.0


# ── exit_levels：方向正確性 ───────────────────────────────────
def test_exit_levels_long_ordering(officer):
    entry = 100.0
    sl, tp = officer.exit_levels(entry, direction=1)
    # 做多：sl < entry < tp
    assert sl < entry < tp
    assert sl == pytest.approx(98.0)   # 100 * (1 - 0.02)
    assert tp == pytest.approx(104.0)  # 100 * (1 + 0.04)


def test_exit_levels_short_ordering(officer):
    entry = 100.0
    sl, tp = officer.exit_levels(entry, direction=-1)
    # 做空：tp < entry < sl
    assert tp < entry < sl
    assert sl == pytest.approx(102.0)  # 100 * (1 + 0.02)
    assert tp == pytest.approx(96.0)   # 100 * (1 - 0.04)


def test_exit_levels_default_direction_is_long(officer):
    entry = 100.0
    assert officer.exit_levels(entry) == officer.exit_levels(entry, direction=1)


# ── ATR 動態停損 / R 倍數停利（atr 入參）─────────────────────────
def test_exit_levels_atr_long(officer):
    """做多 + atr：sl = entry - mult*atr；tp = entry + R*(mult*atr)。"""
    # 預設 atr_mult_sl=2.0, tp_R_mult=2.0；atr=2 → 停損距離 4、停利距離 8
    sl, tp = officer.exit_levels(100.0, direction=1, atr=2.0)
    assert sl == pytest.approx(96.0)
    assert tp == pytest.approx(108.0)


def test_exit_levels_atr_short(officer):
    """做空 + atr：sl 在上方、tp 在下方，距離與多單對稱。"""
    sl, tp = officer.exit_levels(100.0, direction=-1, atr=2.0)
    assert sl == pytest.approx(104.0)
    assert tp == pytest.approx(92.0)


def test_exit_levels_atr_none_falls_back_to_pct(officer):
    """atr=None → 退回固定百分比（向後相容，與不傳 atr 一致）。"""
    assert officer.exit_levels(100.0, direction=1, atr=None) == pytest.approx((98.0, 104.0))
    assert officer.exit_levels(100.0, direction=1, atr=None) == officer.exit_levels(100.0, direction=1)


def test_check_entry_atr_sizes_inversely_to_volatility(officer):
    """波動度歸一化：atr 越大 → 停損距離越大 → 准許數量越小。"""
    d_small = officer.check_entry(10_000.0, 100.0, "2026-06-22T00:00:00", direction=1, atr=1.0)
    d_large = officer.check_entry(10_000.0, 100.0, "2026-06-22T00:01:00", direction=1, atr=5.0)
    assert d_large.quantity < d_small.quantity


def test_check_entry_atr_qty_matches_stop_distance(officer):
    """qty = risk_amount / (atr_mult_sl*atr)（未觸 cap 時）。

    equity=10000, risk_per_trade=0.01 → risk=100；atr=5, mult=2 → 停損距離 10 → qty=10。
    cap=10000*0.30/100=30 不綁定，故 qty=10。
    """
    d = officer.check_entry(10_000.0, 100.0, "2026-06-22T00:00:00", direction=1, atr=5.0)
    assert d.quantity == pytest.approx(10.0)


def test_check_entry_atr_none_backward_compatible(officer):
    """atr=None → 用 stop_loss_pct 算停損距離（與舊版相同）。

    停損距離=100*0.02=2 → 風險法 qty=100/2=50，被 cap=30 綁住 → 30（與舊版一致）。
    """
    d = officer.check_entry(10_000.0, 100.0, "2026-06-22T00:00:00", direction=1, atr=None)
    assert d.quantity == pytest.approx(30.0)


# ── Chandelier 追蹤停損：多單只升不降、空單只降不升 ─────────────────
def test_trailing_stop_long_ratchets_up_only(officer):
    """做多：stop = max(prev, highest - chand_mult*atr)，只升不降。"""
    # 預設 chand_mult=3.0；highest=110, atr=2 → 候選=110-6=104；prev=100 → 104
    assert officer.update_trailing_stop(100.0, 110.0, 2.0, 1) == pytest.approx(104.0)
    # 高點回落使候選低於 prev → 維持 prev（不下調）
    assert officer.update_trailing_stop(104.0, 108.0, 2.0, 1) == pytest.approx(104.0)


def test_trailing_stop_short_ratchets_down_only(officer):
    """做空：stop = min(prev, lowest + chand_mult*atr)，只降不升。"""
    assert officer.update_trailing_stop(100.0, 90.0, 2.0, -1) == pytest.approx(96.0)
    assert officer.update_trailing_stop(96.0, 92.0, 2.0, -1) == pytest.approx(96.0)


def test_trailing_stop_atr_missing_keeps_prev(officer):
    """atr 缺值（None / <=0）→ 不更新，回傳原停損。"""
    assert officer.update_trailing_stop(100.0, 110.0, None, 1) == 100.0
    assert officer.update_trailing_stop(100.0, 110.0, 0.0, 1) == 100.0


# ── mark_bar：當日首根登記基準、之後同日不覆蓋 ─────────────────
def test_mark_bar_registers_on_first_bar_of_day(officer):
    officer.mark_bar("2026-06-22T00:00:00", 10_000.0)
    assert officer._daily_key == "2026-06-22"
    assert officer._daily_start_equity == 10_000.0


def test_mark_bar_does_not_overwrite_same_day(officer):
    officer.mark_bar("2026-06-22T00:00:00", 10_000.0)
    # 同日後續 K 線（權益已變）不可覆蓋當日基準
    officer.mark_bar("2026-06-22T08:30:00", 9_000.0)
    officer.mark_bar("2026-06-22T23:55:00", 12_000.0)
    assert officer._daily_start_equity == 10_000.0


def test_mark_bar_resets_basis_on_new_day(officer):
    officer.mark_bar("2026-06-22T00:00:00", 10_000.0)
    officer.mark_bar("2026-06-22T12:00:00", 9_500.0)  # 同日不變
    assert officer._daily_start_equity == 10_000.0
    # 跨日後重設基準為新一天的首根權益
    officer.mark_bar("2026-06-23T00:00:00", 9_500.0)
    assert officer._daily_key == "2026-06-23"
    assert officer._daily_start_equity == 9_500.0


# ── 單日熔斷：mark_bar 設日開權益後，跌幅 > max_daily_loss_pct → 禁入 ─
def test_daily_circuit_breaker_blocks_entry_after_big_drawdown(officer, cfg):
    day = "2026-06-22"
    officer.mark_bar(f"{day}T00:00:00", 10_000.0)

    # 同日跌幅 6% > max_daily_loss_pct(5%) → 應禁止進場
    dropped_equity = 10_000.0 * (1 - 0.06)  # 9_400
    decision = officer.check_entry(dropped_equity, 100.0, f"{day}T12:00:00", direction=1)

    assert decision.allow is False
    assert decision.quantity == 0.0


def test_daily_circuit_breaker_allows_entry_within_limit(officer):
    day = "2026-06-22"
    officer.mark_bar(f"{day}T00:00:00", 10_000.0)

    # 同日跌幅僅 3% < 5% → 應准許進場
    dropped_equity = 10_000.0 * (1 - 0.03)  # 9_700
    decision = officer.check_entry(dropped_equity, 100.0, f"{day}T12:00:00", direction=1)

    assert decision.allow is True
    assert decision.quantity > 0.0


def test_daily_circuit_breaker_resets_across_days(officer):
    # 第一天觸發熔斷
    officer.mark_bar("2026-06-22T00:00:00", 10_000.0)
    blocked = officer.check_entry(9_300.0, 100.0, "2026-06-22T12:00:00", direction=1)
    assert blocked.allow is False

    # 跨日重設基準：新一天以 9_300 為基準，未再大跌 → 應可進場
    officer.mark_bar("2026-06-23T00:00:00", 9_300.0)
    decision = officer.check_entry(9_300.0, 100.0, "2026-06-23T00:05:00", direction=1)
    assert decision.allow is True
    assert decision.quantity > 0.0
