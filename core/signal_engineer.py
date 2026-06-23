"""信號工程師 /signal-engineer — 技術指標、信號管線。

純函式，輸入價格序列，輸出指標欄位。不依賴幣安。
"""
import pandas as pd
import numpy as np


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — 風控官用來估波動、設停損。"""
    return _true_range(df).ewm(alpha=1 / period, adjust=False).mean()


def zscore(series: pd.Series, window: int = 50) -> pd.Series:
    """滾動 z-score（圖 2 那個 mean-reversion alpha 用的就是這個概念）。"""
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std


def _true_range(df: pd.DataFrame) -> pd.Series:
    """True Range：max(高-低, |高-前收|, |低-前收|)。用 close.shift(1)，causal。"""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    return pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder 趨勢強度：回傳 plus_di / minus_di / adx。

    判別「有沒有趨勢」（ADX 高）與「往哪個方向」（+DI vs -DI），用來做 regime 過濾。
    全部只用當根 high/low 與前一根收盤（close.shift(1)），Wilder 平滑＝
    ewm(alpha=1/period, adjust=False)（與 atr() 同性質），天然 causal、不 repaint。
    """
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    atr_w = _true_range(df).ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_w
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr_w
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_ = dx.ewm(alpha=1 / period, adjust=False).mean()
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_}, index=df.index)


def efficiency_ratio(close: pd.Series, period: int = 14) -> pd.Series:
    """Kaufman 效率比：淨位移 / 路徑總長，值域 [0, 1]。

    1＝完美單向趨勢、0＝純粹來回盤整。只用過去 period 根與當根收盤，causal。
    """
    change = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(period).sum()
    return change / volatility.replace(0, np.nan)


