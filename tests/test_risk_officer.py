"""風控官 RiskOfficer 的 pytest 測試。

只測 core/risk_officer.py 的對外行為，不修改任何 source。
所有數值用確定性資料，方便明確 assert。
"""
import math
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


# ── kelly_fraction ─────────────────────────────────────────────────

from core.risk_officer import kelly_fraction


class TestKellyFraction:

    def test_returns_none_when_empty(self):
        assert kelly_fraction([]) is None

    def test_returns_none_when_too_few_trades(self):
        """min_trades 預設 20，少於此數回傳 None，不產生雜訊 Kelly。"""
        assert kelly_fraction([100.0] * 10) is None

    def test_all_losses_returns_zero(self):
        """Kelly 公式算出負值時夾在 0，不建議反押。"""
        pnl = [-50.0] * 30
        result = kelly_fraction(pnl, min_trades=30)
        assert result == 0.0

    def test_all_wins_returns_none_not_full_kelly(self):
        """OPT-15：全勝（無虧損樣本）無法估盈虧比 → 回 None（保守退回 budget），不再給滿格 1.0。"""
        pnl = [100.0] * 30
        assert kelly_fraction(pnl, min_trades=30) is None

    def test_default_min_trades_raised_to_30(self):
        """OPT-15：min_trades 預設由 20 提到 30（20 筆樣本噪音過大）。"""
        wins = [200.0] * 15
        losses = [-100.0] * 10        # 25 筆 < 30 → None
        assert kelly_fraction(wins + losses) is None
        # 補到 30 筆才算得出
        assert kelly_fraction(wins + losses + [200.0] * 3 + [-100.0] * 2) is not None

    def test_uses_wilson_lower_bound_on_win_rate(self):
        """OPT-15：勝率取 Wilson 信賴下界（z=1），小樣本不高估 → Kelly 比點估計保守。"""
        wins = [200.0] * 18
        losses = [-100.0] * 12        # n=30, p̂=0.6, b=2
        result = kelly_fraction(wins + losses, min_trades=30, z=1.0)
        # 獨立用 Wilson 公式算期望值
        n, p_hat, z, b = 30, 0.6, 1.0, 2.0
        denom = 1 + z * z / n
        centre = p_hat + z * z / (2 * n)
        margin = z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))
        p_lb = (centre - margin) / denom
        expected = max(0.0, (p_lb - (1 - p_lb) / b) / 2.0)   # half-Kelly
        assert result == pytest.approx(expected, abs=1e-9)
        point_est = (0.6 - 0.4 / b) / 2.0                    # = 0.20，點估計
        assert result < point_est                            # 信賴下界更保守

    def test_zero_when_lower_bound_edge_negative(self):
        """60% 勝率配 1:1，在較保守下界(z=2)下邊際轉負 → Kelly 夾 0（不靠噪音下注）。"""
        wins = [100.0] * 18
        losses = [-100.0] * 12        # n=30, b=1, p̂=0.6
        assert kelly_fraction(wins + losses, min_trades=30, z=2.0) == 0.0
        # z=1 時邊際仍微正，但遠小於點估計 half-Kelly 0.10
        assert kelly_fraction(wins + losses, min_trades=30, z=1.0) < 0.02

    def test_half_kelly_false_returns_full_kelly(self):
        """half_kelly=False 回傳完整 Kelly（仍用 Wilson 下界、夾 [0, max_kelly]）。"""
        wins = [200.0] * 18
        losses = [-100.0] * 12
        full = kelly_fraction(wins + losses, min_trades=30, half_kelly=False, z=1.0)
        half = kelly_fraction(wins + losses, min_trades=30, half_kelly=True, z=1.0)
        assert full == pytest.approx(half * 2.0, abs=1e-9)
        assert full < 0.40            # 點估計 full 為 0.40，下界更低

    def test_max_kelly_quarter_cap_for_leveraged(self):
        """OPT-15：可傳更緊的 max_kelly（如槓桿 bot 用 quarter-Kelly 0.25）夾住。"""
        wins = [500.0] * 27
        losses = [-50.0] * 3          # 高勝率高盈虧比 → 點估計很大
        capped = kelly_fraction(wins + losses, min_trades=30, max_kelly=0.25)
        assert capped <= 0.25 + 1e-9

    def test_position_size_uses_kelly_pct(self):
        """kelly_pct 傳入時，position_size 以 kelly_pct 取代 cfg.max_position_pct。"""
        cfg = Config()
        cfg.max_position_pct = 0.30        # 原本 30%
        officer = RiskOfficer(cfg)
        equity, price, stop = 10_000.0, 100.0, 98.0

        size_default = officer.position_size(equity, price, stop)
        size_kelly   = officer.position_size(equity, price, stop, kelly_pct=0.10)

        assert size_kelly < size_default   # 10% Kelly cap 比 30% 預設更保守
        # notional = equity * kelly_pct = 10_000 * 0.10 = 1000 → qty = 1000/100 = 10
        # (risk-based qty 可能更小；取 min)
        assert size_kelly <= 10.0 + 1e-9

    def test_check_entry_accepts_kelly_pct(self):
        """check_entry 接受 kelly_pct 參數並正確傳給 position_size。"""
        cfg = Config()
        cfg.max_position_pct = 0.30
        officer = RiskOfficer(cfg)
        equity, price = 10_000.0, 100.0

        dec_default = officer.check_entry(equity, price, "2026-01-01", kelly_pct=None)
        dec_kelly   = officer.check_entry(equity, price, "2026-01-01", kelly_pct=0.05)

        assert dec_kelly.allow is True
        assert dec_kelly.quantity < dec_default.quantity


