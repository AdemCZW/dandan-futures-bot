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
                "pivot_left": 3, "pivot_right": 3, "ema_trend_period": 200}
    allow_short = True
    regime_pref = "range"          # 回調進出場：只在盤整盤開倉

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = se.fib_retracement(df, pivot_left=int(self.params["pivot_left"]),
                                 pivot_right=int(self.params["pivot_right"]))
        out["rsi"] = se.rsi(out["close"], int(self.params["rsi_period"]))
        out["atr"] = se.atr(out, 14)
        out["ema_trend"] = se.ema(out["close"], int(self.params["ema_trend_period"]))
        return self._prepare_regime(out)

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
        if fib_pos < 0.382 and rsi < 55 and uptrend:
            return 1                        # 上升趨勢 + 黃金支撐區 + RSI 未過熱 → 順勢買回調
        if fib_pos > 0.618 and rsi < 50 and downtrend:
            return -1                       # 下降趨勢 + 黃金阻力區 + RSI 動能轉弱 → 順勢空反彈
        return 0


STRATEGIES = {
    EMACrossStrategy.name: EMACrossStrategy,
    ZScoreRevertStrategy.name: ZScoreRevertStrategy,
    ZScoreLongShortStrategy.name: ZScoreLongShortStrategy,
    FibRetracementStrategy.name: FibRetracementStrategy,
}


def build_strategy(name: str, **params) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"未知策略 {name}，可用：{list(STRATEGIES)}")
    return STRATEGIES[name](**params)
