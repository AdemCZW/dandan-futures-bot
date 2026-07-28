"""市場簡報 — 把 signal_engineer 算好的指標轉成給 LLM 讀的結構化事實。

刻意不把原始 OHLCV 丟給 LLM：模型自己「算」指標既不可靠也浪費 token，
所有數字都來自 core.signal_engineer 已驗證過的因果計算。純函式、不碰網路。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from core import signal_engineer as se

MIN_BARS = 200  # 指標暖機下限（ema26/rsi14/zscore50/fib50 皆綽綽有餘）

DAILY_MA_PERIODS = (20, 60, 120, 200)
"""多層日均線週期。

為什麼要多層：原本只有 htf_trend 一個「多頭/空頭」標籤，但它用 20/60 日均線、
在只餵 400 根 4h（≈66 天）時 60 日均線僅 7 個有效值，89.5% 的時間算不出來，
卻仍輸出一個看似確定的方向給 AI 當證據。改成攤開四層均線的實際數值，
讓 AI 自己判斷排列與距離；算不出來的一律回 None 並在簡報明示「資料不足」，
不給假數字。

資料需求（每天 6 根 4h）：MA200 需 1200 根才有第一個有效值；
進入點已改抓 1500 根（幣安單次上限，≈250 天）。
"""


@dataclass
class MarketBriefing:
    symbol: str
    interval: str
    as_of: str            # 最後一根已收盤 K 棒的時間
    close: float
    ema_fast: float       # EMA12
    ema_slow: float       # EMA26
    rsi: float
    atr: float
    zscore: float
    fib_pos: float        # 0=區間低點, 1=區間高點
    fib_382: float
    fib_618: float
    htf_trend: int        # 日線趨勢 +1/-1/0（已 shift、無前視）
    recent_closes: list   # 最近 6 根收盤價
    daily_mas: dict = field(default_factory=dict)
    """日均線收盤值 {20: v, 60: v, 120: v, 200: v}；資料不足者為 None。

    一律用「已收完的日線」（shift(1)）——今天的日線還在跑，用它就是前視。
    """
    live_price: float | None = None
    """此刻的市場成交價（僅供 AI 感知「收盤後價格已走多遠」，不參與任何指標計算）。

    所有指標一律只用已收盤 K 棒（防前視）；但 4h 週期下，最後一根收盤到現在最多
    可差 4 小時，實測曾出現 -0.81% 的落差，AI 若完全不知情容易掛出過時的價位。
    抓不到現價時為 None，簡報就不顯示這段——絕不可因此讓整輪分析失敗。
    """


def compute_daily_mas(df: pd.DataFrame, periods=DAILY_MA_PERIODS) -> dict:
    """把 4h 重採樣成日線，算各週期均線的最新值（僅用已收完的日線）。

    資料不足的週期回 None——寧可讓 AI 知道「這層算不出來」，
    也不要給一個暖機不足、統計上毫無意義的數字當證據。
    """
    daily = df["close"].resample("1D").last().dropna()
    out = {}
    for n in periods:
        ma = daily.rolling(n, min_periods=n).mean().shift(1)
        v = ma.iloc[-1] if len(ma) else float("nan")
        out[n] = None if pd.isna(v) else float(v)
    return out


def build_market_briefing(df: pd.DataFrame, symbol: str, interval: str,
                          live_price: float | None = None) -> MarketBriefing:
    """df：已收盤 K 棒（DatetimeIndex + open/high/low/close/volume）。

    任一關鍵指標為 NaN → 拋 ValueError（寧可整輪失敗，不餵 LLM 髒資料）。
    """
    if len(df) < MIN_BARS:
        raise ValueError(f"K 棒不足（{len(df)} 根），指標暖機至少需要 {MIN_BARS} 根")
    enriched = se.enrich(df)
    enriched["htf_trend"] = se.htf_trend(df)
    last = enriched.iloc[-1]
    for name in ("ema_fast", "ema_slow", "rsi", "atr", "zscore", "fib_pos"):
        if math.isnan(float(last[name])):
            raise ValueError(f"指標 {name} 為 NaN（暖機不足或資料異常），拒絕產生簡報")
    return MarketBriefing(
        symbol=symbol,
        interval=interval,
        as_of=str(df.index[-1]),
        close=float(last["close"]),
        ema_fast=float(last["ema_fast"]),
        ema_slow=float(last["ema_slow"]),
        rsi=float(last["rsi"]),
        atr=float(last["atr"]),
        zscore=float(last["zscore"]),
        fib_pos=float(last["fib_pos"]),
        fib_382=float(last["fib_382"]),
        fib_618=float(last["fib_618"]),
        htf_trend=int(last["htf_trend"]),
        recent_closes=[float(x) for x in df["close"].iloc[-6:]],
        daily_mas=compute_daily_mas(df),
        live_price=None if live_price is None else float(live_price),
    )


def format_briefing(b: MarketBriefing) -> str:
    """給 LLM 的純文字簡報。只陳述事實，不帶方向暗示。"""
    trend_txt = {1: "多頭", -1: "空頭", 0: "中性/暖機中"}[b.htf_trend]
    recent = " → ".join(f"{x:.2f}" for x in b.recent_closes)
    return (
        f"標的：{b.symbol}（{b.interval} 週期）\n"
        f"資料時間：{b.as_of}（最後一根已收盤 K 棒）\n"
        f"收盤價：{b.close:.2f}\n"
        f"EMA12：{b.ema_fast:.2f}／EMA26：{b.ema_slow:.2f}\n"
        f"RSI(14)：{b.rsi:.1f}\n"
        f"ATR(14)：{b.atr:.2f}\n"
        f"收盤價 Z 分數(50)：{b.zscore:.2f}\n"
        f"Fib 區間位置：{b.fib_pos:.3f}（0=區間低點, 1=區間高點）\n"
        f"Fib 38.2% 水位：{b.fib_382:.2f}／61.8% 水位：{b.fib_618:.2f}\n"
        f"日線趨勢（20/60日均線）：{trend_txt}\n"
        f"{_ma_stack_line(b)}"
        f"最近 6 根收盤：{recent}"
        + _live_price_line(b)
    )


def _ma_stack_line(b: MarketBriefing) -> str:
    """多層日均線一覽，並標示價格在各層的上/下方。資料不足者明說。"""
    if not b.daily_mas:
        return ""
    parts = []
    for n in DAILY_MA_PERIODS:
        v = b.daily_mas.get(n)
        if v is None:
            parts.append(f"MA{n}：資料不足")
        else:
            side = "上" if b.close >= v else "下"
            parts.append(f"MA{n}：{v:.2f}（價在其{side}方）")
    return "日均線層級：" + "；".join(parts) + "\n"


def _live_price_line(b: MarketBriefing) -> str:
    """現價附註。刻意放在最後、並標明「不參與指標」，避免模型拿它當收盤價用。"""
    if b.live_price is None:
        return ""
    drift = b.live_price - b.close
    pct = (drift / b.close * 100) if b.close else 0.0
    return (
        f"\n\n【即時參考】此刻市場現價：{b.live_price:.2f}"
        f"（自上述收盤價已變動 {drift:+.2f}，{pct:+.2f}%）\n"
        "註：上方所有指標一律以「已收盤 K 棒」計算（防前視），不含此現價；"
        "本行僅供你判斷掛單價位時參考價格已走多遠。"
    )