def choppiness_index(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Choppiness Index：>61.8 盤整、<38.2 趨勢。

    用 ΣTR 與滾動波段高低之比的對數正規化。TR 用 close.shift(1)、其餘為滾動過去窗，causal。
    """
    tr_sum = _true_range(df).rolling(period).sum()
    rng = df["high"].rolling(period).max() - df["low"].rolling(period).min()
    return 100 * np.log10(tr_sum / rng.replace(0, np.nan)) / np.log10(period)


def _confirmed_swing_levels(df: pd.DataFrame, left: int, right: int):
    """已確認的 swing 高/低點，向前填補成連續水位（causal、不 repaint）。

    某根 j 是 swing high，須 high[j] = max(high[j-left .. j+right])，且只在 j+right 根
    「收完」後才確認生效（接受 right 根延遲）。以 trailing rolling + shift(right) 實作：
    在當根 k，檢查 right 根前那根（k-right）是否為其 [left+right+1] 視窗的極值。整條只用
    截至當根的資料 → 末根擾動不改寫較早已確認的 pivot（非重繪）。
    """
    high, low = df["high"], df["low"]
    # 嚴格極值判定（避免平台/ties 被逐根誤標後 ffill 覆蓋真正 pivot）：
    # 候選根（k-right）須「嚴格大於右側 right 根」且「不低於左側 left 根」。
    # 右側嚴格 + 左側含等號 → 平台只在最後一根標記一次，持平段不會逐根誤標。
    hs = high.shift(right)                                  # 候選根 high[j]，在 k 確認
    right_max_h = high.rolling(right).max()                # max(high[j+1 .. j+right])
    left_max_h = high.shift(right + 1).rolling(left).max() # max(high[j-left .. j-1])
    is_ph = (hs > right_max_h) & (hs >= left_max_h)
    fib_high = hs.where(is_ph).ffill()

    ls = low.shift(right)
    right_min_l = low.rolling(right).min()
    left_min_l = low.shift(right + 1).rolling(left).min()
    is_pl = (ls < right_min_l) & (ls <= left_min_l)
    fib_low = ls.where(is_pl).ffill()
    return fib_high, fib_low


def fib_retracement(df: pd.DataFrame, lookback: int = 50,
                    pivot_left: int = None, pivot_right: int = None) -> pd.DataFrame:
    """斐波那契回調指標。

    波段高/低點兩種來源（皆 causal、不含未來資訊）：
      - 預設（pivot_* 為 None）：滾動 lookback 根的高/低，用 .shift(1) 不含當根。
      - swing 模式（給 pivot_left/right）：已確認的 swing 擺動點（右側 right 根確認後生效，
        不重繪），比固定盒子極值更貼合結構性高低點。

    新增欄位：
        fib_high  — 波段最高價
        fib_low   — 波段最低價
        fib_pos   — (close - fib_low) / range；0 = 低點、1 = 高點；range=0 時為 NaN
        fib_382   — fib_low + 0.382 * range（38.2% 水位）
        fib_618   — fib_low + 0.618 * range（61.8% 黃金比例水位）
    """
    out = df.copy()
    if pivot_left is not None and pivot_right is not None:
        fib_high, fib_low = _confirmed_swing_levels(df, int(pivot_left), int(pivot_right))
    else:
        # shift(1)：只看「已收完」的過去 lookback 根，不含當根
        fib_high = df["high"].shift(1).rolling(lookback).max()
        fib_low  = df["low"].shift(1).rolling(lookback).min()
    fib_range = fib_high - fib_low
    out["fib_high"] = fib_high
    out["fib_low"]  = fib_low
    # range = 0 → NaN，避免除零
    out["fib_pos"]  = (df["close"] - fib_low) / fib_range.replace(0, np.nan)
    out["fib_382"]  = fib_low + 0.382 * fib_range
    out["fib_618"]  = fib_low + 0.618 * fib_range
    return out


def regime(df: pd.DataFrame, er_period: int = 14, er_trend: float = 0.30,
           chop_period: int = 14, chop_trend: float = 38.2,
           adx_period: int = 14, adx_trend: float = 25.0,
           confirm_bars: int = 2) -> pd.DataFrame:
    """市場狀態判別（趨勢 vs 盤整），用 ER / CHOP / ADX 三票多數決 + 去抖。

    每根三票：ER>er_trend、CHOP<chop_trend、ADX>adx_trend 各算一票「趨勢」，
    ≥2 票 → raw='trend'，否則 'range'。再用 confirm_bars 去抖：raw 連續一致達
    confirm_bars 根才切換 confirmed regime（避免單根雜訊頻繁換邊）。

    全部只用過去與當根已收盤資料，去抖為單向前掃（只看過去）→ causal、不 repaint。
    回傳 DataFrame[er, chop, adx, regime]，regime ∈ {'trend','range',None(warmup/未確認)}。
    """
    er = efficiency_ratio(df["close"], er_period)
    chop = choppiness_index(df, chop_period)
    adx_ = adx(df, adx_period)["adx"]

    votes = ((er > er_trend).astype(float)
             + (chop < chop_trend).astype(float)
             + (adx_ > adx_trend).astype(float))
    defined = er.notna() & chop.notna() & adx_.notna()
    raw = pd.Series(np.where(votes >= 2, "trend", "range"), index=df.index, dtype=object)
    raw[~defined] = None

    confirmed, cur, run_val, run_len = [], None, None, 0
    for r in raw:
        if r is None:
            confirmed.append(cur)
            continue
        if r == run_val:
            run_len += 1
        else:
            run_val, run_len = r, 1
        if cur is None:
            if run_len >= confirm_bars:
                cur = r
        elif r != cur and run_len >= confirm_bars:
            cur = r
        confirmed.append(cur)

    return pd.DataFrame({
        "er": er, "chop": chop, "adx": adx_,
        "regime": pd.Series(confirmed, index=df.index, dtype=object),
    }, index=df.index)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """一次把常用指標都算好，附加到 DataFrame。"""
    out = df.copy()
    out["ema_fast"] = ema(out["close"], 12)
    out["ema_slow"] = ema(out["close"], 26)
    out["rsi"] = rsi(out["close"], 14)
    out["atr"] = atr(out, 14)
    out["zscore"] = zscore(out["close"], 50)
    out = fib_retracement(out, lookback=50)
    return out
