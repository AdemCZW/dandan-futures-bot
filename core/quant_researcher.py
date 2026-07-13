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

    # 策略「隱藏」的最長回看週期（不在 params 裡、寫死在指標中的，如 fib_ema 的 89 慢線）。
    # 子類覆寫此值，warmup_bars() 才算得準。0＝沒有額外隱藏回看。
    max_hidden_lookback = 0

    # warmup_bars() 掃 params 時，哪些 key 視為「回看週期」。
    _LOOKBACK_KEYS = ("ema", "period", "window", "slow", "trend", "lookback", "smooth")

    # OPT-16 CVD/價格背離竭盡過濾共用參數（use_cvd_filter 預設 False＝關，向後相容）。
    CVD_DEFAULTS = {"use_cvd_filter": False, "cvd_window": 14}

    def __init__(self, **params):
        # 使用者傳入的值覆蓋預設值
        self.params = {**self.defaults, **params}

    def warmup_bars(self, mult: int = 4, floor: int = 200, cap: int = 1500) -> int:
        """估算策略指標穩定所需的最少 K 棒數＝最長回看週期 × mult，夾在 [floor, cap]。

        只抓 200 根會讓 200EMA（trend_pullback）暖機嚴重不足——ewm 要 ~4-5× 週期才穩
        （OPT-03）。掃 self.params 中像「週期」的數值 + 類別宣告的 max_hidden_lookback +
        regime 閘門回看，取最大乘 mult。實盤 fetch 根數用 max(200, warmup_bars())。
        """
        periods = [float(self.max_hidden_lookback)]
        for k, v in self.params.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) \
                    and any(t in k for t in self._LOOKBACK_KEYS):
                periods.append(float(v))
        if self.regime_pref != "any":
            periods.append(float(self.REGIME_DEFAULTS.get("adx_period", 14)))
        real = [p for p in periods if p and p > 0]
        if not real:
            return floor                       # 偵測不到回看週期 → 直接回 floor，不放大
        return int(min(max(max(real) * mult, floor), cap))

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

    def _prepare_cvd(self, out: pd.DataFrame) -> pd.DataFrame:
        """把 cvd_div（CVD/價格背離 ∈ {-1,0,1}）算進 DataFrame（OPT-16）。

        缺 taker_base（合成/舊快取）→ 全 0、_cvd_ok 一律放行。use_cvd_filter 關時仍算欄位
        但不影響進場（零成本、可供前端/分析觀察）。
        """
        p = {**self.CVD_DEFAULTS, **self.params}
        out["cvd_div"] = se.cvd_price_divergence(out, window=int(p["cvd_window"]))
        return out

    def _cvd_ok(self, row: pd.Series, direction: int) -> bool:
        """空手想開新倉時呼叫：訂單流背離與進場方向衝突時擋下（竭盡過濾，OPT-16）。

        use_cvd_filter=False（預設）/ 缺 cvd_div 欄 / 值為 NaN → 一律放行（向後相容）。
        做多遇頂背離(cvd_div<0，買盤竭盡)→擋；做空遇底背離(cvd_div>0，賣盤吸收)→擋。
        """
        p = {**self.CVD_DEFAULTS, **self.params}
        if not p["use_cvd_filter"]:
            return True
        val = row.get("cvd_div") if hasattr(row, "get") else None
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return True
        val = float(val)
        if direction == 1 and val < 0:
            return False
        if direction == -1 and val > 0:
            return False
        return True

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
                "require_fvg": False, "regime_confirm_bars": 1,
                "ema_fast_period": 20, "ema_slow_period": 50,
                "use_ema_filter": True,
                # 多週期共振（2026-07-05）：開啟後 BOS 進場方向須與日線 MA 排列一致。
                # 預設關 → 行為與已驗證的線上籃子逐位元一致。
                "use_htf_filter": False, "htf_fast": 20, "htf_slow": 60}
    allow_short = True
    regime_pref = "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out = se.smc_levels(out, pivot_left=int(self.params["pivot_left"]),
                            pivot_right=int(self.params["pivot_right"]))
        out["atr"] = se.atr(out, int(self.params["atr_period"]))
        out["ema_fast"] = se.ema(out["close"], int(self.params["ema_fast_period"]))
        out["ema_slow"] = se.ema(out["close"], int(self.params["ema_slow_period"]))
        if bool(self.params.get("use_htf_filter", False)):
            out["htf_trend"] = se.htf_trend(out, rule="1D",
                                            fast=int(self.params["htf_fast"]),
                                            slow=int(self.params["htf_slow"]))
        return self._prepare_regime(out)

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        bos_bull = _num(g("bos_bull")) or 0.0
        bos_bear = _num(g("bos_bear")) or 0.0
        fvg_bull = _num(g("fvg_bull")) or 0.0
        fvg_bear = _num(g("fvg_bear")) or 0.0
        require_fvg = bool(self.params.get("require_fvg", True))
        ema_fast = _num(g("ema_fast"))
        ema_slow = _num(g("ema_slow"))

        if position == 1:
            return 0 if bos_bear else 1
        if position == -1:
            return 0 if bos_bull else -1

        if not self._regime_ok(row):
            return 0

        # use_ema_filter=False → 跳過方向過濾，回到純 BOS 進場（供 A/B 驗證）
        use_ema = bool(self.params.get("use_ema_filter", True))
        ema_bullish = (not use_ema) or (ema_fast is not None and ema_slow is not None and ema_fast > ema_slow)
        ema_bearish = (not use_ema) or (ema_fast is not None and ema_slow is not None and ema_fast < ema_slow)

        # 多週期共振（只擋新進場；上方出場邏輯不經過這裡）
        htf_ok_bull = htf_ok_bear = True
        if bool(self.params.get("use_htf_filter", False)):
            htf = _num(g("htf_trend"))
            htf_ok_bull = (htf is not None and int(htf) == 1)
            htf_ok_bear = (htf is not None and int(htf) == -1)

        if bos_bull and ema_bullish and htf_ok_bull and (fvg_bull or not require_fvg):
            return 1
        if bos_bear and ema_bearish and htf_ok_bear and (fvg_bear or not require_fvg):
            return -1
        return 0


