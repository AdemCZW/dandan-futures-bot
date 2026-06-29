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


def taker_buy_ratio(df: pd.DataFrame, smooth: int = 1) -> pd.Series:
    """主動買盤佔比 = taker_base / volume ∈ [0,1]（訂單流失衡）。

    taker_base 是「吃 ask 的主動買進量」（K 線自帶欄位），>0.5 代表主動買盤
    壓過主動賣盤。每根只用當根量能，天然 causal、不含未來。
      - 缺 taker_base 欄（合成資料/舊快取）→ 回全 NaN，讓上層優雅退化。
      - volume=0 → 該根 NaN，避免除零。
      - smooth>1：對比值做 EMA 平滑（仍 causal）。
    """
    if "taker_base" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    vol = df["volume"].replace(0, np.nan)
    ratio = df["taker_base"] / vol
    if smooth and smooth > 1:
        ratio = ratio.ewm(span=int(smooth), adjust=False).mean()
    return ratio


def cvd(df: pd.DataFrame) -> pd.Series:
    """累積量差 Cumulative Volume Delta = Σ(主動買量 − 主動賣量)。

    每根量差 = taker_base − (volume − taker_base) = 2·taker_base − volume。
    累積後反映「主動買賣的淨流向」：上升＝買盤累積、下降＝賣盤累積。
    缺 taker_base 欄 → 全 NaN（優雅退化）。只用過去與當根，causal。
    """
    if "taker_base" not in df.columns:
        return pd.Series(np.nan, index=df.index)
    delta = 2.0 * df["taker_base"] - df["volume"]
    return delta.cumsum()