class TestLiquidationGuard:
    """OPT-18：高槓桿下若 ATR 停損距離 ≥ 清算距離，停損會失效→先被強平。須擋下。"""

    def _officer(self, leverage):
        cfg = Config()
        cfg.futures_leverage = leverage
        cfg.atr_mult_sl = 2.0
        cfg.max_daily_loss_pct = 1.0       # 關單日熔斷免干擾
        cfg.max_peak_drawdown_pct = 0.0    # 關峰值熔斷
        return RiskOfficer(cfg)

    def test_blocks_entry_when_atr_stop_beyond_liquidation(self):
        """10x（清算距離≈9.5%），ATR 停損 2×ATR=10 → 10% > 9.5% → 拒單。"""
        officer = self._officer(10)
        dec = officer.check_entry(10_000.0, 100.0, "2026-01-01", direction=1, atr=5.0)
        assert dec.allow is False and "清算" in dec.reason

    def test_allows_entry_when_atr_stop_within_liquidation(self):
        """10x，正常 ATR：2×1=2 → 2% < 9.5% → 放行。"""
        officer = self._officer(10)
        dec = officer.check_entry(10_000.0, 100.0, "2026-01-01", direction=1, atr=1.0)
        assert dec.allow is True

    def test_guard_inactive_at_1x_leverage(self):
        """1x：清算距離≈99.5%，任何合理 ATR 停損都在內 → 不受守衛影響。"""
        officer = self._officer(1)
        dec = officer.check_entry(10_000.0, 100.0, "2026-01-01", direction=1, atr=5.0)
        assert dec.allow is True

    def test_guard_can_be_disabled(self):
        """liq_guard_enabled=False → 即使停損超過清算距離也放行（明確關閉）。"""
        cfg = Config()
        cfg.futures_leverage = 10
        cfg.atr_mult_sl = 2.0
        cfg.liq_guard_enabled = False
        cfg.max_daily_loss_pct = 1.0
        cfg.max_peak_drawdown_pct = 0.0
        officer = RiskOfficer(cfg)
        dec = officer.check_entry(10_000.0, 100.0, "2026-01-01", direction=1, atr=5.0)
        assert dec.allow is True