class FibChannelStrategy(Strategy):
    """費波那契斜向通道策略（多空雙向，支援兩種模式）。

    通道由 signal_engineer.fib_channel_levels 計算：
      fib_ch_pos：0=趨勢原點（上升→底、下降→頂），1=對側目標，與方向無關。
      fib_ch_dir：+1 上升 / −1 下降。

    mode="trend"（預設，新版）：
      進場：回調到原點（pos < entry_zone），方向 = fib_ch_dir（順勢）。
      出場：到達目標側（pos > exit_zone）或跌破原點（pos < −break_buffer）。

    mode="reversion"（舊版，均值回歸）：
      進場：pos < entry_zone → 順 ch_dir；pos > 1−entry_zone → 逆 ch_dir（通道對面）。
      出場：多單到達目標側（pos > exit_zone）或跌出通道（pos < 0）；
            空單到達底部（pos < 1−exit_zone）或突破通道（pos > 1）。
    """
    name = "fib_channel"
    defaults = {"pivot_left": 5, "pivot_right": 3,
                "entry_zone": 0.30, "exit_zone": 0.80, "break_buffer": 0.10,
                "regime_confirm_bars": 1, "min_channel_width_atr": 0.0,
                "volume_spike_ratio": 0.0, "momentum_window": 3,
                "momentum_max_pct": 0.0,
                "mode": "trend"}
    allow_short = True
    regime_pref = "trend"

    def __init__(self, **params):
        super().__init__(**params)
        # regime 偏好由 mode 決定：順勢只在 trend 盤進場，均值回歸只在 range 盤進場
        # （reversion 在趨勢盤逆勢接刀正是虧損主因，故 range 盤才放行）。
        self.regime_pref = "range" if self.params.get("mode") == "reversion" else "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.fib_channel_levels(df.copy(),
                                    int(self.params["pivot_left"]),
                                    int(self.params["pivot_right"]))
        out["atr"] = se.atr(out, 14)
        # vol_ratio = 當根成交量 / 10根均量，供進場時暴量過濾使用
        vol_avg = out["volume"].rolling(10, min_periods=1).mean()
        out["vol_ratio"] = out["volume"] / vol_avg.replace(0, float("nan"))
        # mom_pct = 過去 N 根 K 棒的絕對漲跌幅 (%)，供動能閘門使用
        win = max(1, int(self.params.get("momentum_window", 3)))
        out["mom_pct"] = out["close"].pct_change(win).abs() * 100
        out = self._prepare_regime(out)
        # 訂單流閘門欄位只在啟用時才加（避免關閉時多一欄全 NaN 被 dropna 清空整段，
        # 例如放進 consensus 或合成資料無 taker_base 時）。
        if {**self.STRUCTURE_DEFAULTS, **self.params}["use_structure"]:
            out = self._prepare_structure(out)
            if out["taker_ratio_s"].isna().all():        # 缺 taker_base（舊快取）→ 中性值放行，不清空
                out["taker_ratio_s"] = 0.5
        return out

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        pos_in_ch = _num(g("fib_ch_pos"))
        ch_dir    = _num(g("fib_ch_dir"))

        entry_z = float(self.params.get("entry_zone", 0.30))
        exit_z  = float(self.params.get("exit_zone",  0.80))
        brk     = float(self.params.get("break_buffer", 0.10))
        mode    = str(self.params.get("mode", "trend"))

        # ── 持倉中出場 ───────────────────────────────────────────────────────
        if position != 0:
            if pos_in_ch is None:
                return position
            if mode == "reversion":
                if position == 1:   # 多單：到達目標頂或跌破通道 → 平倉
                    if pos_in_ch > exit_z or pos_in_ch < 0:
                        return 0
                else:               # 空單：到達目標底或突破通道 → 平倉
                    if pos_in_ch < (1.0 - exit_z) or pos_in_ch > 1:
                        return 0
            else:                   # trend：pos 語意方向一致，多空共用
                if pos_in_ch > exit_z or pos_in_ch < -brk:
                    return 0
            return position

        # ── 空手進場 ─────────────────────────────────────────────────────────
        if pos_in_ch is None or ch_dir is None or ch_dir == 0:
            return 0
        if not self._regime_ok(row):
            return 0

        # 暴量突破過濾：當根成交量 > N × 10根均量時跳過進場（breakout bar 逆勢接刀風險高）
        spike_ratio = float(self.params.get("volume_spike_ratio", 0.0))
        if spike_ratio > 0:
            vol_r = _num(g("vol_ratio"))
            if vol_r is not None and not pd.isna(vol_r) and vol_r > spike_ratio:
                return 0

        # 短期動能閘門：過去 N 根 K 棒合計漲跌 > 門檻 (%) 時跳過進場
        # 用途：急漲/急跌行情阻止均值回歸策略逆勢接刀
        mom_max = float(self.params.get("momentum_max_pct", 0.0))
        if mom_max > 0:
            mom = _num(g("mom_pct"))
            if mom is not None and not pd.isna(mom) and mom > mom_max:
                return 0

        min_w_atr = float(self.params.get("min_channel_width_atr", 0.0))
        if min_w_atr > 0:
            atr_val = _num(g("atr"))
            ch0 = _num(g("fib_ch_0"))
            ch100 = _num(g("fib_ch_100"))
            if (atr_val is not None and not pd.isna(atr_val) and
                    ch0 is not None and ch100 is not None):
                if abs(ch100 - ch0) < min_w_atr * atr_val:
                    return 0

        # 先決定候選進場方向，再過訂單流閘門（use_structure）
        if mode == "reversion":
            if pos_in_ch < entry_z:
                target = int(ch_dir)         # 原點側 → 順趨勢（上升=多、下降=空）
            elif pos_in_ch > (1.0 - entry_z):
                target = -int(ch_dir)        # 目標側 → 逆趨勢（上升=空、下降=多）
            else:
                return 0
        else:                                # trend mode（預設）
            if pos_in_ch < entry_z:
                target = int(ch_dir)
            else:
                return 0

        # 訂單流閘門：主動買賣盤失衡與進場方向衝突 → 不進場
        #   做多需買盤佔比 ≥ of_long_min、做空需 ≤ of_short_max（use_structure=False 恆放行）
        if not self._structure_ok(row, target):
            return 0
        return target