def cvd_price_divergence(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """價格與 CVD 的背離（訂單流竭盡過濾，OPT-16）。causal：只用 ≤t 的滾動變化。

    比較過去 window 根的價格淨變化與 CVD 淨變化：
      -1 頂背離：價漲(price_chg>0) 但 CVD 走低(cvd_chg<0)＝買盤竭盡/上方吸收 → 別追多。
      +1 底背離：價跌(price_chg<0) 但 CVD 走高(cvd_chg>0)＝賣盤被吸收 → 別追空。
       0 無背離 / 缺 taker_base（合成資料）/ 暖機不足。

    用既有 K 線自帶 taker_base，零外部資料。不單獨進場，僅當合流/矛盾的降風險閘門。
    """
    c = cvd(df)
    out = pd.Series(0.0, index=df.index)
    if c.isna().all():
        return out                                   # 缺 taker_base → 全 0，優雅退化
    w = max(int(window), 1)
    price_chg = df["close"] - df["close"].shift(w)
    cvd_chg = c - c.shift(w)
    out[(price_chg > 0) & (cvd_chg < 0)] = -1.0       # 頂背離（買盤竭盡）
    out[(price_chg < 0) & (cvd_chg > 0)] = 1.0        # 底背離（賣盤吸收）
    return out


def donchian(df: pd.DataFrame, entry_period: int = 20, exit_period: int = 10) -> pd.DataFrame:
    """Donchian 通道（Turtle 海龜突破）。

    進場通道＝過去 entry_period 根的高/低；出場通道＝過去 exit_period 根的高/低。
    全用 high/low.shift(1)（只看「已收完的過去 N 根」），由策略拿當根 close 去比，
    故突破判定不含當根自身 → causal、不 repaint。

    回傳 DataFrame：
        dc_upper / dc_lower         — 進場通道（突破上軌做多、跌破下軌做空）
        dc_exit_long / dc_exit_short — 出場通道（多單跌破 exit_long 出、空單突破 exit_short 出）
    """
    high, low = df["high"], df["low"]
    return pd.DataFrame({
        "dc_upper": high.shift(1).rolling(entry_period).max(),
        "dc_lower": low.shift(1).rolling(entry_period).min(),
        "dc_exit_long": low.shift(1).rolling(exit_period).min(),
        "dc_exit_short": high.shift(1).rolling(exit_period).max(),
    }, index=df.index)


def supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    """Supertrend — ATR 通道趨勢跟蹤（BTC 最常被引用的穩健趨勢策略核心）。

    以 hl2 ± multiplier×ATR 造上下軌，再「鎖定」成只朝有利方向收緊的最終軌：
      - 收盤上穿上軌 → 轉多(st_dir=+1)，趨勢線跟下軌；
      - 收盤下穿下軌 → 轉空(st_dir=-1)，趨勢線跟上軌。
    band 鎖定為遞迴（依賴前一根最終軌與方向），但只用過去與當根 → causal、不 repaint。

    回傳 DataFrame[supertrend, st_dir]，st_dir ∈ {+1, -1, NaN(warmup)}。
    """
    atr_ = atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upper_basic = hl2 + multiplier * atr_
    lower_basic = hl2 - multiplier * atr_
    close = df["close"].to_numpy()
    ub, lb = upper_basic.to_numpy(), lower_basic.to_numpy()
    n = len(df)
    final_ub = np.full(n, np.nan)
    final_lb = np.full(n, np.nan)
    st = np.full(n, np.nan)
    direction = np.full(n, np.nan)

    # 第一根有效 ATR 之後才開始（warmup 期 ATR 仍有值但不穩，沿用標準作法從頭遞迴）
    prev_dir = 1
    for i in range(n):
        if np.isnan(ub[i]):
            continue
        if i == 0 or np.isnan(final_ub[i - 1]):
            final_ub[i], final_lb[i] = ub[i], lb[i]
            direction[i] = prev_dir
            st[i] = final_lb[i] if prev_dir == 1 else final_ub[i]
            continue
        # 最終上軌：只在更低或前收已突破時才更新（否則鎖住）
        final_ub[i] = ub[i] if (ub[i] < final_ub[i - 1] or close[i - 1] > final_ub[i - 1]) else final_ub[i - 1]
        final_lb[i] = lb[i] if (lb[i] > final_lb[i - 1] or close[i - 1] < final_lb[i - 1]) else final_lb[i - 1]
        # 方向翻轉：收盤穿越前一根最終軌
        if close[i] > final_ub[i - 1]:
            direction[i] = 1
        elif close[i] < final_lb[i - 1]:
            direction[i] = -1
        else:
            direction[i] = prev_dir
        prev_dir = int(direction[i])
        st[i] = final_lb[i] if direction[i] == 1 else final_ub[i]

    return pd.DataFrame({"supertrend": st, "st_dir": direction}, index=df.index)


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


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.DataFrame:
    """價格 MACD：line=ema(fast)−ema(slow)、signal=ema(line)、hist=line−signal。

    全為 ema（adjust=False，causal）的組合，天然 causal、不 repaint。
    與 of_momentum（MACD-on-CVD，吃訂單流）不同：這是吃【價格】的動量。
    """
    line = ema(close, fast) - ema(close, slow)
    signal = ema(line, sig)
    return pd.DataFrame({"macd_line": line, "macd_signal": signal,
                         "macd_hist": line - signal}, index=close.index)


def bollinger(close: pd.Series, period: int = 20, mult: float = 2.0) -> pd.DataFrame:
    """布林通道：mid=SMA(period)、上下軌=mid±mult×母體標準差(ddof=0)。

    另回傳 bandwidth=(上−下)/mid（波動壓縮度，squeeze 用）與
    pct_b=(close−下)/(上−下)（band 位置，0=下軌、1=上軌；可超出 [0,1]）。
    全用滾動過去 period 根，causal。range=0（常數段）→ pct_b=NaN 避免除零。
    """
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)            # Bollinger 慣例：母體標準差
    upper = mid + mult * sd
    lower = mid - mult * sd
    width = upper - lower
    return pd.DataFrame({
        "bb_mid": mid, "bb_upper": upper, "bb_lower": lower,
        "bandwidth": width / mid.replace(0, np.nan),
        "pct_b": (close - lower) / width.replace(0, np.nan),
    }, index=close.index)


def stochastic(df: pd.DataFrame, k_period: int = 14,
               smooth_k: int = 3, d_period: int = 3) -> pd.DataFrame:
    """隨機指標 KD：

    raw %K = 100 × (close − LL(k_period)) / (HH(k_period) − LL(k_period))
    stoch_k = SMA(raw %K, smooth_k)（慢速 %K）；stoch_d = SMA(stoch_k, d_period)（%D）。
    全用滾動過去 k_period 根的 high/low，causal、不 repaint。
    區間 [0,100]。HH==LL（range=0，常數段）→ raw %K = NaN，避免除零。
    """
    high, low, close = df["high"], df["low"], df["close"]
    ll = low.rolling(k_period).min()
    hh = high.rolling(k_period).max()
    rng = (hh - ll).replace(0, np.nan)
    raw_k = 100 * (close - ll) / rng
    k = raw_k.rolling(smooth_k).mean() if smooth_k > 1 else raw_k
    d = k.rolling(d_period).mean()
    return pd.DataFrame({"stoch_k": k, "stoch_d": d}, index=df.index)


