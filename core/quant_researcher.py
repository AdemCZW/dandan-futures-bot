"""量化研究員 /quant-researcher — 設計策略、產生進出場信號。

每個策略吃一根「已收完」的 K 線（含指標）與「目前倉位」，吐出**目標倉位**：
    +1 = 想做多（持有多單）
     0 = 想空手（平掉任何倉位）
    -1 = 想做空（持有空單；僅在 allow_short=True 的策略才會用到）

回測引擎會比對「目前倉位 vs 目標倉位」自動進出場與換邊。
（注意：這是相對舊版的契約變更——舊版 -1 代表「平多」，新版 -1 代表「做空」、
平倉一律用 0。僅做多策略只會回 0/＋1，行為與舊版完全一致。）

指標參數可由 self.params 調整（預設值與舊版相同），
這樣 backtest/optimize.py 才能對它們做參數掃描與 walk-forward。
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from core import signal_engineer as se


class Strategy:
    name = "base"
    defaults: dict = {}
    allow_short = False          # True 的策略才會被回測引擎允許做空
    regime_pref = "any"          # 'trend'（順勢）/ 'range'（均值回歸）/ 'any'（不過濾）

    # regime 閘門共用參數（可被 self.params 覆蓋）
    REGIME_DEFAULTS = {
        "er_period": 14, "er_trend": 0.30,
        "chop_period": 14, "chop_trend": 38.2,
        "adx_period": 14, "adx_trend": 25.0,
        "regime_confirm_bars": 2,
    }

    # 市場結構（訂單流）確認閘門共用參數（可被 self.params 覆蓋）。
    # use_structure 預設 False＝向後相容、不改既有行為；經 walk-forward 驗證有效後才開啟。
    STRUCTURE_DEFAULTS = {
        "use_structure": False,   # 閘門總開關
        "of_smooth": 20,          # 主動買盤佔比的 EMA 平滑根數
        "of_long_min": 0.45,      # 做多需主動買盤佔比 ≥ 此值（不逆賣壓做多）
        "of_short_max": 0.55,     # 做空需主動買盤佔比 ≤ 此值（不逆買盤做空）
    }

    def __init__(self, **params):
        # 使用者傳入的值覆蓋預設值
        self.params = {**self.defaults, **params}

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def signal(self, row: pd.Series, position: int) -> int:
        """回傳目標倉位 +1/0/-1。position 為目前倉位（+1/0/-1）。"""
        raise NotImplementedError

    def _prepare_regime(self, out: pd.DataFrame) -> pd.DataFrame:
        """把 er/chop/adx/regime 欄位算進 DataFrame（regime_pref='any' 則略過）。"""
        if self.regime_pref == "any":
            return out
        p = {**self.REGIME_DEFAULTS, **self.params}
        reg = se.regime(out, er_period=int(p["er_period"]), er_trend=p["er_trend"],
                        chop_period=int(p["chop_period"]), chop_trend=p["chop_trend"],
                        adx_period=int(p["adx_period"]), adx_trend=p["adx_trend"],
                        confirm_bars=int(p["regime_confirm_bars"]))
        out["er"], out["chop"], out["adx"], out["regime"] = (
            reg["er"], reg["chop"], reg["adx"], reg["regime"])
        return out

    def _regime_ok(self, row: pd.Series) -> bool:
        """空手想開新倉時呼叫：regime 與策略偏好相符才放行。

        'any' 一律放行；row 無 regime 欄或為 None/NaN（精簡單元測試列、warmup）也放行——
        真實回測路徑會 dropna()，signal() 只會看到已確認的 'trend'/'range'，閘門才實際生效。
        """
        if self.regime_pref == "any":
            return True
        reg = row.get("regime") if hasattr(row, "get") else None
        if reg is None or (isinstance(reg, float) and math.isnan(reg)):
            return True
        return reg == self.regime_pref

    def _prepare_structure(self, out: pd.DataFrame) -> pd.DataFrame:
        """把訂單流欄位（taker_ratio_s 平滑買盤佔比）算進 DataFrame。

        缺 taker_base（合成資料/舊快取）→ se.taker_buy_ratio 回全 NaN，
        欄位仍加入但全 NaN，_structure_ok 會優雅放行。
        """
        p = {**self.STRUCTURE_DEFAULTS, **self.params}
        out["taker_ratio_s"] = se.taker_buy_ratio(out, smooth=int(p["of_smooth"]))
        return out

    def _structure_ok(self, row: pd.Series, direction: int) -> bool:
        """空手想開新倉時呼叫：訂單流與進場方向不衝突才放行。

        use_structure=False（預設）/ 缺 taker_ratio_s 欄 / 值為 NaN → 一律放行
        （向後相容、優雅退化）。開啟後：做多需買盤佔比 ≥ of_long_min、
        做空需買盤佔比 ≤ of_short_max，避免逆著主動成交流進場。
        """
        p = {**self.STRUCTURE_DEFAULTS, **self.params}
        if not p["use_structure"]:
            return True
        val = row.get("taker_ratio_s") if hasattr(row, "get") else None
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return True
        val = float(val)
        if direction == 1:
            return val >= p["of_long_min"]
        if direction == -1:
            return val <= p["of_short_max"]
        return True


class EMACrossStrategy(Strategy):
    """EMA 快線上穿慢線 + RSI 未過熱 → 進場；快線下穿 → 出場。

    順勢策略（regime_pref='trend'）：只在趨勢盤開倉，盤整盤被 regime 閘門擋下，避免 whipsaw。
    """
    name = "ema_cross"
    # rsi_mid=50 順勢動能確認；rsi_max 放寬到 80（只擋極端追高，不再因鈍化砍掉強趨勢段）；
    # sep_atr_k 交叉緩衝帶：進場要求兩線分離 > sep_atr_k×ATR，濾掉零軸抖動式假交叉。
    defaults = {"fast": 12, "slow": 26, "rsi_period": 14,
                "rsi_mid": 50, "rsi_max": 80, "sep_atr_k": 0.5}
    regime_pref = "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ema_fast"] = se.ema(out["close"], int(self.params["fast"]))
        out["ema_slow"] = se.ema(out["close"], int(self.params["slow"]))
        out["rsi"] = se.rsi(out["close"], int(self.params["rsi_period"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_regime(out)

    def signal(self, row: pd.Series, position: int) -> int:
        if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]) or pd.isna(row["rsi"]):
            return position                       # 資料不足：維持現狀（與其他策略契約一致）
        fast, slow, rsi = float(row["ema_fast"]), float(row["ema_slow"]), float(row["rsi"])
        bull_hold = fast > slow                   # 出場用裸交叉（hysteresis：進場要分離、出場只需翻轉）
        if position == 0:
            atr = row.get("atr") if hasattr(row, "get") else None
            sep = self.params["sep_atr_k"] * float(atr) if (atr is not None and not pd.isna(atr)) else 0.0
            cleared = (fast - slow) > sep         # 交叉緩衝帶：分離足夠才算有效金叉
            momentum_ok = self.params["rsi_mid"] < rsi < self.params["rsi_max"]
            # 空手：有效金叉 + 順勢動能(50<rsi<max) + 趨勢盤 → 目標做多
            if cleared and momentum_ok and self._regime_ok(row):
                return 1
            return 0
        # 持多：續抱直到死叉
        return 1 if bull_hold else 0


class ZScoreRevertStrategy(Strategy):
    """均值回歸：z-score < -entry 超賣 → 買進；回到 0 附近 → 出場。

    對應圖 2 提到的 mean-reversion alpha（但只做多側）。
    """
    name = "zscore_revert"
    defaults = {"window": 50, "entry_z": 2.0, "exit_z": 0.3}
    regime_pref = "range"          # 均值回歸：只在盤整盤開倉

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["zscore"] = se.zscore(out["close"], int(self.params["window"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_regime(out)

    def signal(self, row: pd.Series, position: int) -> int:
        z = row["zscore"]
        if pd.isna(z):
            return position                       # 資料不足：維持現狀
        if position == 0:
            # 空手：超賣且處於盤整盤 → 目標做多
            return 1 if (z < -self.params["entry_z"] and self._regime_ok(row)) else 0
        # 持多：回到均值附近（z 回到 -exit_z 之上）→ 平倉
        return 0 if z > -self.params["exit_z"] else 1


class ZScoreLongShortStrategy(Strategy):
    """均值回歸（多空雙向）：超賣做多、超買做空，回到均值附近平倉。

    與 zscore_revert 同套指標，差別在多了空方。allow_short=True，
    只有支援做空的回測引擎會真的開空單；run_live 在現貨上會安全忽略空方。
    """
    name = "zscore_ls"
    defaults = {"window": 50, "entry_z": 2.0, "exit_z": 0.3}
    allow_short = True
    regime_pref = "range"          # 均值回歸：只在盤整盤開倉，強趨勢不逆勢接刀

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["zscore"] = se.zscore(out["close"], int(self.params["window"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_regime(out)

    def signal(self, row: pd.Series, position: int) -> int:
        z = row["zscore"]
        if pd.isna(z):
            return position
        entry, exit_ = self.params["entry_z"], self.params["exit_z"]
        if position == 0:
            if not self._regime_ok(row):
                return 0                          # 非盤整盤：不開任何新倉（堵住逆勢接刀破口）
            if z < -entry:
                return 1                          # 超賣 → 做多
            if z > entry:
                return -1                         # 超買 → 做空
            return 0
        if position == 1:
            return 0 if z > -exit_ else 1          # 多單回到均值附近 → 平倉
        # position == -1：空單回到均值附近 → 平倉（回補）
        return 0 if z < exit_ else -1


class FibRetracementStrategy(Strategy):
    """斐波那契回調均值回歸（多空雙向）。

    以近期波段高低點計算 fib_pos（0=在低點、1=在高點）：
    - fib_pos < 0.382 且 RSI < 55 → 在黃金支撐區，做多
    - fib_pos > 0.618 且 RSI > 45 → 在黃金阻力區，做空
    - 持多且 fib_pos > 0.55 → 漲到中線以上，獲利平多
    - 持空且 fib_pos < 0.45 → 跌到中線以下，獲利平空
    """
    name = "fib_retracement"
    # pivot_left/right：用已確認 swing 擺動點界定波段（取代固定 lookback 盒子極值）。
    # ema_trend_period：長線趨勢過濾，把逆勢均回改為順勢回調（上升趨勢買支撐、下降趨勢空阻力）。
    defaults = {"lookback": 50, "rsi_period": 14,
                "pivot_left": 3, "pivot_right": 2, "ema_trend_period": 200,
                "er_trend": 0.25, "regime_confirm_bars": 1}
    allow_short = True
    regime_pref = "range"          # 回調進出場：只在盤整盤開倉

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.fib_retracement(df, pivot_left=int(self.params["pivot_left"]),
                                 pivot_right=int(self.params["pivot_right"]))
        out["rsi"] = se.rsi(out["close"], int(self.params["rsi_period"]))
        out["atr"] = se.atr(out, 14)
        out["ema_trend"] = se.ema(out["close"], int(self.params["ema_trend_period"]))
        return self._prepare_structure(self._prepare_regime(out))

    def signal(self, row: pd.Series, position: int) -> int:
        fib_pos = row.get("fib_pos") if hasattr(row, "get") else row["fib_pos"]
        rsi = row.get("rsi") if hasattr(row, "get") else row["rsi"]
        if fib_pos is None or rsi is None:
            return position
        try:
            if math.isnan(float(fib_pos)) or math.isnan(float(rsi)):
                return position
        except (TypeError, ValueError):
            return position

        fib_pos = float(fib_pos)
        rsi = float(rsi)

        if position == 1:                   # 持多
            return 0 if fib_pos > 0.55 else 1
        if position == -1:                  # 持空
            return 0 if fib_pos < 0.45 else -1
        # 空手：找進場訊號（須處於盤整盤）
        if not self._regime_ok(row):
            return 0
        # 長線趨勢方向（順勢回調）：上升趨勢只在支撐區做多、下降趨勢只在阻力區做空。
        # 趨勢未知（精簡單元測試 row 無 close/ema_trend）→ 不過濾，維持向後相容。
        close = row.get("close") if hasattr(row, "get") else None
        ema_trend = row.get("ema_trend") if hasattr(row, "get") else None
        trend_known = (close is not None and ema_trend is not None
                       and not pd.isna(close) and not pd.isna(ema_trend))
        uptrend = (not trend_known) or float(close) > float(ema_trend)
        downtrend = (not trend_known) or float(close) < float(ema_trend)
        if fib_pos < 0.382 and rsi < 55 and uptrend and self._structure_ok(row, 1):
            return 1                        # 上升趨勢 + 黃金支撐區 + RSI 未過熱 + 訂單流不逆 → 順勢買回調
        if fib_pos > 0.618 and rsi < 50 and downtrend and self._structure_ok(row, -1):
            return -1                       # 下降趨勢 + 黃金阻力區 + RSI 動能轉弱 + 訂單流不逆 → 順勢空反彈
        return 0


class SupertrendStrategy(Strategy):
    """Supertrend ATR 趨勢跟蹤（多空雙向）。

    跟隨 st_dir：轉多做多、轉空做空。趨勢策略本身即定義趨勢，不另外套 regime 閘門
    （regime_pref='any'）。訂單流閘門只擋「新開倉/翻倉」，既有同向倉不強制平出。
    這是 BTC 上文獻最常引用的穩健趨勢策略（Supertrend ATR=10、mult=3 為經典值）。

    HTF（高時框）趨勢過濾器（use_htf_filter=False 預設關閉）：
    開啟後，只在 close > ema_trend 時做多，close < ema_trend 時做空，
    避免逆大趨勢入場。既有倉位不被此閘門強制平出。
    靈感來自 QuantPedia 研究：日線趨勢確認使 1h MACD Sharpe 從 0.33 → 1.07，
    以及 AdaptiveTrend (arXiv 2602.11708) 在 H4 的 OOS Sharpe 2.08。
    """
    name = "supertrend"
    defaults = {"period": 10, "multiplier": 3.0,
                "use_htf_filter": False, "htf_ema_period": 200}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        st = se.supertrend(out, period=int(self.params["period"]),
                           multiplier=float(self.params["multiplier"]))
        out["st_dir"] = st["st_dir"]
        out["supertrend"] = st["supertrend"]
        out["atr"] = se.atr(out, 14)
        out["ema_trend"] = se.ema(out["close"], int(self.params["htf_ema_period"]))
        return self._prepare_structure(out)

    def _htf_ok(self, row: pd.Series, direction: int) -> bool:
        """HTF 趨勢閘門：close 須在 ema_trend 正確一側才開新倉。

        use_htf_filter=False / ema_trend 為 NaN（暖機）→ 一律放行。
        只在新開倉時呼叫，不強制平出既有倉位。
        """
        if not self.params.get("use_htf_filter", False):
            return True
        ema_t = row.get("ema_trend") if hasattr(row, "get") else None
        close = row.get("close") if hasattr(row, "get") else None
        if ema_t is None or close is None:
            return True
        if pd.isna(float(ema_t)) or pd.isna(float(close)):
            return True
        if direction == 1:
            return float(close) > float(ema_t)
        if direction == -1:
            return float(close) < float(ema_t)
        return True

    def signal(self, row: pd.Series, position: int) -> int:
        st_dir = row.get("st_dir") if hasattr(row, "get") else row["st_dir"]
        if st_dir is None or (isinstance(st_dir, float) and math.isnan(st_dir)):
            return position                       # 方向未定（warmup）→ 維持現狀
        target = 1 if float(st_dir) > 0 else -1
        if target == position:
            return position                       # 同向 → 續抱（不被閘門踢出）
        # 新開倉/翻倉：訂單流 + HTF 趨勢兩關都通過才進場，否則退回空手
        if self._structure_ok(row, target) and self._htf_ok(row, target):
            return target
        return 0


class DonchianBreakoutStrategy(Strategy):
    """Donchian 通道突破（海龜系統，多空雙向）。

    收盤突破過去 entry_period 根高點 → 做多；跌破低點 → 做空。
    出場用較短的 exit_period 通道（多單跌破 exit_long、空單突破 exit_short → 平倉）。
    經典海龜 System 1：entry=20 / exit=10。趨勢策略，regime_pref='any'。
    訂單流閘門只擋「新開倉」，既有倉由出場通道決定何時平。
    """
    name = "donchian"
    defaults = {"entry_period": 20, "exit_period": 10}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        dc = se.donchian(out, entry_period=int(self.params["entry_period"]),
                         exit_period=int(self.params["exit_period"]))
        for c in ("dc_upper", "dc_lower", "dc_exit_long", "dc_exit_short"):
            out[c] = dc[c]
        out["atr"] = se.atr(out, 14)
        return self._prepare_structure(out)

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        close = g("close")
        upper, lower = g("dc_upper"), g("dc_lower")
        exit_long, exit_short = g("dc_exit_long"), g("dc_exit_short")
        if close is None or any(v is None or (isinstance(v, float) and math.isnan(v))
                                for v in (upper, lower)):
            return position                       # 通道未暖機 → 維持現狀
        close = float(close)

        if position == 1:                         # 持多：跌破出場下軌才平
            if exit_long is not None and not pd.isna(exit_long) and close < float(exit_long):
                return 0
            return 1
        if position == -1:                        # 持空：突破出場上軌才平
            if exit_short is not None and not pd.isna(exit_short) and close > float(exit_short):
                return 0
            return -1
        # 空手：突破進場通道 → 開倉（訂單流不可逆向）
        if close > float(upper) and self._structure_ok(row, 1):
            return 1
        if close < float(lower) and self._structure_ok(row, -1):
            return -1
        return 0


class OrderFlowMomentumStrategy(Strategy):
    """CVD 訂單流動量（多空雙向，短線用）。

    主訊號＝CVD（累積主動買賣量差）的快/慢 EMA 交叉（MACD-on-CVD）：
    買盤動量轉強(of_fast>of_slow)做多、賣盤動量轉強做空。這是「以訂單流本身為
    主訊號」而非價格 TA，理論上是短週期微結構訊號。缺 taker_base（合成資料）→
    of_fast/of_slow 為 NaN → 維持現狀（優雅退化）。
    """
    name = "of_momentum"
    defaults = {"cvd_fast": 10, "cvd_slow": 30}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        cvd_s = se.cvd(out)                                # 缺 taker_base → 全 NaN
        out["cvd"] = cvd_s
        out["of_fast"] = se.ema(cvd_s, int(self.params["cvd_fast"]))
        out["of_slow"] = se.ema(cvd_s, int(self.params["cvd_slow"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_structure(out)

    def signal(self, row: pd.Series, position: int) -> int:
        of_fast = row.get("of_fast") if hasattr(row, "get") else row["of_fast"]
        of_slow = row.get("of_slow") if hasattr(row, "get") else row["of_slow"]
        for v in (of_fast, of_slow):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return position                           # 訂單流未暖機 → 維持現狀
        if float(of_fast) > float(of_slow):
            return 1
        if float(of_fast) < float(of_slow):
            return -1
        return position


def _num(v, default=None):
    """把 row 取出的值轉 float；None/NaN → default。給短線策略的 NaN 防護用。"""
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


class VwapBandReversionStrategy(Strategy):
    """滾動 VWAP 偏離 + 影線拒絕的均值回歸（多空雙向，盤整盤）。

    與 zscore 系列的差異（避免淪為換皮）：fair value 用【成交量加權】的滾動 VWAP，
    且進場必須在觸帶當根出現【拒絕影線】（下影線買、上影線賣）——量價結構雙確認，
    而非單純統計極端。研究+對抗式驗證後選為均值回歸的一條獨立 edge。
    """
    name = "vwap_band_reversion"
    defaults = {"vwap_window": 50, "k": 2.2, "exit_z": 0.4, "wick_frac": 0.5,
                "use_structure": True, "of_long_min": 0.45, "of_short_max": 0.55}
    allow_short = True
    regime_pref = "range"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        w = int(self.params["vwap_window"])
        out["vwap_roll"] = se.rolling_vwap(out, w)
        dist = out["close"] - out["vwap_roll"]
        out["band_sd"] = dist.rolling(w).std(ddof=0)
        out["vwdist_z"] = dist / out["band_sd"].replace(0, np.nan)
        rng = (out["high"] - out["low"]).replace(0, np.nan)
        body_low = out[["open", "close"]].min(axis=1)
        body_high = out[["open", "close"]].max(axis=1)
        out["lower_wick_frac"] = (body_low - out["low"]) / rng
        out["upper_wick_frac"] = (out["high"] - body_high) / rng
        out["atr"] = se.atr(out, 14)
        return self._prepare_structure(self._prepare_regime(out))

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        z, vwap, close = _num(g("vwdist_z")), _num(g("vwap_roll")), _num(g("close"))
        if z is None or vwap is None or close is None:
            return position
        exit_z = self.params["exit_z"]
        if position == 1:
            return 0 if (close >= vwap or z >= -exit_z) else 1
        if position == -1:
            return 0 if (close <= vwap or z <= exit_z) else -1
        if not self._regime_ok(row):
            return 0
        k, wf = self.params["k"], self.params["wick_frac"]
        lw, uw = _num(g("lower_wick_frac"), 0.0), _num(g("upper_wick_frac"), 0.0)
        if z <= -k and lw >= wf and self._structure_ok(row, 1):
            return 1
        if z >= k and uw >= wf and self._structure_ok(row, -1):
            return -1
        return 0


class HeikinAshiMomoStrategy(Strategy):
    """Heikin-Ashi 顏色連續 + 強實體的順勢續抱（多空雙向）。

    用 HA K 棒的「顏色連續根數 + 影線幾何」作動量訊號——這是既有策略都沒用過的
    K 棒建構式動量（有別於 ema_cross 的均線交叉、supertrend 的 ATR 通道）。
    EMA(200) 為長線方向閘門；平滑訂單流只擋新開倉。從空手才開新倉（不直接翻倉）。
    """
    name = "heikin_ashi_momo"
    defaults = {"ema_len": 200, "wick_frac": 0.15, "min_run": 2,
                "use_structure": True, "of_smooth": 20,
                "of_long_min": 0.45, "of_short_max": 0.55}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        ha = se.heikin_ashi(out)
        for c in ("ha_open", "ha_close", "ha_high", "ha_low"):
            out[c] = ha[c]
        color = np.sign(ha["ha_close"] - ha["ha_open"])
        out["ha_color"] = color
        rng = (ha["ha_high"] - ha["ha_low"]).replace(0, np.nan)
        body_low = pd.concat([ha["ha_open"], ha["ha_close"]], axis=1).min(axis=1)
        body_high = pd.concat([ha["ha_open"], ha["ha_close"]], axis=1).max(axis=1)
        out["ha_lower_wick_frac"] = (body_low - ha["ha_low"]) / rng
        out["ha_upper_wick_frac"] = (ha["ha_high"] - body_high) / rng
        col = color.to_numpy()
        run = np.ones(len(col))
        for i in range(1, len(col)):
            run[i] = run[i - 1] + 1 if (col[i] == col[i - 1] and col[i] != 0) else 1
        out["ha_same_color_run"] = run
        out["ema_trend"] = se.ema(out["close"], int(self.params["ema_len"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_structure(out)

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        color, close, ema_t = _num(g("ha_color")), _num(g("close")), _num(g("ema_trend"))
        if color is None or close is None or ema_t is None:
            return position
        if position == 1:
            return 0 if (color <= 0 or close < ema_t) else 1
        if position == -1:
            return 0 if (color >= 0 or close > ema_t) else -1
        wf, min_run = self.params["wick_frac"], self.params["min_run"]
        run = _num(g("ha_same_color_run"))
        if run is None:
            return 0
        lw, uw = _num(g("ha_lower_wick_frac")), _num(g("ha_upper_wick_frac"))
        if (color > 0 and lw is not None and lw <= wf and run >= min_run
                and close > ema_t and self._structure_ok(row, 1)):
            return 1
        if (color < 0 and uw is not None and uw <= wf and run >= min_run
                and close < ema_t and self._structure_ok(row, -1)):
            return -1
        return 0


class MacdScalpStrategy(Strategy):
    """價格 MACD 零軸 + ADX 趨勢閘門的動量策略（多空雙向，趨勢盤）。

    觸發＝MACD 線/訊號交叉 + 零軸過濾 + 柱狀圖上升 + EMA(50) 方向 + ADX 強度 + regime。
    與 of_momentum（MACD-on-CVD 吃訂單流）不同：這是吃【價格】的 MACD。
    出場＝柱狀圖連兩根衰竭或反向交叉。從空手才開新倉。
    """
    name = "macd_scalp"
    defaults = {"fast": 12, "slow": 26, "sig": 9, "ema_trend_period": 50,
                "adx_min": 18, "use_structure": True,
                "of_long_min": 0.45, "of_short_max": 0.55}
    allow_short = True
    regime_pref = "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        m = se.macd(out["close"], int(self.params["fast"]), int(self.params["slow"]),
                    int(self.params["sig"]))
        out["macd_line"], out["macd_signal"], out["macd_hist"] = (
            m["macd_line"], m["macd_signal"], m["macd_hist"])
        out["macd_hist_prev"] = out["macd_hist"].shift(1)
        out["macd_hist_prev2"] = out["macd_hist"].shift(2)
        line, sig = m["macd_line"], m["macd_signal"]
        out["cross_up"] = (line > sig) & (line.shift(1) <= sig.shift(1))
        out["cross_dn"] = (line < sig) & (line.shift(1) >= sig.shift(1))
        out["ema_trend"] = se.ema(out["close"], int(self.params["ema_trend_period"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_structure(self._prepare_regime(out))

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        line, hist = _num(g("macd_line")), _num(g("macd_hist"))
        if line is None or _num(g("macd_signal")) is None or hist is None:
            return position
        hp = _num(g("macd_hist_prev"), hist)
        hp2 = _num(g("macd_hist_prev2"), hp)
        cross_up, cross_dn = bool(g("cross_up")), bool(g("cross_dn"))
        if position == 1:
            return 0 if ((hist < hp and hp < hp2) or cross_dn) else 1
        if position == -1:
            return 0 if ((hist > hp and hp > hp2) or cross_up) else -1
        if not self._regime_ok(row):
            return 0
        adx = _num(g("adx"), 0.0)
        close, ema_t = _num(g("close")), _num(g("ema_trend"))
        trend_up = (close is None or ema_t is None) or close > ema_t
        trend_dn = (close is None or ema_t is None) or close < ema_t
        adx_min = self.params["adx_min"]
        if cross_up and line > 0 and hist > hp and trend_up and adx >= adx_min and self._structure_ok(row, 1):
            return 1
        if cross_dn and line < 0 and hist < hp and trend_dn and adx >= adx_min and self._structure_ok(row, -1):
            return -1
        return 0


class BollingerSqueezeStrategy(Strategy):
    """布林帶寬百分位壓縮 → 波動突破（多空雙向）。

    squeeze＝bandwidth 落在過去 squeeze_lookback 根的低百分位（波動壓縮）；
    前一根 squeeze 後當根收盤突破上/下軌 + ADX 確認 → 順勢突破進場。
    與 donchian（價格通道突破）不同：這裡用【波動壓縮】百分位作突破前置條件、%B 作位置。
    出場＝跌回中軌（突破失敗）。從空手才開新倉。
    """
    name = "bb_squeeze_breakout"
    defaults = {"bb_n": 20, "mult": 2.0, "squeeze_lookback": 100, "squeeze_pct": 0.20,
                "adx_min": 20, "adx_period": 14, "use_structure": True,
                "of_long_min": 0.45, "of_short_max": 0.55}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        bb = se.bollinger(out["close"], int(self.params["bb_n"]), float(self.params["mult"]))
        for c in ("bb_mid", "bb_upper", "bb_lower", "bandwidth", "pct_b"):
            out[c] = bb[c]
        lb, q = int(self.params["squeeze_lookback"]), float(self.params["squeeze_pct"])
        thresh = out["bandwidth"].rolling(lb).quantile(q)
        squeeze = out["bandwidth"] <= thresh
        out["squeeze"] = squeeze
        out["squeeze_prev"] = squeeze.shift(1).fillna(False)
        out["adx"] = se.adx(out, int(self.params["adx_period"]))["adx"]
        out["atr"] = se.atr(out, 14)
        return self._prepare_structure(out)

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        close, mid = _num(g("close")), _num(g("bb_mid"))
        if close is None or mid is None:
            return position
        if position == 1:
            pb = _num(g("pct_b"), 1.0)
            return 0 if (close < mid or pb < 0.5) else 1
        if position == -1:
            pb = _num(g("pct_b"), 0.0)
            return 0 if (close > mid or pb > 0.5) else -1
        if not bool(g("squeeze_prev")):
            return 0
        if _num(g("adx"), 0.0) < self.params["adx_min"]:
            return 0
        upper = _num(g("bb_upper"), float("inf"))
        lower = _num(g("bb_lower"), float("-inf"))
        if close > upper and self._structure_ok(row, 1):
            return 1
        if close < lower and self._structure_ok(row, -1):
            return -1
        return 0


class Rsi2ConnorsStrategy(Strategy):
    """Larry Connors RSI(2) 極端 + EMA(200) 方向閘門（多空雙向，順勢回調）。

    RSI(2)<rsi_lo 且 close>EMA200 → 上升趨勢中買超賣回調；
    RSI(2)>rsi_hi 且 close<EMA200 → 下降趨勢中空超買反彈。出場＝close 回到 SMA(5)。
    與 zscore（滾動 z 帶）/fib（pivot+RSI14）不同：快速振盪器的趨勢內回調，不是統計極端的逆勢淡化。
    """
    name = "rsi2_connors"
    defaults = {"rsi_lo": 5, "rsi_hi": 95, "trend_ema_period": 200, "sma_exit_period": 5}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["rsi2"] = se.rsi(out["close"], 2)
        out["trend_ema"] = se.ema(out["close"], int(self.params["trend_ema_period"]))
        out["sma_exit"] = out["close"].rolling(int(self.params["sma_exit_period"])).mean()
        out["atr"] = se.atr(out, 14)
        return out

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        rsi2, close, trend = _num(g("rsi2")), _num(g("close")), _num(g("trend_ema"))
        if rsi2 is None or close is None or trend is None:
            return position
        sma = _num(g("sma_exit"))
        if position == 1:
            return 1 if sma is None else (0 if close > sma else 1)
        if position == -1:
            return -1 if sma is None else (0 if close < sma else -1)
        if rsi2 < self.params["rsi_lo"] and close > trend:
            return 1
        if rsi2 > self.params["rsi_hi"] and close < trend:
            return -1
        return 0


class SmcStructureStrategy(Strategy):
    """Smart Money Concept — Break of Structure + Fair Value Gap（多空雙向）。

    看漲 BOS（close 突破最近已確認 swing high）且有看漲 FVG → 做多；
    看跌 BOS（close 跌破最近已確認 swing low）且有看跌 FVG → 做空；
    反向 BOS 出現時平倉。
    regime_pref='trend'：只在趨勢盤操作，避免盤整盤假突破。
    """
    name = "smc_structure"
    defaults = {"pivot_left": 5, "pivot_right": 3, "atr_period": 14,
                "require_fvg": False, "regime_confirm_bars": 1}
    allow_short = True
    regime_pref = "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out = se.smc_levels(out, pivot_left=int(self.params["pivot_left"]),
                            pivot_right=int(self.params["pivot_right"]))
        out["atr"] = se.atr(out, int(self.params["atr_period"]))
        return self._prepare_regime(out)

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        bos_bull = _num(g("bos_bull")) or 0.0
        bos_bear = _num(g("bos_bear")) or 0.0
        fvg_bull = _num(g("fvg_bull")) or 0.0
        fvg_bear = _num(g("fvg_bear")) or 0.0
        require_fvg = bool(self.params.get("require_fvg", True))

        if position == 1:
            return 0 if bos_bear else 1
        if position == -1:
            return 0 if bos_bull else -1

        if not self._regime_ok(row):
            return 0

        if bos_bull and (fvg_bull or not require_fvg):
            return 1
        if bos_bear and (fvg_bear or not require_fvg):
            return -1
        return 0


class FibChannelStrategy(Strategy):
    """費波那契斜向通道順勢策略（多空雙向，趨勢自適應）。

    通道由 signal_engineer.fib_channel_levels 計算：
      - 上升趨勢（fib_ch_dir=+1）：0 線沿 swing low（支撐），目標在上。
      - 下降趨勢（fib_ch_dir=−1）：0 線沿 swing high（阻力），目標在下。
    fib_ch_pos：0=趨勢原點（進場側）、1=對側（目標側），與方向無關。

    進場：回調到原點（fib_ch_pos < entry_zone）順勢進場，方向 = fib_ch_dir。
    出場：到達目標側（pos > exit_zone）或跌破原點（pos < −break_buffer，趨勢失效）。
    """
    name = "fib_channel"
    defaults = {"pivot_left": 5, "pivot_right": 3,
                "entry_zone": 0.30, "exit_zone": 0.80, "break_buffer": 0.10,
                "regime_confirm_bars": 1}
    allow_short = True
    regime_pref = "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.fib_channel_levels(df.copy(),
                                    int(self.params["pivot_left"]),
                                    int(self.params["pivot_right"]))
        out["atr"] = se.atr(out, 14)
        return self._prepare_regime(out)

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        pos_in_ch = _num(g("fib_ch_pos"))
        ch_dir    = _num(g("fib_ch_dir"))

        entry_z = float(self.params.get("entry_zone", 0.30))
        exit_z  = float(self.params.get("exit_zone",  0.80))
        brk     = float(self.params.get("break_buffer", 0.10))

        # 持倉中：到達目標側或跌破原點 → 平倉（pos 語意與方向無關，多空共用）
        if position != 0:
            if pos_in_ch is None:
                return position
            if pos_in_ch > exit_z or pos_in_ch < -brk:
                return 0
            return position

        # 空手進場：需有效通道 + regime 確認 + 回調到原點區
        if pos_in_ch is None or ch_dir is None or ch_dir == 0:
            return 0
        if not self._regime_ok(row):
            return 0
        if pos_in_ch < entry_z:
            return int(ch_dir)          # 順勢方向進場（+1 多 / −1 空）
        return 0


STRATEGIES = {
    EMACrossStrategy.name: EMACrossStrategy,
    ZScoreRevertStrategy.name: ZScoreRevertStrategy,
    ZScoreLongShortStrategy.name: ZScoreLongShortStrategy,
    SupertrendStrategy.name: SupertrendStrategy,
    DonchianBreakoutStrategy.name: DonchianBreakoutStrategy,
    OrderFlowMomentumStrategy.name: OrderFlowMomentumStrategy,
    FibRetracementStrategy.name: FibRetracementStrategy,
    VwapBandReversionStrategy.name: VwapBandReversionStrategy,
    HeikinAshiMomoStrategy.name: HeikinAshiMomoStrategy,
    MacdScalpStrategy.name: MacdScalpStrategy,
    BollingerSqueezeStrategy.name: BollingerSqueezeStrategy,
    Rsi2ConnorsStrategy.name: Rsi2ConnorsStrategy,
    SmcStructureStrategy.name: SmcStructureStrategy,
    FibChannelStrategy.name: FibChannelStrategy,
}


class ConsensusStrategy:
    """多策略共識過濾：N 個子策略投票，≥ min_agree 票同方向才進場。

    prepare() 依序執行各子策略並合併欄位（後者覆蓋同名欄位）。
    signal()  對各子策略取票：long_votes / short_votes，達門檻才回傳 1/-1，
              否則回傳 0（不進場，但已持倉方向繼續持有由呼叫端決定）。

    BOT_PARAMS 範例：
      {"strategies": ["rsi2_connors", "fib_channel", "smc_structure"], "min_agree": 2}
    """
    name = "consensus"

    def __init__(self, strategies=None, min_agree: int = 2, **_):
        if strategies is None:
            strategies = []
        # 接受 strategy 物件清單或名稱字串清單
        self._subs: list = []
        for s in strategies:
            if isinstance(s, str):
                self._subs.append(build_strategy(s))
            else:
                self._subs.append(s)
        self.min_agree = min_agree

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        result = df.copy()
        for sub in self._subs:
            prepared = sub.prepare(df)
            for col in prepared.columns:
                if col not in result.columns or col in prepared.columns:
                    result[col] = prepared[col]
        return result

    def signal(self, row: pd.Series, position: int) -> int:
        long_v = short_v = 0
        for sub in self._subs:
            v = sub.signal(row, position)
            if v == 1:
                long_v += 1
            elif v == -1:
                short_v += 1
        if long_v >= self.min_agree:
            return 1
        if short_v >= self.min_agree:
            return -1
        return 0


def build_strategy(name: str, **params) -> Strategy:
    if name == "consensus":
        return ConsensusStrategy(**params)
    if name not in STRATEGIES:
        raise ValueError(f"未知策略 {name}，可用：{list(STRATEGIES)} + consensus")
    return STRATEGIES[name](**params)
