"""TDD：跨 bot 投資組合風控（cross-bot correlation control）。

三台 bot 共用 SQLite/PostgreSQL；開倉前讀取其他 bot 的持倉，
若同向暴露超過 max_notional 上限則阻止新倉。
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.portfolio_guard import PortfolioGuard


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_portfolio.db")


@pytest.fixture
def guard(db_path):
    return PortfolioGuard(db_path=db_path)


class TestPortfolioGuard:

    def test_empty_db_allows_entry(self, guard):
        """沒有任何 bot 持倉時，任何方向都放行。"""
        ok, reason = guard.check_exposure("bot_a", direction=1, new_notional=1000.0,
                                          max_notional=5000.0)
        assert ok is True

    def test_upsert_creates_record(self, guard):
        """upsert_position 後，get_positions 能查到該 bot 的持倉。"""
        guard.upsert_position("bot_a", symbol="BTCUSDT", direction=1,
                              qty=0.01, price=60000.0)
        positions = guard.get_positions()
        assert len(positions) == 1
        assert positions[0]["strategy"] == "bot_a"
        assert positions[0]["direction"] == 1
        assert abs(positions[0]["notional"] - 600.0) < 0.01

    def test_upsert_overwrites_same_strategy(self, guard):
        """同一 strategy 再次 upsert → 更新，不新增。"""
        guard.upsert_position("bot_a", "BTCUSDT", 1, 0.01, 60000.0)
        guard.upsert_position("bot_a", "BTCUSDT", 1, 0.02, 60000.0)
        assert len(guard.get_positions()) == 1
        assert abs(guard.get_positions()[0]["notional"] - 1200.0) < 0.01

    def test_clear_removes_record(self, guard):
        """clear_position 後，該 bot 的持倉消失。"""
        guard.upsert_position("bot_a", "BTCUSDT", 1, 0.01, 60000.0)
        guard.clear_position("bot_a")
        assert len(guard.get_positions()) == 0

    def test_blocks_when_same_direction_exceeds_limit(self, guard):
        """兩台 bot 已做多 4000 USDT，新倉 2000 USDT 會超過 5000 上限。"""
        guard.upsert_position("bot_b", "BTCUSDT", 1, 0.033, 60000.0)  # ~2000
        guard.upsert_position("bot_c", "ETHUSDT", 1, 1.0,   2000.0)   # 2000
        # bot_a 想再做多 2000，同向合計 6000 > 5000 → 拒絕
        ok, reason = guard.check_exposure("bot_a", direction=1, new_notional=2000.0,
                                          max_notional=5000.0)
        assert ok is False
        assert "暴露" in reason or "notional" in reason.lower() or "超過" in reason

    def test_allows_opposite_direction(self, guard):
        """其他 bot 都在做多，新 bot 想做空 → 方向不同，不計入同向暴露。"""
        guard.upsert_position("bot_b", "BTCUSDT", 1, 0.1, 60000.0)    # 6000 多
        guard.upsert_position("bot_c", "ETHUSDT", 1, 1.0, 3000.0)     # 3000 多
        # bot_a 做空 → 不計多單暴露
        ok, _ = guard.check_exposure("bot_a", direction=-1, new_notional=2000.0,
                                     max_notional=5000.0)
        assert ok is True

    def test_own_strategy_excluded_from_check(self, guard):
        """自己上一筆持倉（平倉前殘留）不計入同向暴露。"""
        guard.upsert_position("bot_a", "BTCUSDT", 1, 0.05, 60000.0)   # 3000 自己的舊倉
        guard.upsert_position("bot_b", "SOLUSDT", 1, 5.0,  200.0)     # 1000 其他
        # bot_a 新倉 2000；排除自己的舊倉 → 其他合計 1000 < 5000 → 放行
        ok, _ = guard.check_exposure("bot_a", direction=1, new_notional=2000.0,
                                     max_notional=5000.0)
        assert ok is True

    def test_multiple_bots_multiple_directions(self, guard):
        """混合方向：只加總相同方向的暴露。"""
        guard.upsert_position("bot_b", "BTCUSDT",  1, 0.05, 60000.0)  # 3000 多
        guard.upsert_position("bot_c", "ETHUSDT", -1, 1.0,  3000.0)   # 3000 空
        # bot_a 做多 2000：同向多單合計 3000+2000=5000，剛好等於 5000 → 放行（≤ 不是 <）
        ok, _ = guard.check_exposure("bot_a", direction=1, new_notional=2000.0,
                                     max_notional=5000.0)
        assert ok is True
        # 若新倉 2001 → 超過 5000 → 拒絕
        ok2, _ = guard.check_exposure("bot_a", direction=1, new_notional=2001.0,
                                      max_notional=5000.0)
        assert ok2 is False