def rolling_vwap(df: pd.DataFrame, window: int = 50) -> pd.Series:
    """滾動 N 根成交量加權典型價（causal，無 session 錨點）。

    典型價=(high+low+close)/3；vwap=Σ(典型價×量)/Σ量（過去 window 根）。
    這是日內 VWAP 的 causal 變體：不重置、只看過去 window 根，故無 look-ahead。
    Σ量=0 → NaN（避免除零）。
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"]
    pv = (typical * vol).rolling(window).sum()
    vv = vol.rolling(window).sum()
    return pv / vv.replace(0, np.nan)


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Heikin-Ashi 平均 K 線（遞迴、causal）。

    ha_close=(O+H+L+C)/4；ha_open[i]=(ha_open[i−1]+ha_close[i−1])/2，
    種子 ha_open[0]=(O[0]+C[0])/2；ha_high=max(H, ha_open, ha_close)、
    ha_low=min(L, ha_open, ha_close)。遞迴只依賴前一根 → causal、不 repaint。
    """
    o = df["open"].to_numpy(); h = df["high"].to_numpy()
    l = df["low"].to_numpy();  c = df["close"].to_numpy()
    n = len(df)
    ha_close = (o + h + l + c) / 4.0
    ha_open = np.empty(n)
    if n:
        ha_open[0] = (o[0] + c[0]) / 2.0
        for i in range(1, n):
            ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
    ha_high = np.maximum.reduce([h, ha_open, ha_close]) if n else np.array([])
    ha_low = np.minimum.reduce([l, ha_open, ha_close]) if n else np.array([])
    return pd.DataFrame({"ha_open": ha_open, "ha_close": ha_close,
                         "ha_high": ha_high, "ha_low": ha_low}, index=df.index)


def smc_levels(df: pd.DataFrame, pivot_left: int = 5, pivot_right: int = 5) -> pd.DataFrame:
    """Smart Money Concept 結構偵測：Break of Structure + Fair Value Gap（causal、不 repaint）。

    Break of Structure（BOS）：收盤突破最近已確認的 swing 極值。
      pivot 確認需 right 根延遲（_confirmed_swing_levels 同機制），所以不含未來資料。
    Fair Value Gap（FVG）：三根 K 線間的價格缺口。
      看漲 FVG：high[i-2] < low[i]（i-1 根 body 未能覆蓋，存在向上缺口）
      看跌 FVG：low[i-2] > high[i]（向下缺口）

    回傳欄位：bos_bull / bos_bear / fvg_bull / fvg_bear（0/1 float）
    """
    out = df.copy()
    fib_high, fib_low = _confirmed_swing_levels(df, pivot_left, pivot_right)

    out["bos_bull"] = (df["close"] > fib_high).astype(float)
    out["bos_bear"] = (df["close"] < fib_low).astype(float)

    # FVG：用 shift(2) 取 i-2 根的極值，與當根比較 → 完全 causal
    out["fvg_bull"] = (df["high"].shift(2) < df["low"]).astype(float)
    out["fvg_bear"] = (df["low"].shift(2) > df["high"]).astype(float)

    # 缺口中點（可作為進場目標參考）
    out["fvg_bull_mid"] = (df["high"].shift(2) + df["low"]) / 2
    out["fvg_bear_mid"] = (df["low"].shift(2) + df["high"]) / 2

    # warmup 期（pivot 尚未確認）的 BOS 強制設 NaN
    warmup = pivot_left + pivot_right
    out.loc[out.index[:warmup], ["bos_bull", "bos_bear"]] = np.nan

    return out


# 費波那契通道比率 → 欄位名。0/1.0 為錨點，其餘（含 >1.0 延伸）皆平行排列。
FIB_CHANNEL_RATIOS: dict[float, str] = {
    0.0:   "fib_ch_0",
    0.236: "fib_ch_236",
    0.382: "fib_ch_382",
    0.5:   "fib_ch_5",
    0.618: "fib_ch_618",
    0.786: "fib_ch_786",
    1.0:   "fib_ch_100",
    1.272: "fib_ch_1272",
    1.618: "fib_ch_1618",
    2.0:   "fib_ch_200",
}