class TrendPullbackStrategy(Strategy):
    """趨勢過濾 + 回踩進場 + KD 觸發（使用者指定的多指標短線打法）。

    主方向（200EMA）：價 > 200EMA 只做多、價 < 200EMA 只做空，過濾逆勢假突破。
    進場（順勢回踩，不追極端，四條件 AND）：
      多 = 價 > 200EMA、EMA20 > EMA50（短線動能向上）、
           RSI 在回踩區 [rsi_lo, rsi_hi]（非 >hi 追高）、KD 黃金交叉（%K 上穿 %D，觸發鍵）。
      空 = 鏡像（價 < 200EMA、EMA20 < EMA50、RSI 在區間、KD 死叉）。
    出場：趨勢翻轉（價反向跨越 200EMA）或動能翻轉（EMA20/50 反向）。
          ATR 動態停損/停利由 risk 層（RiskOfficer.exit_levels）負責，不在此重複。

    布林通道暫不納入進場（避免條件過多→幾乎不交易）；regime 預設不過濾
    （200EMA + EMA 交叉本身已是趨勢過濾）。
    """
    name = "trend_pullback"
    defaults = {"ema_trend": 200, "ema_fast": 20, "ema_slow": 50,
                "rsi_period": 14, "rsi_lo": 40.0, "rsi_hi": 60.0,
                "k_period": 14, "smooth_k": 3, "d_period": 3}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        p = self.params
        out["ema_t"] = se.ema(out["close"], int(p["ema_trend"]))
        out["ema_f"] = se.ema(out["close"], int(p["ema_fast"]))
        out["ema_s"] = se.ema(out["close"], int(p["ema_slow"]))
        out["rsi"]   = se.rsi(out["close"], int(p["rsi_period"]))
        kd = se.stochastic(out, int(p["k_period"]), int(p["smooth_k"]), int(p["d_period"]))
        out["stoch_k"], out["stoch_d"] = kd["stoch_k"], kd["stoch_d"]
        k, d = out["stoch_k"], out["stoch_d"]
        # 黃金/死亡交叉（用 shift(1) 比前一根，causal、不 repaint）
        out["kd_gold"] = ((k > d) & (k.shift(1) <= d.shift(1))).astype(float)
        out["kd_dead"] = ((k < d) & (k.shift(1) >= d.shift(1))).astype(float)
        out["atr"] = se.atr(out, 14)
        out = self._prepare_cvd(out)               # OPT-16：背離欄（use_cvd_filter 關時不影響進場）
        return self._prepare_regime(out)

    def signal(self, row, position: int) -> int:
        g = (lambda key: row.get(key) if hasattr(row, "get") else row[key])
        close = _num(g("close")); et = _num(g("ema_t"))
        ef = _num(g("ema_f")); es = _num(g("ema_s")); rsi = _num(g("rsi"))
        if any(v is None or pd.isna(v) for v in (close, et, ef, es, rsi)):
            return position                      # 資料不足：維持現狀

        # 出場：趨勢/動能翻轉（與 regime 無關，多空共用）
        if position == 1:
            return 0 if (close < et or ef < es) else 1
        if position == -1:
            return 0 if (close > et or ef > es) else -1

        # 空手進場
        if not self._regime_ok(row):
            return 0
        lo = float(self.params["rsi_lo"]); hi = float(self.params["rsi_hi"])
        in_zone = lo <= rsi <= hi
        gold = (_num(g("kd_gold")) or 0.0) > 0.5
        dead = (_num(g("kd_dead")) or 0.0) > 0.5
        if close > et and ef > es and in_zone and gold and self._cvd_ok(row, 1):
            return 1
        if close < et and ef < es and in_zone and dead and self._cvd_ok(row, -1):
            return -1
        return 0


class FibEmaStrategy(Strategy):
    """費波那契 EMA 排列策略（Fibonacci EMA Alignment）。

    核心指標：fib_ema_score — 以 8/13/21（快）vs 34/55/89（慢）共 9 對 EMA
    計算多頭/空頭排列的強度（0=完全空頭, 1=完全多頭）。

    進場：score ≥ score_bull（預設 0.67）且 RSI 在合理區間（避免追極端）→ 多
          score ≤ score_bear（預設 0.33）且 RSI 在合理區間 → 空
    出場：持多時 score 跌破 score_bear；持空時 score 突破 score_bull
    """

    name = "fib_ema"
    defaults = {
        "rsi_period": 14,
        "rsi_lo": 35.0,
        "rsi_hi": 65.0,
        "score_bull": 0.67,
        "score_bear": 0.33,
        # OPT-17：出場死區門檻。None=現行（持多等 score≤bear=0.33 才出，死區大→回吐浮盈）；
        # 設值(如 0.5)→持多 score≤exit_mid 即出、持空 score≥1−exit_mid 即出（縮小死區）。預設關。
        "exit_mid": None,
    }
    allow_short = True
    regime_pref = "trend"
    max_hidden_lookback = 89   # fib_ema_score 用 34/55/89 慢線，最慢 89（不在 params 內）

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        p = self.params
        out["fib_score"] = se.fib_ema_score(out["close"])
        out["rsi"] = se.rsi(out["close"], int(p["rsi_period"]))
        out["atr"] = se.atr(out, 14)
        out = self._prepare_cvd(out)               # OPT-16：背離欄（use_cvd_filter 關時不影響進場）
        return self._prepare_regime(out)

    def signal(self, row, position: int) -> int:
        def g(col):
            v = row.get(col) if hasattr(row, "get") else getattr(row, col, None)
            return float(v) if v is not None and str(v) != "nan" else None

        score = g("fib_score")
        rsi   = g("rsi")
        if score is None:
            return position  # not enough data yet, hold

        bull = float(self.params.get("score_bull", 0.67))
        bear = float(self.params.get("score_bear", 0.33))
        rsi_lo = float(self.params.get("rsi_lo", 35.0))
        rsi_hi = float(self.params.get("rsi_hi", 65.0))
        in_rsi_zone = rsi is None or (rsi_lo <= rsi <= rsi_hi)

        # exit logic — score reversal（出場不受 regime 限制）
        # OPT-17：exit_mid 有設時用較緊的中位門檻出場，縮小 bear/bull 死區以減少浮盈回吐。
        exit_mid = self.params.get("exit_mid")
        long_exit_thr = bear if exit_mid is None else float(exit_mid)
        short_exit_thr = bull if exit_mid is None else (1.0 - float(exit_mid))
        if position == 1:
            return 0 if score <= long_exit_thr else 1
        if position == -1:
            return 0 if score >= short_exit_thr else -1

        # entry: regime 閘門（trend 盤才進場，盤整盤 whipsaw 太嚴重）
        if not self._regime_ok(row):
            return 0

        if score >= bull and self._cvd_ok(row, 1):       # OPT-16：頂背離時不追多
            return 1
        if score <= bear and self._cvd_ok(row, -1):      # OPT-16：底背離時不追空
            return -1
        return 0


