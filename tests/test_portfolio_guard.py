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


class TestSameStrategyDifferentSymbol:
    """兩台 bot 跑同一策略（fib_channel）但不同標的（BTC / SOL）。

    問題背景：identity 原本只用 strategy → 兩台共用同一列，互相覆蓋/刪除，
    且 check_exposure 把對方的倉位誤當「自己的舊倉」排除。
    修法：identity 改用 (strategy, symbol) 複合鍵。
    """

    @pytest.fixture
    def guard(self, tmp_path):
        return PortfolioGuard(db_path=str(tmp_path / "g.db"))

    def test_two_symbols_same_strategy_tracked_separately(self, guard):
        """同 strategy 不同 symbol → 各自獨立記錄，不互相覆蓋。"""
        guard.upsert_position("fib_channel", "BTCUSDT", 1, 0.01, 60000.0)
        guard.upsert_position("fib_channel", "SOLUSDT", -1, 10.0, 68.0)
        positions = guard.get_positions()
        assert len(positions) == 2, "同策略不同標的應各自獨立，不該互相覆蓋"
        syms = {p["symbol"] for p in positions}
        assert syms == {"BTCUSDT", "SOLUSDT"}

    def test_clear_one_symbol_keeps_other(self, guard):
        """clear_position 帶 symbol → 只刪該標的，不影響同策略另一標的。"""
        guard.upsert_position("fib_channel", "BTCUSDT", 1, 0.01, 60000.0)
        guard.upsert_position("fib_channel", "SOLUSDT", -1, 10.0, 68.0)
        guard.clear_position("fib_channel", "SOLUSDT")
        positions = guard.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTCUSDT", "只該刪掉 SOLUSDT"

    def test_exposure_counts_same_strategy_other_symbol_as_other_bot(self, guard):
        """同策略、不同 symbol 的倉位，對 check_exposure 而言是「別台 bot」，須計入暴露。"""
        # Bot2 = fib_channel/SOL 已做多 4000
        guard.upsert_position("fib_channel", "SOLUSDT", 1, 58.8, 68.0)  # ~4000
        # Bot1 = fib_channel/BTC 想做多 2000，同向合計 6000 > 5000 → 拒絕
        ok, reason = guard.check_exposure("fib_channel", direction=1,
                                          new_notional=2000.0, max_notional=5000.0,
                                          own_symbol="BTCUSDT")
        assert ok is False, "對方同策略不同標的的倉位應計入同向暴露"

    def test_exposure_excludes_own_symbol(self, guard):
        """check_exposure 排除自己（strategy+symbol 都相同）的舊倉。"""
        # Bot1 自己 fib_channel/BTC 的殘留舊倉 4000
        guard.upsert_position("fib_channel", "BTCUSDT", 1, 0.0667, 60000.0)  # ~4000
        # Bot1 新倉 2000；排除自己舊倉 → 其他為 0 → 放行
        ok, _ = guard.check_exposure("fib_channel", direction=1,
                                     new_notional=2000.0, max_notional=5000.0,
                                     own_symbol="BTCUSDT")
        assert ok is True, "自己（strategy+symbol 相同）的舊倉不該計入"


class TestConcentrationSubCap:
    """OPT-13：per-symbol / 相關性桶同向子上限，讓三台 SOL 集中度真正被擋。"""

    def test_per_symbol_cap_blocks_third_same_symbol_long(self, guard):
        guard.upsert_position("bot_a", "SOLUSDT", 1, 40.0, 100.0)   # 4000
        guard.upsert_position("bot_b", "SOLUSDT", 1, 40.0, 100.0)   # 4000
        ok, reason = guard.check_exposure(
            "bot_c", direction=1, new_notional=4000.0, max_notional=10 ** 9,
            own_symbol="SOLUSDT", symbol_cap=10000.0)               # 8000+4000>10000
        assert ok is False and "SOL" in reason.upper()

    def test_per_symbol_cap_allows_opposite_direction(self, guard):
        guard.upsert_position("bot_a", "SOLUSDT", 1, 40.0, 100.0)
        guard.upsert_position("bot_b", "SOLUSDT", 1, 40.0, 100.0)
        ok, _ = guard.check_exposure(
            "bot_c", direction=-1, new_notional=4000.0, max_notional=10 ** 9,
            own_symbol="SOLUSDT", symbol_cap=10000.0)               # 空單不與多單疊加
        assert ok is True

    def test_corr_bucket_aggregates_sol_and_eth(self, guard):
        guard.upsert_position("bot_a", "SOLUSDT", 1, 60.0, 100.0)   # SOL 多 6000
        ok, reason = guard.check_exposure(
            "bot_eth", direction=1, new_notional=5000.0, max_notional=10 ** 9,
            own_symbol="ETHUSDT", symbol_cap=10000.0,
            corr_symbols=["SOLUSDT", "ETHUSDT"])                    # 6000+5000>10000
        assert ok is False

    def test_corr_bucket_does_not_aggregate_unrelated_symbol(self, guard):
        guard.upsert_position("bot_a", "BTCUSDT", 1, 0.1, 60000.0)  # BTC 多 6000（不在桶內）
        ok, _ = guard.check_exposure(
            "bot_eth", direction=1, new_notional=5000.0, max_notional=10 ** 9,
            own_symbol="ETHUSDT", symbol_cap=10000.0,
            corr_symbols=["SOLUSDT", "ETHUSDT"])                    # BTC 不計入 → 5000<10000
        assert ok is True

    def test_symbol_cap_none_is_backward_compatible(self, guard):
        guard.upsert_position("bot_a", "SOLUSDT", 1, 40.0, 100.0)
        ok, _ = guard.check_exposure(
            "bot_c", direction=1, new_notional=4000.0, max_notional=10 ** 9,
            own_symbol="SOLUSDT")                                   # 不傳 cap → 只看全組合
        assert ok is True

    def test_global_cap_still_enforced_alongside_symbol_cap(self, guard):
        """全組合 max_notional 與 per-symbol cap 同時生效，任一超過即擋。"""
        guard.upsert_position("bot_a", "SOLUSDT", 1, 40.0, 100.0)   # 4000
        ok, _ = guard.check_exposure(
            "bot_c", direction=1, new_notional=2000.0, max_notional=5000.0,
            own_symbol="SOLUSDT", symbol_cap=10000.0)              # symbol ok 但全組合 4000+2000>5000
        assert ok is False


