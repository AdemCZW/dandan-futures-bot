"""外部總經資料 — 目前只有 SPX(^GSPC) 日收盤，供 quant_researcher.SmcStructureStrategy
的美股連動過濾（use_corr_filter，見該檔案的 signal_engineer.daily_return_corr）用。

2026-08-06 切半驗證通過（corr_max=0.5 兩半皆不輸基準，見 research/scratchpad/
spx_corr_split_half.py），此模組是把它接上即時執行路徑缺的那段膠水程式碼。

原則與 ai_desk.fetch_live_price 一致：這是選用的總經參考，不是核心訊號，抓不到
時不可讓整輪分析中斷——一律失敗返回 None，由呼叫端（strategy 的 use_corr_filter
邏輯本身）決定「抓不到就視為不通過過濾」。
"""
from __future__ import annotations

import json
import time
import urllib.request

import pandas as pd

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC"
DEFAULT_RANGE = "2y"   # daily_return_corr 只需 20 日滾動窗，2 年遠遠夠用且穩定


def _default_fetch_json(url: str, timeout: int) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def parse_yahoo_chart(payload: dict) -> pd.Series | None:
    """Yahoo chart API 回應 → 日收盤 Series（DatetimeIndex）。格式不符或無資料回 None。"""
    try:
        result = payload["chart"]["result"][0]
        ts = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None
    if not ts:
        return None
    idx = pd.to_datetime(ts, unit="s").normalize()
    s = pd.Series(closes, index=idx, name="spx_close").dropna()
    return s if len(s) else None


def fetch_spx_daily(*, fetch_json=_default_fetch_json, timeout: int = 10) -> pd.Series | None:
    """抓 SPX 近 2 年日收盤。失敗（網路/格式任何原因）一律回 None，絕不拋錯。

    fetch_json 可注入（測試用假回應；生產預設打 Yahoo Finance 公開 API，免金鑰）。
    """
    try:
        url = f"{YAHOO_CHART_URL}?range={DEFAULT_RANGE}&interval=1d"
        payload = fetch_json(url, timeout)
    except Exception:
        return None
    return parse_yahoo_chart(payload)


class ExternalDailyCloseCache:
    """帶 TTL 的外部日收盤資料快取，供即時執行迴圈每輪呼叫。

    為什麼需要快取：日收盤資料一天只變一次，即時迴圈可能每分鐘輪詢，沒有快取
    會不必要地重複打外部 API、增加被限流風險。

    fetch_fn / now_fn 皆可注入，測試不碰真實時間或網路。fetch 失敗時沿用舊快取
    （若有）而非清空——總比讓過濾器整輪失去資料好；從未成功抓過則維持 None，
    交由呼叫端的過濾邏輯保守處理（缺值視為不通過，與其他過濾器一致）。
    """

    def __init__(self, fetch_fn=fetch_spx_daily, ttl_seconds: int = 3600, now_fn=time.time):
        self._fetch_fn = fetch_fn
        self._ttl = ttl_seconds
        self._now_fn = now_fn
        self._cached: pd.Series | None = None
        self._cached_at: float | None = None

    def get(self) -> pd.Series | None:
        now = self._now_fn()
        stale = self._cached_at is None or (now - self._cached_at) > self._ttl
        if stale:
            fresh = self._fetch_fn()
            if fresh is not None:
                self._cached = fresh
                self._cached_at = now
        return self._cached
