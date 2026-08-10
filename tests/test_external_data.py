"""core.external_data 測試 — SPX 日收盤抓取 + TTL 快取，供 smc_structure 的
美股連動過濾（use_corr_filter）即時管道用。全部依賴注入，不碰真實網路。
"""
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.external_data import ExternalDailyCloseCache, fetch_spx_daily, parse_yahoo_chart


# ── fetch_spx_daily：純解析邏輯（注入 fetch_fn，不碰網路）────────────

def _fake_chart_json(closes, start_ts=1700000000, step=86400):
    return {"chart": {"result": [{
        "timestamp": [start_ts + i * step for i in range(len(closes))],
        "indicators": {"quote": [{"close": closes}]},
    }]}}


def test_parse_yahoo_chart_returns_series_indexed_by_date():
    s = parse_yahoo_chart(_fake_chart_json([100.0, 101.5, 102.0]))
    assert isinstance(s, pd.Series)
    assert len(s) == 3
    assert isinstance(s.index, pd.DatetimeIndex)


def test_parse_yahoo_chart_drops_none_closes():
    """Yahoo 對非交易日常回 None，不可讓這些洞污染序列。"""
    s = parse_yahoo_chart(_fake_chart_json([100.0, None, 102.0]))
    assert len(s) == 2
    assert not s.isna().any()


def test_parse_yahoo_chart_empty_result_returns_none():
    assert parse_yahoo_chart({"chart": {"result": [{"timestamp": [],
                              "indicators": {"quote": [{"close": []}]}}]}}) is None


def test_fetch_spx_daily_success(monkeypatch):
    def fake_fetch_json(url, timeout):
        return _fake_chart_json([100.0, 101.0])
    s = fetch_spx_daily(fetch_json=fake_fetch_json)
    assert len(s) == 2


def test_fetch_spx_daily_never_raises_returns_none_on_failure():
    """失敗一律回 None，不拋錯——這是選用的總經參考，不該讓整輪分析中斷
    （比照 ai_desk.fetch_live_price 的容錯慣例）。"""
    def boom(url, timeout):
        raise RuntimeError("網路斷線")
    assert fetch_spx_daily(fetch_json=boom) is None


def test_fetch_spx_daily_malformed_response_returns_none():
    def bad(url, timeout):
        return {"unexpected": "shape"}
    assert fetch_spx_daily(fetch_json=bad) is None


# ── ExternalDailyCloseCache：TTL 快取，全時間/抓取皆注入 ──────────────

def test_cache_fetches_on_first_get():
    calls = []

    def fetch():
        calls.append(1)
        return pd.Series([1.0], index=pd.to_datetime(["2026-01-01"]))

    cache = ExternalDailyCloseCache(fetch_fn=fetch, ttl_seconds=3600, now_fn=lambda: 1000.0)
    result = cache.get()
    assert len(calls) == 1
    assert result is not None


def test_cache_reuses_within_ttl():
    calls = []
    clock = {"t": 1000.0}

    def fetch():
        calls.append(1)
        return pd.Series([1.0], index=pd.to_datetime(["2026-01-01"]))

    cache = ExternalDailyCloseCache(fetch_fn=fetch, ttl_seconds=3600, now_fn=lambda: clock["t"])
    cache.get()
    clock["t"] += 1800                       # 還在 TTL 內
    cache.get()
    assert len(calls) == 1                   # 沒有重打


def test_cache_refetches_after_ttl_expires():
    calls = []
    clock = {"t": 1000.0}

    def fetch():
        calls.append(1)
        return pd.Series([float(len(calls))], index=pd.to_datetime(["2026-01-01"]))

    cache = ExternalDailyCloseCache(fetch_fn=fetch, ttl_seconds=3600, now_fn=lambda: clock["t"])
    cache.get()
    clock["t"] += 3601
    cache.get()
    assert len(calls) == 2


def test_cache_keeps_stale_value_when_refetch_fails():
    """抓取失敗時沿用舊快取——總比整輪過濾器直接失效好。"""
    responses = [pd.Series([1.0], index=pd.to_datetime(["2026-01-01"])), None]
    clock = {"t": 1000.0}

    def fetch():
        return responses.pop(0)

    cache = ExternalDailyCloseCache(fetch_fn=fetch, ttl_seconds=3600, now_fn=lambda: clock["t"])
    first = cache.get()
    clock["t"] += 3601
    second = cache.get()                     # 這次 fetch 回 None
    assert second is first                   # 沿用舊值，不是 None


def test_cache_returns_none_when_never_successfully_fetched():
    cache = ExternalDailyCloseCache(fetch_fn=lambda: None, ttl_seconds=3600, now_fn=lambda: 1000.0)
    assert cache.get() is None