class TestPortfolioKillSwitch:
    """OPT-05：組合層淨值/回撤 kill-switch，補上唯一缺的組合級熔斷。"""

    def test_upsert_equity_tracks_peak(self, guard):
        guard.upsert_equity("bot_a", "SOLUSDT", 5000.0)
        guard.upsert_equity("bot_a", "SOLUSDT", 4000.0)   # 跌 → peak 仍 5000
        eq, peak, dd = guard.portfolio_drawdown()
        assert eq == pytest.approx(4000.0)
        assert peak == pytest.approx(5000.0)
        assert dd == pytest.approx(-0.20)

    def test_portfolio_drawdown_aggregates_across_bots(self, guard):
        guard.upsert_equity("bot_a", "SOLUSDT", 5000.0)   # peak 5000
        guard.upsert_equity("bot_a", "SOLUSDT", 4000.0)   # 現值 4000
        guard.upsert_equity("bot_b", "ETHUSDT", 5000.0)   # peak 5000
        guard.upsert_equity("bot_b", "ETHUSDT", 4500.0)   # 現值 4500
        eq, peak, dd = guard.portfolio_drawdown()
        assert eq == pytest.approx(8500.0)
        assert peak == pytest.approx(10000.0)
        assert dd == pytest.approx(-0.15)

    def test_check_blocks_when_drawdown_exceeds(self, guard):
        guard.upsert_equity("bot_a", "SOLUSDT", 5000.0)
        guard.upsert_equity("bot_a", "SOLUSDT", 4200.0)   # dd -0.16
        ok, reason = guard.check_portfolio_drawdown(max_dd=0.15)
        assert ok is False and "回撤" in reason

    def test_check_allows_when_drawdown_within_limit(self, guard):
        guard.upsert_equity("bot_a", "SOLUSDT", 5000.0)
        guard.upsert_equity("bot_a", "SOLUSDT", 4800.0)   # dd -0.04
        ok, _ = guard.check_portfolio_drawdown(max_dd=0.15)
        assert ok is True

    def test_check_allows_when_no_equity_data(self, guard):
        """無資料（剛啟動/DB 空）→ 放行（fail-open，不阻斷交易）。"""
        ok, _ = guard.check_portfolio_drawdown(max_dd=0.15)
        assert ok is True

    def test_check_disabled_when_max_dd_zero(self, guard):
        """max_dd=0 → 停用組合熔斷（一律放行）。"""
        guard.upsert_equity("bot_a", "SOLUSDT", 5000.0)
        guard.upsert_equity("bot_a", "SOLUSDT", 1000.0)   # dd -0.80
        ok, _ = guard.check_portfolio_drawdown(max_dd=0.0)
        assert ok is True


class TestLegacySchemaMigration:
    """舊版單一主鍵 (strategy) 的表，初始化時應遷移成 (strategy, symbol) 並保留資料。"""

    def test_migrates_old_single_pk_table_preserving_rows(self, tmp_path):
        import sqlite3
        db_path = str(tmp_path / "legacy.db")
        # 手動建立「舊結構」：strategy 為唯一主鍵
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE open_positions ("
            "strategy TEXT PRIMARY KEY, symbol TEXT, direction INTEGER, "
            "qty REAL, price REAL, notional REAL, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO open_positions VALUES "
            "('fib_channel','SOLUSDT',-1,10.0,68.0,680.0,'2026-06-26T00:00:00')"
        )
        conn.commit(); conn.close()

        # 初始化 guard → 觸發遷移
        guard = PortfolioGuard(db_path=db_path)

        # 舊資料保留
        positions = guard.get_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "SOLUSDT"

        # 遷移後可容納同策略不同標的（舊結構會因 PK 衝突失敗）
        guard.upsert_position("fib_channel", "BTCUSDT", 1, 0.01, 60000.0)
        assert len(guard.get_positions()) == 2


# ── F6（2026-07-04）：clear_equity — testnet 重置時清掉組合層淨值/峰值殘留 ──
def test_clear_equity_wipes_all_rows(tmp_path):
    g = PortfolioGuard(db_path=str(tmp_path / "g.db"))
    g.upsert_equity("s1", "BTCUSDT", 5000.0)
    g.upsert_equity("s2", "ETHUSDT", 4000.0)
    g.clear_equity()
    assert g.portfolio_drawdown() == (0.0, 0.0, 0.0)


def test_clear_equity_on_empty_table_is_noop(tmp_path):
    g = PortfolioGuard(db_path=str(tmp_path / "g.db"))
    g.clear_equity()                                          # 不拋例外
    assert g.portfolio_drawdown() == (0.0, 0.0, 0.0)