class VolMomentumStrategy(Strategy):
    """成交量計時的時序動量（Volume-timed Time-Series Momentum）。

    文獻依據：Bitcoin intraday time-series momentum（Shen 2022, Financial Review）—
    「高成交量／高波動時段的動量最可預測」。策略庫既有的動量策略（ema_cross/supertrend/
    donchian）都失敗在「不分量能一律追」；本策略的差異化就是**只在量能放大時才順動量進場**，
    過濾掉無量的假動量漂移。

    進場：
      · 近 lookback 根報酬 mom > +thresh（強勢上行）且量能 vol_ratio ≥ vol_min
        且（無趨勢過濾 or close > 200EMA）→ 做多
      · mom < −thresh 且量能足且（close < 200EMA）→ 做空
    出場：
      · 動量衰竭：持多時 mom 跌回 0 以下、持空時 mom 升回 0 以上 → 平倉
      （硬停損 / 追蹤停損由風控引擎另外處理，與策略層無關）

    causal：mom 用 pct_change(lookback)（只看過去）、vol_ratio 用滾動均量、EMA 只用過去。
    """
    name = "vol_momentum"
    defaults = {"lookback": 6, "entry_thresh": 0.010, "vol_period": 20,
                "vol_min": 1.2, "use_trend_filter": True, "trend_ema_period": 200}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        lb = max(1, int(self.params["lookback"]))
        out["mom"] = out["close"].pct_change(lb)
        vp = max(1, int(self.params["vol_period"]))
        vavg = out["volume"].rolling(vp, min_periods=1).mean().replace(0, float("nan"))
        out["vol_ratio"] = out["volume"] / vavg
        out["ema_trend"] = se.ema(out["close"], int(self.params["trend_ema_period"]))
        out["atr"] = se.atr(out, 14)
        return out

    def signal(self, row: pd.Series, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        mom = _num(g("mom"))
        if mom is None:
            return position                          # 暖機 → 維持現狀
        # ── 出場：動量衰竭（穿回 0）──
        if position == 1 and mom < 0:
            return 0
        if position == -1 and mom > 0:
            return 0
        if position != 0:
            return position                          # 動量仍同向 → 續抱

        # ── 進場：強動量 + 量能放大 + （可選）順大趨勢 ──
        thresh = float(self.params["entry_thresh"])
        vol_r = _num(g("vol_ratio"))
        vol_ok = vol_r is not None and vol_r >= float(self.params["vol_min"])
        if not vol_ok:
            return 0
        close = _num(g("close"))
        et = _num(g("ema_trend"))
        use_tf = bool(self.params.get("use_trend_filter", True))
        trend_up = (not use_tf) or et is None or close is None or close > et
        trend_dn = (not use_tf) or et is None or close is None or close < et
        if mom > thresh and trend_up:
            return 1
        if mom < -thresh and trend_dn:
            return -1
        return 0


class EmaFibVolStrategy(Strategy):
    """雙均線 × 斐波那契通道 × 量能 三重確認複合策略（2026-07-04）。

    設計依據（pullback trading 文獻標準做法）：順勢策略最大的敗因是「追突破」——
    在動能耗盡點進場。改成三個獨立維度同時確認才進場：
      1. 趨勢（雙均線）：ema_fast/ema_slow 交叉方向 + ATR 分離緩衝
         （沿用 ema_cross 驗證過的抗假交叉設計：分離 > sep_atr_k×ATR 才算有效）
      2. 時機（斐波那契通道）：fib_channel_levels 的 fib_ch_pos < entry_zone
         —— 只在「回調到通道原點區」買，不追已走遠的價格（買回調不買突破）
      3. 參與度（量能）：vol_ratio ≥ vol_min —— 只在量能放大時進場，
         過濾無量假動（沿用 vol_momentum 的量能計時原則）
    且通道方向必須與均線方向一致（confluence，兩個獨立趨勢判定互相驗證）。

    出場（hysteresis，進嚴出鬆）：
      · 均線翻轉（裸交叉，不需分離）→ 趨勢結束
      · fib_ch_pos > exit_zone → 到達通道目標側，停利
      · fib_ch_pos < -break_buffer → 跌破通道原點，停損
    """
    name = "ema_fib_vol"
    defaults = {"fast": 12, "slow": 26, "sep_atr_k": 0.5,
                "pivot_left": 5, "pivot_right": 3,
                "entry_zone": 0.382, "exit_zone": 0.80, "break_buffer": 0.10,
                "vol_period": 20, "vol_min": 1.2}
    allow_short = True
    regime_pref = "trend"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.fib_channel_levels(df.copy(),
                                    int(self.params["pivot_left"]),
                                    int(self.params["pivot_right"]))
        out["ema_fast"] = se.ema(out["close"], int(self.params["fast"]))
        out["ema_slow"] = se.ema(out["close"], int(self.params["slow"]))
        out["atr"] = se.atr(out, 14)
        vp = max(1, int(self.params["vol_period"]))
        vavg = out["volume"].rolling(vp, min_periods=1).mean().replace(0, float("nan"))
        out["vol_ratio"] = out["volume"] / vavg
        return self._prepare_regime(out)

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        fast = _num(g("ema_fast"))
        slow = _num(g("ema_slow"))
        if fast is None or slow is None:
            return position                          # 暖機 → 維持現狀（全庫契約一致）
        pos_in_ch = _num(g("fib_ch_pos"))
        ch_dir = _num(g("fib_ch_dir"))

        exit_z = float(self.params["exit_zone"])
        brk = float(self.params["break_buffer"])

        # ── 持倉中出場：均線翻轉（裸交叉）或通道目標/跌破 ──
        if position != 0:
            if position == 1 and fast < slow:
                return 0
            if position == -1 and fast > slow:
                return 0
            if pos_in_ch is not None:
                if pos_in_ch > exit_z or pos_in_ch < -brk:
                    return 0
            return position

        # ── 空手進場：三閘門 + 方向一致 ──
        if pos_in_ch is None or ch_dir is None or ch_dir == 0:
            return 0
        if not self._regime_ok(row):
            return 0
        # 閘門 2：時機 — 回調到通道原點區才進，不追價
        if pos_in_ch >= float(self.params["entry_zone"]):
            return 0
        # 閘門 3：參與度 — 量能放大確認
        vol_r = _num(g("vol_ratio"))
        if vol_r is None or vol_r < float(self.params["vol_min"]):
            return 0
        # 閘門 1：趨勢 — 均線方向與通道方向一致，且分離超過 ATR 緩衝（防假交叉）
        atr_val = _num(g("atr"))
        sep = float(self.params["sep_atr_k"]) * atr_val if (atr_val is not None) else 0.0
        target = int(ch_dir)
        if target == 1 and (fast - slow) > sep:
            return 1
        if target == -1 and (slow - fast) > sep:
            return -1
        return 0


class MaConvergencePullbackStrategy(Strategy):
    """六線密集/發散 + 首次回踩 20 均線（2026-07-05，還原 YouTube 分析的雙均線系統）。

    核心概念（與 ema_cross 的死叉觸發完全不同）：
      1. 六線（MA20/60/120 + EMA20/60/120）密集 = 盤整；發散且排列一致
         （短週期在最外側）= 趨勢確立。
      2. 進場不是發散的當下，是發散確立**之後**、價格第一次拉回觸及 20 均線
         但收盤未跌破（"回踩不破"）——確認拉回結束、趨勢延續才進場，
         同一段趨勢只觸發一次（避免每次小拉回都重複進場）。
      3. 出場：均線排列被打破（趨勢結束）強制平倉；獲利了結交給既有風控層
         的 R 倍數停利/Chandelier 追蹤停損（與其他策略一致，策略層不重複做）。

    此設計刻意不用死叉當出場——影片原始系統的出場是固定賠率/前密集區/
    費波那契擴張位，全部是「結構性目標」而非「反轉訊號」，對應本庫的
    risk_officer.exit_levels（tp_R_mult）與 Chandelier 追蹤停損。
    """
    name = "ma_convergence_pullback"
    defaults = {"ma_fast": 20, "ma_mid": 60, "ma_slow": 120,
                "divergence_thresh": 0.03, "pullback_tolerance": 0.0,
                # 密集/二次回踩（圖表顯示用；signal() 仍只吃首次回踩，b9 行為不變）：
                "density_thresh": 0.015,     # 六線 spread ≤ 此值視為密集（收斂盤整）
                "rearm_gap": 0.01,           # 首踩後 price 須離開 MA20 此比例才偵測二次回踩
                # 多週期共振（2026-07-05，文獻：雙均線最常見的假訊號過濾器）：
                # 開啟後進場方向須與日線 MA 排列一致（htf_trend=0 中性/暖機也擋，
                # 嚴格共振語意）。預設關 → 行為與舊版逐位元一致。
                "use_htf_filter": False, "htf_fast": 20, "htf_slow": 60,
                # 2026-07-06：is_breakout 補「必須先真的密集過」的前提（見下方 prepare()）。
                # 預設關＝b9 現行線上邏輯逐位元不變；圖表面板（core.chart_data.
                # ma6_overlay_data）明確開啟，只影響顯示，不動 b9 實際下單依據。
                # 之後若要讓 b9 也吃這個修正，再由使用者決定開啟 + 重新回測。
                "require_density_for_breakout": False,
                # 2026-07-06：合併訊號進場（測試能否用增加樣本量改善 edge）。
                # 預設全關＝b9 現行只吃 is_first_pullback，逐位元不變。開啟後
                # is_breakout/is_second_pullback 也能觸發進場（一樣受 htf 過濾約束）。
                "use_breakout_entry": False, "use_second_pullback_entry": False}
    allow_short = True
    regime_pref = "any"          # 趨勢判斷已內建在 trend_dir，不疊加外層 regime 閘門

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        f, m, sl = int(self.params["ma_fast"]), int(self.params["ma_mid"]), int(self.params["ma_slow"])
        out["ma20"] = out["close"].rolling(f, min_periods=f).mean()
        out["ma60"] = out["close"].rolling(m, min_periods=m).mean()
        out["ma120"] = out["close"].rolling(sl, min_periods=sl).mean()
        out["ema20"] = se.ema(out["close"], f)
        out["ema60"] = se.ema(out["close"], m)
        out["ema120"] = se.ema(out["close"], sl)
        out["atr"] = se.atr(out, 14)
        # 多週期共振：日線 MA 排列方向（shift(1) 只用已收完日線，見 se.htf_trend）
        if bool(self.params.get("use_htf_filter", False)):
            out["htf_trend"] = se.htf_trend(out, rule="1D",
                                            fast=int(self.params["htf_fast"]),
                                            slow=int(self.params["htf_slow"]))

        lines = out[["ma20", "ma60", "ma120", "ema20", "ema60", "ema120"]]
        out["spread"] = (lines.max(axis=1) - lines.min(axis=1)) / out["close"]

        bull_order = ((out["ma20"] > out["ma60"]) & (out["ma60"] > out["ma120"]) &
                      (out["ema20"] > out["ema60"]) & (out["ema60"] > out["ema120"]))
        bear_order = ((out["ma20"] < out["ma60"]) & (out["ma60"] < out["ma120"]) &
                      (out["ema20"] < out["ema60"]) & (out["ema60"] < out["ema120"]))
        # order_dir：六線「當下排列」方向，跟 trend_dir 不同——trend_dir 是狀態機鎖定值
        # （只在 breakout 那一根才從 0 翻正/負，密集/發散初期整段是 0）；order_dir 每根
        # 都逐根反映當下排列，供圖表子圖連續畫出「收斂→排列成形→發散」的過程，不用
        # 等狀態機確認才顯示方向（2026-07-13，見 chart_data.py 的 spread 子圖）。
        out["order_dir"] = np.where(bull_order, 1.0, np.where(bear_order, -1.0, 0.0))
        thresh = float(self.params["divergence_thresh"])
        divergent_bull = bull_order & (out["spread"] > thresh)
        divergent_bear = bear_order & (out["spread"] > thresh)

        # 六線密集（收斂盤整）：spread ≤ density_thresh。
        out["is_density"] = out["spread"] <= float(self.params["density_thresh"])

        # 狀態機（單次正向掃描）：trend_dir 在同向排列持續期間維持 ±1，排列打破歸零；
        # is_first_pullback 在每個新趨勢區間內最多標記一次 True（首次觸及 20 均線不破）。
        # is_breakout（方法一）：密集→發散的突破當根。require_density_for_breakout 開啟時，
        # 必須先真的密集過（seen_density）才算數，否則只是排列剛好對齊+spread夠大（可能是
        # 已經在半路的強趨勢），不算「糾結後發散」——2026-07-06：原本沒這個前提，會把真實
        # 幣種資料上「半路的強趨勢」誤標成突破。此開關預設關，只給圖表面板用（見上方
        # defaults 註解），b9 實盤 trend_dir / is_first_pullback 逐位元不變。
        require_density = bool(self.params.get("require_density_for_breakout", False))
        n = len(out)
        trend_dir = np.zeros(n)
        first_pullback = np.zeros(n, dtype=bool)
        breakout = np.zeros(n, dtype=bool)
        second_pullback = np.zeros(n, dtype=bool)
        cur_dir = 0
        used = False
        armed2 = False              # 首踩後、price 已離開 MA20 夠遠 → 可偵測二次回踩
        seen_density = False        # 目前這段「無趨勢」期間，六線是否真的密集過一次
        tol = float(self.params["pullback_tolerance"])
        rearm = float(self.params["rearm_gap"])
        close_v = out["close"].to_numpy()
        low_v = out["low"].to_numpy()
        high_v = out["high"].to_numpy()
        ma20_v = out["ma20"].to_numpy()
        db = divergent_bull.to_numpy()
        de = divergent_bear.to_numpy()
        bo = bull_order.to_numpy()
        be = bear_order.to_numpy()
        dens = out["is_density"].to_numpy()

        for i in range(n):
            if np.isnan(ma20_v[i]):
                continue
            if cur_dir == 1:
                if not bo[i]:               # 多頭排列被打破 → 趨勢結束
                    cur_dir, used, armed2, seen_density = 0, False, False, False
                elif not used and low_v[i] <= ma20_v[i] * (1 + tol) and close_v[i] > ma20_v[i] * (1 - tol):
                    first_pullback[i] = True
                    used = True
                elif used:                  # 首踩已發生 → 偵測二次回踩（純加法，不影響首踩）
                    if close_v[i] > ma20_v[i] * (1 + rearm):
                        armed2 = True
                    if armed2 and low_v[i] <= ma20_v[i] * (1 + tol) and close_v[i] > ma20_v[i] * (1 - tol):
                        second_pullback[i] = True
                        armed2 = False
            elif cur_dir == -1:
                if not be[i]:
                    cur_dir, used, armed2, seen_density = 0, False, False, False
                elif not used and high_v[i] >= ma20_v[i] * (1 - tol) and close_v[i] < ma20_v[i] * (1 + tol):
                    first_pullback[i] = True
                    used = True
                elif used:
                    if close_v[i] < ma20_v[i] * (1 - rearm):
                        armed2 = True
                    if armed2 and high_v[i] >= ma20_v[i] * (1 - tol) and close_v[i] < ma20_v[i] * (1 + tol):
                        second_pullback[i] = True
                        armed2 = False
            else:
                # 沒有趨勢期間：先累積「是否真的密集過」；require_density 開啟時，
                # 密集突破必須先經歷密集才算數（否則只是排列剛好對齊+spread夠大，
                # 可能是已經在半路的強趨勢）。關閉時（預設）維持舊行為，不檢查前提。
                if dens[i]:
                    seen_density = True
                density_ok = seen_density or not require_density
                if db[i] and density_ok:
                    cur_dir, used, armed2 = 1, False, False
                    breakout[i] = True          # 方法一：密集突破當根
                    seen_density = False
                elif de[i] and density_ok:
                    cur_dir, used, armed2 = -1, False, False
                    breakout[i] = True
                    seen_density = False
            trend_dir[i] = cur_dir

        out["trend_dir"] = trend_dir
        out["is_first_pullback"] = first_pullback
        out["is_breakout"] = breakout
        out["is_second_pullback"] = second_pullback
        return out

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        trend_dir = _num(g("trend_dir"))
        if trend_dir is None:
            return position                          # 暖機 → 維持現狀

        if position != 0:
            # 出場：均線排列被打破 / 趨勢翻轉（結構性停損），獲利了結交給風控層
            if position == 1 and trend_dir != 1:
                return 0
            if position == -1 and trend_dir != -1:
                return 0
            return position

        entry_trigger = bool(g("is_first_pullback"))
        if not entry_trigger and bool(self.params.get("use_breakout_entry", False)):
            entry_trigger = bool(g("is_breakout"))
        if not entry_trigger and bool(self.params.get("use_second_pullback_entry", False)):
            entry_trigger = bool(g("is_second_pullback"))
        if not entry_trigger:
            return 0
        # 多週期共振：進場方向須與日線趨勢一致（0=中性/暖機也擋，嚴格版）。
        # 只擋新進場——上方出場邏輯不經過這裡，持倉管理不受 htf 影響。
        if bool(self.params.get("use_htf_filter", False)):
            htf = _num(g("htf_trend"))
            if htf is None or int(htf) != int(trend_dir):
                return 0
        if trend_dir == 1:
            return 1
        if trend_dir == -1:
            return -1
        return 0


class ChartPatternBreakoutStrategy(Strategy):
    """古典圖表形態（三角形/楔形收斂後突破），2026-07-06。

    使用者要求測試 TradingView「Chart Patterns Screener」這類形態辨識指標背後
    的核心概念是否有 edge：用 signal_engineer.trendline_pair() 算出的上下兩條
    已確認樞紐趨勢線（跟均線密集/發散完全不同的訊號來源——真實樞紐點連線，
    不是六線價差），夾角收斂到 convergence_ratio 以下時，收盤價突破其中一側
    → 進場；跌回被突破的線內 → 視為結構失敗、出場。跟 ma_convergence_pullback
    同精神（收斂後發散才算數），但這裡的「收斂」是幾何樞紐趨勢線夾角，不是
    六線價差——完全獨立的訊號家族，用來驗證古典圖表形態這條路子是否也一樣
    卡在訊號密度問題，還是真的是不同類別的 edge。
    """
    name = "chart_pattern_breakout"
    defaults = {"pivot_left": 5, "pivot_right": 5, "convergence_ratio": 0.8}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.trendline_pair(df.copy(), pivot_left=int(self.params["pivot_left"]),
                                pivot_right=int(self.params["pivot_right"]))
        out["atr"] = se.atr(out, 14)

        n = len(out)
        res_line = out["res_line"].to_numpy()
        sup_line = out["sup_line"].to_numpy()
        res_slope = out["res_slope"].to_numpy()
        sup_slope = out["sup_slope"].to_numpy()
        res_p1 = out["res_p1"].to_numpy()
        sup_p1 = out["sup_p1"].to_numpy()
        close = out["close"].to_numpy()

        ratio = float(self.params["convergence_ratio"])
        breakout = np.zeros(n, dtype=bool)
        breakout_dir = np.zeros(n)
        used = False
        anchor_key = None

        for i in range(n):
            if np.isnan(res_line[i]) or np.isnan(sup_line[i]):
                continue
            key = (res_p1[i], sup_p1[i])
            if key != anchor_key:
                anchor_key = key
                used = False

            gap_now = res_line[i] - sup_line[i]
            start_idx = int(max(res_p1[i], sup_p1[i]))
            # 用當根的斜率把兩條線外推回 start_idx，避免直接查歷史欄位值
            # （那可能是用不同樞紐組合算出來的，見 trendline_pair 註解）。
            res_at_start = res_line[i] - res_slope[i] * (i - start_idx)
            sup_at_start = sup_line[i] - sup_slope[i] * (i - start_idx)
            gap_start = res_at_start - sup_at_start
            converging = 0 < gap_now < gap_start * ratio

            if converging and not used:
                if close[i] > res_line[i]:
                    breakout[i] = True
                    breakout_dir[i] = 1
                    used = True
                elif close[i] < sup_line[i]:
                    breakout[i] = True
                    breakout_dir[i] = -1
                    used = True

        out["is_pattern_breakout"] = breakout
        out["pattern_breakout_dir"] = breakout_dir
        return out

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        res_line = _num(g("res_line"))
        sup_line = _num(g("sup_line"))
        close = _num(g("close"))

        if position == 1:
            if res_line is not None and close is not None and close < res_line:
                return 0        # 跌回被突破的阻力線內 → 結構失敗，出場
            return 1
        if position == -1:
            if sup_line is not None and close is not None and close > sup_line:
                return 0        # 漲回被突破的支撐線內 → 結構失敗，出場
            return -1

        is_bo = bool(g("is_pattern_breakout"))
        if not is_bo:
            return 0
        d = _num(g("pattern_breakout_dir"))
        if d == 1:
            return 1
        if d == -1:
            return -1
        return 0


class RegressionChannelStrategy(Strategy):
    """滾動線性迴歸通道均值回歸，2026-07-06。

    使用者要求測試 TradingView「Polynomial/Linear Regression Volume Profile」
    這類指標背後的核心概念：用統計迴歸配適的通道（而非 fib_channel 的樞紐點
    錨定通道）當作進出場依據。對每根 K 棒取過去 window 根收盤價做 OLS 線性
    迴歸，配適線在最後一根的值當「中心線」，殘差標準差 × band_mult 當通道
    寬度。收盤價觸及下軌 → 做多賭均值回歸；觸及上軌 → 做空。回到中心線視為
    結構性停利出場；停損交給既有風控層 ATR 機制（跟其他策略一致，方便公平
    比較進場邏輯本身的差異）。
    """
    name = "regression_channel"
    defaults = {"window": 100, "band_mult": 2.0}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        window = int(self.params["window"])
        mult = float(self.params["band_mult"])
        close = out["close"].to_numpy()
        n = len(out)

        center = np.full(n, np.nan)
        upper = np.full(n, np.nan)
        lower = np.full(n, np.nan)
        slope = np.full(n, np.nan)

        if n >= window:
            t = np.arange(window, dtype=float)
            t_mean = t.mean()
            t_var = float(((t - t_mean) ** 2).sum())

            for i in range(window - 1, n):
                y = close[i - window + 1: i + 1]
                y_mean = y.mean()
                b = float(((t - t_mean) * (y - y_mean)).sum() / t_var)
                a = y_mean - b * t_mean
                fitted = a + b * t
                resid_std = float(np.std(y - fitted))
                center[i] = fitted[-1]
                slope[i] = b
                upper[i] = fitted[-1] + mult * resid_std
                lower[i] = fitted[-1] - mult * resid_std

        out["reg_center"] = center
        out["reg_upper"] = upper
        out["reg_lower"] = lower
        out["reg_slope"] = slope
        out["atr"] = se.atr(out, 14)
        return out

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        center = _num(g("reg_center"))
        upper = _num(g("reg_upper"))
        lower = _num(g("reg_lower"))
        close = _num(g("close"))
        if center is None or upper is None or lower is None or close is None:
            return position

        if position == 1:
            return 0 if close >= center else 1
        if position == -1:
            return 0 if close <= center else -1

        if close <= lower:
            return 1
        if close >= upper:
            return -1
        return 0


class FibZeroAxisRejectStrategy(Strategy):
    """零軸拒絕交易法（2026-07-09，復刻使用者提供的分析師「自然交易理論」看盤法）。

    分析師觀點：迴歸斐波那契通道的「零軸」（下降通道=上緣壓力／上升通道=下緣支撐）
    有高機率成為反向點。當價格觸及零軸、且多頭量能衰減（動能枯竭、無放量續強突破）
    → 順通道方向進場（下降做空／上升做多）；價格走到通道內部目標（靠近一軸）獲利，
    價格反向突破零軸（通道結構破壞）則停損。

    ⚠️ 分析師宣稱零軸/一軸有 80-90% 反轉勝率——這是他的說法、未經驗證。本策略只是
    忠實把「機械規則」實作出來供嚴格回測檢驗，不代表那個勝率是真的（見
    docs/strategy_research_log.md 這一整輪對網路策略的實測，幾乎全部無 edge）。

    pos 語意（se.fib_regression_levels）：0=零軸(進場側)、1=一軸(對側目標)；多空
    在 pos 空間對稱——都是 pos 往 1 走獲利、pos 轉負(反向突破零軸)停損。
    """
    name = "fib_zero_reject"
    defaults = {"lookback": 60, "reg_k": 2.0,
                "zero_zone": 0.15,       # pos ≤ 此值視為「觸零軸」
                "entry_zone": 0.25,      # 第二根確認時，當根 pos 仍須 ≤ 此值（還在零軸附近）
                "target_pos": 0.5,       # pos ≥ 此值視為到達內部目標（獲利了結）
                "break_buffer": 0.15,    # pos < −此值視為反向突破零軸（通道破壞、停損）
                "vol_window": 20,        # 量能均值視窗
                "use_volume_gate": True, # 是否要求量能衰減才進場（分析師核心條件）
                # 2026-07-09 使用者指正：分析師是「撞零軸後看第二根 4h K 棒」才決定進場——
                # 第一根撞零軸(測試)，第二根若沒放量突破零軸(續強失敗)才算拒絕、才進場。
                # 預設關＝維持舊「觸即進」行為（既有測試/回測相容）；開啟＝更忠實復刻。
                "use_second_candle_confirm": False}
    allow_short = True
    regime_pref = "any"

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.fib_regression_levels(df.copy(), lookback=int(self.params["lookback"]),
                                       k=float(self.params["reg_k"]))
        out["atr"] = se.atr(out, 14)
        # 量能衰減：當根成交量 < 近 vol_window 根均量 → 多頭動能枯竭（分析師的「量縮」）。
        vw = int(self.params["vol_window"])
        vol_avg = out["volume"].rolling(vw, min_periods=max(2, vw // 2)).mean()
        out["vol_decay"] = (out["volume"] < vol_avg).astype(float)

        # 第二根四小時確認：前一根撞零軸(pos_{i-1}≤zero_zone)，當根未放量突破零軸
        # （非「pos_i<−break 且量能放大」）且仍在零軸附近(pos_i≤entry_zone、未破)、
        # 同通道方向 → zero_reject_dir[i]=通道方向。這是分析師的「等第二根確認拒絕」。
        n = len(out)
        pos = out["fib_rc_pos"].to_numpy()
        direc = out["fib_rc_dir"].to_numpy()
        vol = out["volume"].to_numpy()
        zz = float(self.params["zero_zone"]); ez = float(self.params["entry_zone"])
        brk = float(self.params["break_buffer"])
        zr = np.zeros(n)
        for i in range(1, n):
            if np.isnan(pos[i]) or np.isnan(pos[i - 1]) or np.isnan(direc[i]):
                continue
            prev_at_zero = pos[i - 1] <= zz
            same_dir = direc[i] == direc[i - 1]
            still_near = (-brk) <= pos[i] <= ez               # 還在零軸附近、未反向突破
            vol_expand = vol[i] > vol[i - 1]
            broke_out = pos[i] < -brk                          # 反向突破零軸（往對壓力方向）
            is_breakout = broke_out and vol_expand            # 放量突破 → 不是拒絕
            if prev_at_zero and same_dir and still_near and not is_breakout:
                zr[i] = direc[i]
        out["zero_reject_dir"] = zr
        return out

    def signal(self, row, position: int) -> int:
        g = (lambda k: row.get(k) if hasattr(row, "get") else row[k])
        pos = _num(g("fib_rc_pos"))
        d = _num(g("fib_rc_dir"))
        target = float(self.params["target_pos"])
        brk = float(self.params["break_buffer"])

        if position != 0:
            if pos is None:
                return position                      # 暖機 → 維持現狀
            if pos >= target:                        # 到達內部目標 → 獲利了結
                return 0
            if pos < -brk:                           # 反向突破零軸 → 通道破壞、停損
                return 0
            return position

        # 第二根確認：進場依 prepare 算好的 zero_reject_dir（已含「未突破」判斷）
        if bool(self.params.get("use_second_candle_confirm", False)):
            zr = _num(g("zero_reject_dir"))
            if zr is None or zr == 0:
                return 0
            if bool(self.params.get("use_volume_gate", True)):   # 疊加量縮閘門（可選）
                vd = _num(g("vol_decay"))
                if vd is None or vd <= 0:
                    return 0
            return int(zr)

        # 舊行為（觸即進）：觸零軸 + 量能衰減 → 順通道方向進場
        if pos is None or d is None or d == 0:
            return 0
        if pos > float(self.params["zero_zone"]):    # 不在零軸 → 不進場
            return 0
        if bool(self.params.get("use_volume_gate", True)):
            vd = _num(g("vol_decay"))
            if vd is None or vd <= 0:
                return 0
        return int(d)                                # 順通道方向：下降做空、上升做多


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
    TrendPullbackStrategy.name: TrendPullbackStrategy,
    FibEmaStrategy.name: FibEmaStrategy,
    VolMomentumStrategy.name: VolMomentumStrategy,
    EmaFibVolStrategy.name: EmaFibVolStrategy,
    MaConvergencePullbackStrategy.name: MaConvergencePullbackStrategy,
    ChartPatternBreakoutStrategy.name: ChartPatternBreakoutStrategy,
    RegressionChannelStrategy.name: RegressionChannelStrategy,
    FibZeroAxisRejectStrategy.name: FibZeroAxisRejectStrategy,
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

    def __init__(self, strategies=None, min_agree: int = 2,
                 min_agree_range: int | None = None, **_):
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
        # 橫盤降門檻：未指定時等於 min_agree（向後相容）
        self.min_agree_range = min_agree_range if min_agree_range is not None else min_agree

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
        regime = row.get("regime", "") if hasattr(row, "get") else ""
        threshold = self.min_agree_range if regime == "range" else self.min_agree
        if long_v >= threshold:
            return 1
        if short_v >= threshold:
            return -1
        return 0


def build_strategy(name: str, **params) -> Strategy:
    if name == "consensus":
        return ConsensusStrategy(**params)
    if name not in STRATEGIES:
        raise ValueError(f"未知策略 {name}，可用：{list(STRATEGIES)} + consensus")
    return STRATEGIES[name](**params)