class TestUseFixedTP:
    """OPT-02：趨勢策略可關固定 TP（預設 True=現行），讓 Chandelier 接管趨勢尾段。"""

    def test_default_keeps_fixed_tp(self):
        cfg = Config()                       # use_fixed_tp 預設 True
        sl, tp = RiskOfficer(cfg).exit_levels(100.0, direction=1, atr=2.0)
        # 預設：tp = entry + tp_R_mult×atr_mult_sl×ATR = 100 + 2×2×2 = 108
        assert tp == pytest.approx(108.0)
        assert sl == pytest.approx(96.0)

    def test_use_fixed_tp_false_pushes_tp_far_long(self):
        cfg_on = Config(); cfg_off = Config(use_fixed_tp=False)
        _, tp_on = RiskOfficer(cfg_on).exit_levels(100.0, 1, atr=2.0)
        sl_off, tp_off = RiskOfficer(cfg_off).exit_levels(100.0, 1, atr=2.0)
        assert tp_off > tp_on                # TP 推遠 → 不在 2R 截斷
        assert sl_off == pytest.approx(96.0) # SL 不變（Chandelier 仍由 sl 端保護）

    def test_use_fixed_tp_false_pushes_tp_far_short(self):
        cfg_on = Config(); cfg_off = Config(use_fixed_tp=False)
        _, tp_on = RiskOfficer(cfg_on).exit_levels(100.0, -1, atr=2.0)
        sl_off, tp_off = RiskOfficer(cfg_off).exit_levels(100.0, -1, atr=2.0)
        assert tp_off < tp_on                # 空單 TP 在下方，推遠＝更低
        assert sl_off == pytest.approx(104.0)

    def test_use_fixed_tp_false_fixed_pct_path(self):
        """無 atr 的固定百分比路徑也要遵守 use_fixed_tp。"""
        cfg_off = Config(use_fixed_tp=False)
        _, tp_off = RiskOfficer(cfg_off).exit_levels(100.0, 1, atr=None)
        assert tp_off > 100.0 * (1 + Config().take_profit_pct)


# ── R1（2026-07-04 全系統體檢）：測試網重置後峰值回撤熔斷永久觸發 ──────────────
# 背景：testnet 每月重置一次餘額。RiskOfficer._equity_peak 停在重置前的高點，
# 重置後 equity 驟降 → peak_dd 立刻超過 max_peak_drawdown_pct 且永遠回不去
# （因為 peak 只會漲不會跌）。bot 因此永久拒絕所有新倉，且原因難以察覺
# （check_entry 的 reason 字串會顯示，但使用者只會看到「一直不交易」）。

def test_equity_peak_survives_testnet_reset_blocks_forever(officer, cfg):
    """重現 bug：重置後不重置 peak → 熔斷永久卡死（RED，證明問題存在）。"""
    cfg.max_peak_drawdown_pct = 0.20
    officer.check_entry(equity=5000.0, price=100.0, ts="2026-07-01", direction=1)  # 建立 peak=5000
    # testnet 重置：餘額驟降到 100（新一輪虛擬資金），但 peak 沒被重置
    d = officer.check_entry(equity=100.0, price=100.0, ts="2026-07-02", direction=1)
    assert d.allow is False   # 重置後的合理小額波動也會被舊 peak 誤判成 -98% 回撤


def test_reset_equity_peak_clears_after_testnet_reset(officer, cfg):
    """修復：呼叫 reset_equity_peak() 後，新的 equity 立刻成為新 peak，不再誤觸熔斷。"""
    cfg.max_peak_drawdown_pct = 0.20
    officer.check_entry(equity=5000.0, price=100.0, ts="2026-07-01", direction=1)
    officer.reset_equity_peak()
    d = officer.check_entry(equity=100.0, price=100.0, ts="2026-07-02", direction=1)
    assert d.allow is True   # peak 已跟著重置 → 100 是新高點，回撤 0%
    assert officer._equity_peak == 100.0