def _confirmed_pivots(values: np.ndarray, pivot_left: int, pivot_right: int,
                      kind: str) -> list[tuple[int, float]]:
    """找所有確認的 swing pivot（j+pivot_right 根後才知道 j 是 pivot，故 causal）。

    kind="low"  → swing low（局部最小）；kind="high" → swing high（局部最大）。
    """
    n = len(values)
    out: list[tuple[int, float]] = []
    for j in range(pivot_left, n - pivot_right):
        left  = values[j - pivot_left: j]
        right = values[j + 1: j + pivot_right + 1]
        if kind == "low":
            if values[j] < right.min() and values[j] <= left.min():
                out.append((j, values[j]))
        else:
            if values[j] > right.max() and values[j] >= left.max():
                out.append((j, values[j]))
    return out


def fib_channel_levels(df: pd.DataFrame, pivot_left: int = 5, pivot_right: int = 5,
                       trend_fast: int = 20, trend_slow: int = 50) -> pd.DataFrame:
    """費波那契斜向通道（與 TradingView Fibonacci Channel 同定義，漲跌雙向）。

    構造方式：
      1. 趨勢方向：EMA(fast) vs EMA(slow) 判定（causal）。
         上升 → 沿最近兩個已確認 swing low 連線（0 線在下、支撐）。
         下降 → 沿最近兩個已確認 swing high 連線（0 線在上、阻力）。
      2. 通道寬度 W：基線確立後，[p1 → 當根] 視窗內價格往對側超出基線的最大實際距離
         （上升取 high−基線、下降取 基線−low）。完全用實際振幅，非 ATR。
      3. 每條線 = fib_ch_0 + sign × r × W，sign=+1 上升 / −1 下降，
         r ∈ {0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618, 2.0}。
         等價於 fib_ch_0 + r × (fib_ch_100 − fib_ch_0)，即平行 + 費波那契間距。

    全部 causal：pivot 需延遲確認，寬度與趨勢只用過去 K 線。

    新增欄位：FIB_CHANNEL_RATIOS 的 10 條線 + fib_ch_pos + fib_ch_dir。
      fib_ch_pos  收盤相對位置：0=趨勢原點（進場側）, 1=對側（目標側）；與方向無關。
      fib_ch_dir  趨勢方向：+1 上升 / −1 下降 / 0 未定（無有效通道）。
    """
    out = df.copy()
    n = len(df)
    lows   = df["low"].values
    highs  = df["high"].values
    closes = df["close"].values

    level_cols = list(FIB_CHANNEL_RATIOS.values())
    for col in level_cols + ["fib_ch_pos", "fib_ch_dir"]:
        out[col] = np.nan
    out["fib_ch_dir"] = 0.0

    # 趨勢方向（causal）：快線 ≥ 慢線視為上升
    ema_f = ema(df["close"], trend_fast).values
    ema_s = ema(df["close"], trend_slow).values

    swing_lows  = _confirmed_pivots(lows,  pivot_left, pivot_right, "low")
    swing_highs = _confirmed_pivots(highs, pivot_left, pivot_right, "high")
    if len(swing_lows) < 2 and len(swing_highs) < 2:
        return out

    col_idx = {c: out.columns.get_loc(c) for c in level_cols}
    pos_idx = out.columns.get_loc("fib_ch_pos")
    dir_idx = out.columns.get_loc("fib_ch_dir")

    for i in range(n):
        if np.isnan(ema_f[i]) or np.isnan(ema_s[i]):
            continue
        up = ema_f[i] >= ema_s[i]
        pivots = swing_lows if up else swing_highs
        confirmed = [(j, p) for j, p in pivots if j + pivot_right <= i]
        if len(confirmed) < 2:
            continue

        p1_j, p1_v = confirmed[-2]
        p2_j, p2_v = confirmed[-1]
        if p2_j <= p1_j:
            continue

        slope = (p2_v - p1_v) / (p2_j - p1_j)
        # 基線（0 線）在各 bar 的值，覆蓋窗口 [p1_j, i]
        ks       = np.arange(p1_j, i + 1)
        baseline = p1_v + slope * (ks - p1_j)
        ch0 = p1_v + slope * (i - p1_j)             # 0 線在當根

        if up:
            width = float((highs[p1_j:i + 1] - baseline).max())   # high 超出支撐
            sign  = 1.0
        else:
            width = float((baseline - lows[p1_j:i + 1]).max())    # low 跌破阻力
            sign  = -1.0

        if width <= 0:
            continue

        for r, col in FIB_CHANNEL_RATIOS.items():
            out.iloc[i, col_idx[col]] = ch0 + sign * r * width
        out.iloc[i, pos_idx] = (closes[i] - ch0) * sign / width
        out.iloc[i, dir_idx] = sign

    return out


