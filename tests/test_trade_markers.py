"""交易標記轉換 — service.build_trade_markers 純函式測試。

把交易日誌列轉成 K 線標記：
  - 每 6 小時聚合一個點（同 bot、同進/出場方向合併，帶筆數 count、均價）。
  - 依 symbol 過濾、side 分類、帶 strategy + mode（供前端標明回測）。
  - 回傳 bots 清單（含 mode 與總筆數），標記依時間遞增排序。
"""
from webapp.backend.service import build_trade_markers, _interval_seconds


def _row(ts, side, price, strategy="ema_cross", symbol="BTCUSDT", mode="paper"):
    return {"ts": ts, "side": side, "price": price, "strategy": strategy,
            "symbol": symbol, "mode": mode, "qty": 0.01, "pnl": 0.0}


class TestIntervalSeconds:
    def test_known_intervals(self):
        assert _interval_seconds("1m") == 60
        assert _interval_seconds("4h") == 14400
        assert _interval_seconds("1d") == 86400

    def test_unknown_defaults_to_hour(self):
        assert _interval_seconds("weird") == 3600


class TestBuildTradeMarkers:
    def test_empty_input(self):
        out = build_trade_markers([], "BTCUSDT")
        assert out["markers"] == []
        assert out["bots"] == []

    def test_filters_by_symbol(self):
        rows = [_row("2026-06-24 00:10:00", "entry", 100, symbol="BTCUSDT"),
                _row("2026-06-24 00:10:00", "entry", 200, symbol="ETHUSDT")]
        out = build_trade_markers(rows, "BTCUSDT")
        assert len(out["markers"]) == 1
        assert out["markers"][0]["price"] == 100

    def test_entry_long_classification(self):
        out = build_trade_markers([_row("2026-06-24 00:10:00", "entry", 100)], "BTCUSDT")
        m = out["markers"][0]
        assert m["side"] == "entry" and m["dir"] == 1

    def test_entry_short_classification(self):
        out = build_trade_markers([_row("2026-06-24 00:10:00", "entry_short", 100)], "BTCUSDT")
        m = out["markers"][0]
        assert m["side"] == "entry" and m["dir"] == -1

    def test_exit_classification(self):
        for side in ("exit_signal", "exit_sltp", "scale_out"):
            out = build_trade_markers([_row("2026-06-24 00:10:00", side, 100)], "BTCUSDT")
            assert out["markers"][0]["side"] == "exit", f"{side} 應分類為 exit"

    def test_six_hour_bucket_snapping(self):
        """ts 對齊到 6 小時桶（floor）。01:41 → 當日 00:00（0–6h 桶）。"""
        out = build_trade_markers([_row("2026-06-24 01:41:00", "entry", 100)], "BTCUSDT")
        t = out["markers"][0]["time"]
        assert t % 21600 == 0, "對齊後應為 6h 整數倍"
        from datetime import datetime, timezone
        raw = int(datetime(2026, 6, 24, 1, 41, tzinfo=timezone.utc).timestamp())
        assert t <= raw and raw - t < 21600

    def test_aggregates_same_bucket_same_side(self):
        """同一 6h 桶、同 bot、同進場方向的多筆 → 聚合成一個點，帶 count 與均價。"""
        rows = [_row("2026-06-24 00:10:00", "entry", 100),
                _row("2026-06-24 02:00:00", "entry", 200),
                _row("2026-06-24 05:00:00", "entry", 300)]
        out = build_trade_markers(rows, "BTCUSDT")
        assert len(out["markers"]) == 1, "三筆同桶同向應聚合為一點"
        m = out["markers"][0]
        assert m["count"] == 3
        assert m["price"] == 200.0, "均價 = (100+200+300)/3"

    def test_entry_and_exit_not_merged(self):
        """同桶但進場/出場不可合併（不同事件）。"""
        rows = [_row("2026-06-24 00:10:00", "entry", 100),
                _row("2026-06-24 00:20:00", "exit_signal", 110)]
        out = build_trade_markers(rows, "BTCUSDT")
        assert len(out["markers"]) == 2

    def test_long_and_short_not_merged(self):
        """同桶但多單/空單進場不可合併（方向不同）。"""
        rows = [_row("2026-06-24 00:10:00", "entry", 100),
                _row("2026-06-24 00:20:00", "entry_short", 110)]
        out = build_trade_markers(rows, "BTCUSDT")
        assert len(out["markers"]) == 2

    def test_different_buckets_not_merged(self):
        """不同 6h 桶不可合併。"""
        rows = [_row("2026-06-24 01:00:00", "entry", 100),
                _row("2026-06-24 07:00:00", "entry", 110)]
        out = build_trade_markers(rows, "BTCUSDT")
        assert len(out["markers"]) == 2

    def test_marker_carries_mode_and_strategy(self):
        out = build_trade_markers(
            [_row("2026-06-24 00:10:00", "entry", 100, strategy="zscore_ls", mode="backtest")],
            "BTCUSDT")
        m = out["markers"][0]
        assert m["strategy"] == "zscore_ls" and m["mode"] == "backtest"

    def test_bots_list_with_mode_and_count(self):
        rows = [_row("2026-06-24 00:10:00", "entry", 100, strategy="ema_cross", mode="paper"),
                _row("2026-06-24 06:10:00", "exit_sltp", 120, strategy="ema_cross", mode="paper"),
                _row("2026-06-24 00:10:00", "exit_signal", 90, strategy="zscore_ls", mode="backtest")]
        out = build_trade_markers(rows, "BTCUSDT")
        bots = {b["strategy"]: b for b in out["bots"]}
        assert bots["ema_cross"]["mode"] == "paper" and bots["ema_cross"]["count"] == 2
        assert bots["zscore_ls"]["mode"] == "backtest" and bots["zscore_ls"]["count"] == 1

    def test_markers_sorted_ascending_by_time(self):
        rows = [_row("2026-06-24 18:00:00", "exit_sltp", 120),
                _row("2026-06-24 00:00:00", "entry", 100),
                _row("2026-06-24 12:00:00", "entry", 110)]
        out = build_trade_markers(rows, "BTCUSDT")
        times = [m["time"] for m in out["markers"]]
        assert times == sorted(times)

    def test_iso_with_timezone_parsed(self):
        out = build_trade_markers([_row("2026-06-24T01:41:00+00:00", "entry", 100)], "BTCUSDT")
        assert len(out["markers"]) == 1
        assert out["markers"][0]["time"] % 21600 == 0

    def test_unparseable_ts_skipped(self):
        rows = [_row("not-a-date", "entry", 100),
                _row("2026-06-24 00:00:00", "entry", 110)]
        out = build_trade_markers(rows, "BTCUSDT")
        assert len(out["markers"]) == 1 and out["markers"][0]["price"] == 110

    def test_custom_bucket_hours(self):
        """bucket_hours 可調；1h 桶下 01:00 與 02:00 不同桶。"""
        rows = [_row("2026-06-24 01:00:00", "entry", 100),
                _row("2026-06-24 02:00:00", "entry", 110)]
        out = build_trade_markers(rows, "BTCUSDT", bucket_hours=1)
        assert len(out["markers"]) == 2
