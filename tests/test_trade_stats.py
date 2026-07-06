"""交易統計 — service.trade_stats 純函式測試。

把全量成交列（newest-first，與 read_trades DESC 同序）配對成回合後，算：
  - max_drawdown_pct：以 init_capital 為基底的已實現權益曲線最大回撤 %
  - sharpe：每筆交易 ROI（pnl / 名目）的 mean/std（每筆夏普，非年化）
  - 多/空各自的筆數、勝筆、損益（配對 entry→exit 推方向）

輸入順序與 all_hist 一致（newest-first）；feed() 把時間正序的場景轉成 newest-first。
"""
from webapp.backend.service import trade_stats


def _row(side, pnl, price=100.0, qty=1.0):
    return {"side": side, "pnl": pnl, "price": price, "qty": qty, "ts": "2026-06-01 00:00:00"}


# 場景以時間正序撰寫；trade_stats 期望 newest-first，故 feed 反轉
def feed(chrono):
    return list(reversed(chrono))


class TestEmptyAndDegenerate:
    def test_empty(self):
        s = trade_stats([])
        assert s["max_drawdown_pct"] is None
        assert s["sharpe"] is None
        assert s["long_trades"] == 0 and s["short_trades"] == 0
        assert s["long_wins"] == 0 and s["short_wins"] == 0
        assert s["long_pnl"] == 0.0 and s["short_pnl"] == 0.0

    def test_only_entries_no_close(self):
        # 只有進場、無平倉 → 無已實現統計
        s = trade_stats(feed([_row("entry", 0.0)]))
        assert s["long_trades"] == 0
        assert s["max_drawdown_pct"] is None


class TestLongShortSplit:
    def test_two_long_wins(self):
        chrono = [
            _row("entry", 0.0),
            _row("exit_tp", 10.0),
            _row("entry", 0.0),
            _row("exit_tp", 20.0),
        ]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 2
        assert s["long_wins"] == 2
        assert s["long_pnl"] == 30.0
        assert s["short_trades"] == 0

    def test_short_position_attribution(self):
        # entry_short 開空，exit 平倉的損益歸到「空」
        chrono = [
            _row("entry_short", 0.0),
            _row("exit_sl", -5.0),
            _row("entry_short", 0.0),
            _row("exit_tp", 8.0),
        ]
        s = trade_stats(feed(chrono))
        assert s["short_trades"] == 2
        assert s["short_wins"] == 1
        assert s["short_pnl"] == 3.0
        assert s["long_trades"] == 0

    def test_scale_out_counts_with_open_direction(self):
        # scale_out 部分了結（pnl!=0）也算一筆，方向跟著當下開倉
        chrono = [
            _row("entry", 0.0),
            _row("scale_out", 5.0),
            _row("exit_trail", 7.0),
        ]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 2          # scale_out + exit 各算一筆平倉事件
        assert s["long_pnl"] == 12.0

    def test_mixed_long_and_short(self):
        chrono = [
            _row("entry", 0.0),
            _row("exit_tp", 10.0),            # long win
            _row("entry_short", 0.0),
            _row("exit_sl", -4.0),            # short loss
        ]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 1 and s["long_wins"] == 1 and s["long_pnl"] == 10.0
        assert s["short_trades"] == 1 and s["short_wins"] == 0 and s["short_pnl"] == -4.0

    def test_breakeven_excluded(self):
        # pnl == 0 的平倉（保本）不計入筆數（與既有 realized 口徑一致）
        chrono = [_row("entry", 0.0), _row("exit_breakeven", 0.0)]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 0

    def test_orphan_exit_excluded_from_split(self):
        # 視窗起點就是平倉（entry 在視窗外）→ 無法判方向，不計入多空，但仍進回撤曲線
        chrono = [_row("exit_sl", -3.0), _row("entry", 0.0), _row("exit_tp", 5.0)]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 1          # 只有那筆有 entry 的算 long
        assert s["short_trades"] == 0
        assert s["max_drawdown_pct"] is not None   # 孤兒 -3 仍進回撤計算


class TestReconciledExcluded:
    def test_reconciled_exit_not_counted_in_split(self):
        # exit_reconciled（接管孤兒 backfill）估計 pnl 不計入乾淨勝率/多空拆分
        chrono = [
            _row("entry", 0.0), _row("exit_tp", 10.0),       # 乾淨 long win
            _row("entry", 0.0), _row("exit_reconciled", -8.0),  # 接管 → 不計
        ]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 1 and s["long_wins"] == 1 and s["long_pnl"] == 10.0

    def test_reconciled_resets_position_for_next_trade(self):
        # 接管收倉後，後續 entry_short→exit_sl 要正確歸為 short
        chrono = [
            _row("entry", 0.0), _row("exit_reconciled", -8.0),
            _row("entry_short", 0.0), _row("exit_sl", -5.0),
        ]
        s = trade_stats(feed(chrono))
        assert s["long_trades"] == 0
        assert s["short_trades"] == 1 and s["short_pnl"] == -5.0


class TestDrawdown:
    def test_monotonic_up_zero_drawdown(self):
        chrono = [_row("entry", 0.0), _row("exit_tp", 10.0),
                  _row("entry", 0.0), _row("exit_tp", 10.0)]
        s = trade_stats(feed(chrono))
        assert s["max_drawdown_pct"] == 0.0

    def test_drawdown_after_peak(self):
        # 起始 1000，+100 到 1100（峰），-220 到 880 → 回撤 (1100-880)/1100 = 20%
        chrono = [_row("entry", 0.0), _row("exit_tp", 100.0),
                  _row("entry", 0.0), _row("exit_sl", -220.0)]
        s = trade_stats(feed(chrono), init_capital=1000.0)
        assert s["max_drawdown_pct"] == 20.0


class TestSharpe:
    def test_equal_returns_zero_std_none(self):
        # 兩筆 ROI 相同 → std=0 → sharpe None
        chrono = [_row("entry", 0.0), _row("exit_tp", 10.0, price=100, qty=1),
                  _row("entry", 0.0), _row("exit_tp", 10.0, price=100, qty=1)]
        s = trade_stats(feed(chrono))
        assert s["sharpe"] is None

    def test_positive_sharpe_when_mostly_winning(self):
        chrono = [_row("entry", 0.0), _row("exit_tp", 10.0, price=100, qty=1),
                  _row("entry", 0.0), _row("exit_tp", 12.0, price=100, qty=1),
                  _row("entry", 0.0), _row("exit_sl", -2.0, price=100, qty=1)]
        s = trade_stats(feed(chrono))
        assert s["sharpe"] is not None
        assert s["sharpe"] > 0

    def test_single_trade_sharpe_none(self):
        chrono = [_row("entry", 0.0), _row("exit_tp", 10.0)]
        s = trade_stats(feed(chrono))
        assert s["sharpe"] is None        # 不足兩筆無法算標準差
