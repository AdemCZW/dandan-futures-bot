"""交易標記轉換 — service.build_trade_markers 純函式測試。

把交易日誌列轉成 K 線標記：ts 對齊 interval 時間桶、side 分類、依 symbol 過濾、帶 strategy。
"""
from webapp.backend.service import build_trade_markers, _interval_seconds


def _row(ts, side, price, strategy="ema_cross", symbol="BTCUSDT"):
    return {"ts": ts, "side": side, "price": price,
            "strategy": strategy, "symbol": symbol, "qty": 0.01, "pnl": 0.0}


class TestIntervalSeconds:
    def test_known_intervals(self):
        assert _interval_seconds("1m") == 60
        assert _interval_seconds("5m") == 300
        assert _interval_seconds("15m") == 900
        assert _interval_seconds("1h") == 3600
        assert _interval_seconds("4h") == 14400
        assert _interval_seconds("1d") == 86400

    def test_unknown_defaults_to_hour(self):
        assert _interval_seconds("weird") == 3600


class TestBuildTradeMarkers:
    def test_empty_input(self):
        out = build_trade_markers([], "BTCUSDT", "4h")
        assert out["markers"] == []
        assert out["strategies"] == []

    def test_filters_by_symbol(self):
        rows = [_row("2026-06-24 00:10:00", "entry", 100, symbol="BTCUSDT"),
                _row("2026-06-24 00:10:00", "entry", 200, symbol="ETHUSDT")]
        out = build_trade_markers(rows, "BTCUSDT", "4h")
        assert len(out["markers"]) == 1
        assert out["markers"][0]["price"] == 100

    def test_entry_long_classification(self):
        out = build_trade_markers([_row("2026-06-24 00:10:00", "entry", 100)],
                                  "BTCUSDT", "4h")
        m = out["markers"][0]
        assert m["side"] == "entry" and m["dir"] == 1

    def test_entry_short_classification(self):
        out = build_trade_markers([_row("2026-06-24 00:10:00", "entry_short", 100)],
                                  "BTCUSDT", "4h")
        m = out["markers"][0]
        assert m["side"] == "entry" and m["dir"] == -1

    def test_exit_classification(self):
        for side in ("exit_signal", "exit_sltp", "scale_out"):
            out = build_trade_markers([_row("2026-06-24 00:10:00", side, 100)],
                                      "BTCUSDT", "4h")
            assert out["markers"][0]["side"] == "exit", f"{side} 應分類為 exit"

    def test_time_snapped_to_interval_bucket(self):
        """ts 對齊到 interval 時間桶（floor），與 K 棒開盤時間一致。"""
        # 2026-06-24 01:41:00 UTC → 4h 桶 = 2026-06-24 00:00:00 UTC
        out = build_trade_markers([_row("2026-06-24 01:41:00", "entry", 100)],
                                  "BTCUSDT", "4h")
        t = out["markers"][0]["time"]
        assert t % 14400 == 0, "對齊後應為 interval 整數倍"
        # 對齊後不晚於原始時間
        from datetime import datetime, timezone
        raw = int(datetime(2026, 6, 24, 1, 41, tzinfo=timezone.utc).timestamp())
        assert t <= raw and raw - t < 14400

    def test_distinct_strategies_listed(self):
        rows = [_row("2026-06-24 00:10:00", "entry", 100, strategy="ema_cross"),
                _row("2026-06-24 04:10:00", "entry", 110, strategy="fib_channel"),
                _row("2026-06-24 08:10:00", "exit_sltp", 120, strategy="ema_cross")]
        out = build_trade_markers(rows, "BTCUSDT", "4h")
        assert set(out["strategies"]) == {"ema_cross", "fib_channel"}

    def test_markers_sorted_ascending_by_time(self):
        """lightweight-charts 要求標記時間遞增。"""
        rows = [_row("2026-06-24 08:00:00", "exit_sltp", 120),
                _row("2026-06-24 00:00:00", "entry", 100),
                _row("2026-06-24 04:00:00", "entry", 110)]
        out = build_trade_markers(rows, "BTCUSDT", "4h")
        times = [m["time"] for m in out["markers"]]
        assert times == sorted(times)

    def test_iso_with_timezone_parsed(self):
        out = build_trade_markers([_row("2026-06-24T01:41:00+00:00", "entry", 100)],
                                  "BTCUSDT", "4h")
        assert len(out["markers"]) == 1
        assert out["markers"][0]["time"] % 14400 == 0

    def test_unparseable_ts_skipped(self):
        rows = [_row("not-a-date", "entry", 100),
                _row("2026-06-24 00:00:00", "entry", 110)]
        out = build_trade_markers(rows, "BTCUSDT", "4h")
        assert len(out["markers"]) == 1 and out["markers"][0]["price"] == 110
