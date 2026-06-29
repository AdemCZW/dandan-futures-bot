"""方向感知通道護欄 DirectionalChannelGuard 的單元測試。

語意：連續 max_losses 筆「同方向」平倉虧損 → 暫停『該方向』新進場，
直到通道方向翻轉或冷卻 cooldown_bars 根 K 棒。只擋進場、不影響出場。
"""
from core.directional_guard import DirectionalChannelGuard


def _g(**kw):
    kw.setdefault("max_losses", 3)
    kw.setdefault("cooldown_bars", 8)
    return DirectionalChannelGuard(**kw)


class TestBlocking:
    def test_below_threshold_not_blocked(self):
        g = _g()
        g.record_trade(-1, -1.0)
        g.record_trade(-1, -2.0)            # 只 2 筆，門檻 3
        assert not g.blocks(-1)

    def test_three_same_dir_losses_block_that_dir_only(self):
        g = _g()
        for _ in range(3):
            g.record_trade(-1, -1.0)        # 連 3 筆做空虧
        assert g.blocks(-1)                 # 做空被擋
        assert not g.blocks(1)              # 做多不受影響

    def test_win_same_dir_resets_streak(self):
        g = _g()
        g.record_trade(-1, -1.0)
        g.record_trade(-1, -1.0)
        g.record_trade(-1, +5.0)            # 贏一筆 → 歸零
        g.record_trade(-1, -1.0)
        assert not g.blocks(-1)             # 只剩 1 筆連虧

    def test_alternating_dir_losses_do_not_accumulate(self):
        g = _g()
        g.record_trade(-1, -1.0)
        g.record_trade(1, -1.0)            # 換方向 → 重新計數
        g.record_trade(-1, -1.0)
        assert not g.blocks(-1)
        assert not g.blocks(1)


class TestUnblock:
    def test_win_after_block_unblocks(self):
        g = _g()
        for _ in range(3):
            g.record_trade(-1, -1.0)
        assert g.blocks(-1)
        g.record_trade(-1, +3.0)            # 被擋方向贏一筆 → 解封
        assert not g.blocks(-1)

    def test_channel_flip_unblocks(self):
        g = _g()
        g.on_bar(ch_dir=-1)                 # 封鎖當下通道方向 = 下降
        for _ in range(3):
            g.record_trade(-1, -1.0)
        assert g.blocks(-1)
        assert g.block_chdir == -1
        g.on_bar(ch_dir=1)                  # 通道翻成上升 → 解封
        assert not g.blocks(-1)

    def test_cooldown_expiry_unblocks(self):
        g = _g(cooldown_bars=4)
        g.on_bar(ch_dir=-1)
        for _ in range(3):
            g.record_trade(-1, -1.0)
        assert g.blocks(-1)
        for _ in range(4):                  # 通道方向不變，靠冷卻解封
            g.on_bar(ch_dir=-1)
        assert not g.blocks(-1)

    def test_cooldown_not_yet_expired_still_blocked(self):
        g = _g(cooldown_bars=4)
        g.on_bar(ch_dir=-1)
        for _ in range(3):
            g.record_trade(-1, -1.0)
        for _ in range(3):                  # 只過 3 根 < 4
            g.on_bar(ch_dir=-1)
        assert g.blocks(-1)


class TestDisabled:
    def test_disabled_never_blocks(self):
        g = _g(enabled=False)
        for _ in range(5):
            g.record_trade(-1, -5.0)
        assert not g.blocks(-1)             # 停用 → 永不擋


class TestPersistence:
    def test_to_from_dict_roundtrip_preserves_block(self):
        g = _g()
        g.on_bar(ch_dir=-1)
        for _ in range(3):
            g.record_trade(-1, -1.0)
        assert g.blocks(-1)
        d = g.to_dict()
        g2 = DirectionalChannelGuard.from_dict(d, max_losses=3, cooldown_bars=8)
        assert g2.blocks(-1)
        assert g2.block_chdir == -1
        assert g2.cooldown_left == g.cooldown_left

    def test_from_dict_empty_is_clean(self):
        g = DirectionalChannelGuard.from_dict({}, max_losses=3, cooldown_bars=8)
        assert not g.blocks(-1)
        assert not g.blocks(1)