def fib_channel_single(df: pd.DataFrame, pivot_left: int = 5, pivot_right: int = 5,
                       trend_fast: int = 20, trend_slow: int = 50) -> dict | None:
    """畫圖用的「單一」費波那契通道（與 TradingView 手動畫一條一致）。

    不同於 fib_channel_levels（逐根 causal、供策略用），本函式只回傳「當前最新結構」
    的一條通道參數，前端可據此把每條費波那契線拉成橫跨整圖的直線——乾淨不跳動。

    取最後一根的趨勢方向，沿最近兩個已確認 pivot（漲沿 swing low / 跌沿 swing high）
    定基線斜率，寬度 = 錨點起算到末根之間價格往對側的最大實際距離。

    回傳 dict（無足夠 pivot 時回 None）：
      dir          +1 上升 / −1 下降
      anchor_idx   0 線錨點的位置索引（df 第幾根）
      anchor_price 0 線在 anchor_idx 的價格
      slope        每根的斜率（價格/根）
      width        通道寬度 W（fib_ch_100 = 0 線 + dir×W）
    """
    n = len(df)
    if n < pivot_left + pivot_right + 2:
        return None

    lows  = df["low"].values
    highs = df["high"].values
    ema_f = ema(df["close"], trend_fast).values
    ema_s = ema(df["close"], trend_slow).values

    # 末根趨勢方向（往回找第一個有效 EMA）
    last = n - 1
    while last >= 0 and (np.isnan(ema_f[last]) or np.isnan(ema_s[last])):
        last -= 1
    if last < 0:
        return None
    up = ema_f[last] >= ema_s[last]

    pivots = _confirmed_pivots(lows if up else highs, pivot_left, pivot_right,
                               "low" if up else "high")
    # 只取在末根前已確認的
    confirmed = [(j, p) for j, p in pivots if j + pivot_right <= last]
    if len(confirmed) < 2:
        return None

    p1_j, p1_v = confirmed[-2]
    p2_j, p2_v = confirmed[-1]
    if p2_j <= p1_j:
        return None

    slope = (p2_v - p1_v) / (p2_j - p1_j)
    ks = np.arange(p1_j, last + 1)
    baseline = p1_v + slope * (ks - p1_j)
    if up:
        width = float((highs[p1_j:last + 1] - baseline).max())
    else:
        width = float((baseline - lows[p1_j:last + 1]).max())
    if width <= 0:
        return None

    return {
        "dir": 1 if up else -1,
        "anchor_idx": int(p1_j),
        "anchor_price": float(p1_v),
        "slope": float(slope),
        "width": width,
    }


_FIB_FAST_DEFAULT = (8, 13, 21)
_FIB_SLOW_DEFAULT = (34, 55, 89)


def fib_ema_score(
    close: pd.Series,
    fast: tuple = _FIB_FAST_DEFAULT,
    slow: tuple = _FIB_SLOW_DEFAULT,
) -> pd.Series:
    """費波那契 EMA 排列分數（Fibonacci EMA Alignment Score）。

    以 Fibonacci 數列週期（8/13/21 快 vs 34/55/89 慢）計算多頭/空頭排列強度。
    分數 = 快線全部高於慢線的組合數 / 總組合數，回傳 [0, 1]：
      1.0 = 完全多頭排列（快線全在慢線上方）
      0.0 = 完全空頭排列（快線全在慢線下方）
      0.5 附近 = 盤整混沌

    Causal：只用過去 K 線，不 repaint。
    NaN：慢線週期最長一條穩定前均為 NaN。
    """
    fast_emas = [ema(close, p) for p in fast]
    slow_emas = [ema(close, p) for p in slow]

    n_pairs = len(fast) * len(slow)
    score = sum(
        (f > s).astype(float)
        for f in fast_emas
        for s in slow_emas
    ) / n_pairs

    # NaN until the slowest EMA has settled (first non-NaN of slowest)
    slowest = ema(close, max(slow))
    score = score.where(slowest.notna())
    return score


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
