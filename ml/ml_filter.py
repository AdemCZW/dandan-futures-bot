"""XGBoost ML Filter — 在 entry signal 前加一道機率門檻。

流程：
  1. extract_features(prepared, events) → X（每個 entry 時間點的市場特徵）
  2. train_filter(X, y)                → fitted XGBClassifier
  3. signal_proba(model, X_row)        → float，表示「好交易」機率
  4. save_filter / load_filter         → 序列化 / 反序列化

特徵欄位 (FEATURE_COLS)：
  atr, adx, rsi, er, choppiness，及衍生的
  atr_pct（ATR 佔收盤價比例）、price_vs_ema200（若有）、vol_z（成交量 z-score）
"""
from __future__ import annotations
import os
import pickle
import numpy as np
import pandas as pd
from xgboost import XGBClassifier

# 基礎 7 個 + 策略專屬欄位（缺失時 fillna(median) 降級，不影響無此欄位的策略）
FEATURE_COLS = [
    # ── 通用市場微結構 ──
    "atr", "adx", "rsi", "er", "choppiness", "atr_pct", "vol_z",
    # ── fib_ema 專屬 ──
    "fib_score",        # Fibonacci EMA 排列強度 0~1
    # ── trend_pullback 專屬 ──
    "ema_t_dist",       # (close - EMA200) / close，趨勢偏離程度
    "stoch_k",          # Stochastic %K
    "stoch_d",          # Stochastic %D
    # ── fib_channel 專屬 ──
    "fib_ch_pos",       # 價格在費波那契通道的相對位置 0~1
    # ── smc_structure 專屬（2026-07-05 結構特徵）──
    # 2026-07-04 嚴格重測證實泛用波動度特徵對 BOS 訊號成敗無預測力（AUC 0.557），
    # 根因處方是「訊號自身的結構品質」；文獻（SMC 品質分級）同方向。
    "fvg_size_atr",     # FVG 缺口大小 / ATR（缺口越大 = 失衡越強）
    "bos_dist_atr",     # BOS 突破距離 / ATR（close 越過 swing 位準多遠 = 突破力道）
    "bos_body_atr",     # 突破棒實體 / ATR（大實體 = 決斷性突破，小實體 = 猶豫）
]


def extract_features(prepared: pd.DataFrame,
                     events: pd.DatetimeIndex,
                     vol_z_window: int = 20) -> pd.DataFrame:
    """從 prepared DataFrame 的 events 時間點擷取特徵。

    策略專屬欄位（fib_score / ema_t_dist / stoch_k / stoch_d / fib_ch_pos）缺失時
    填 NaN 後由 fillna(median) 補，等同「此策略不使用該特徵」。

    vol_z（成交量 z-score）用「滾動視窗」而非整段資料的 mean/std 計算：
    舊版拿全資料集的統計量，等於讓早期事件「偷看」了資料尾端才發生的爆量，
    訓練/驗證時的特徵分佈跟實盤推論時（只看得到當下為止）不一致（look-ahead 洩漏）。
    rolling().mean()/std() 在 t 時間點只用 [t-window, t] 的資料，因果、可對齊實盤。
    """
    if "volume" in prepared.columns:
        roll = prepared["volume"].rolling(vol_z_window, min_periods=max(2, vol_z_window // 2))
        vol_z_series = (prepared["volume"] - roll.mean()) / (roll.std() + 1e-9)
    else:
        vol_z_series = None

    # SMC 結構特徵需要 i-2 根的 high/low（FVG 缺口）→ 先建位置索引
    idx_pos = {t: i for i, t in enumerate(prepared.index)}
    high_v = prepared["high"].to_numpy() if "high" in prepared.columns else None
    low_v = prepared["low"].to_numpy() if "low" in prepared.columns else None

    rows = []
    for t in events:
        if t not in prepared.index:
            rows.append({c: np.nan for c in FEATURE_COLS})
            continue
        r = prepared.loc[t]
        close = float(r.get("close", np.nan))
        atr   = float(r.get("atr",   np.nan))

        vol_z = float(vol_z_series.loc[t]) if vol_z_series is not None else 0.0
        if np.isnan(vol_z):
            vol_z = 0.0

        ema_t = float(r.get("ema_t", np.nan))
        ema_t_dist = (close - ema_t) / close if (close and not np.isnan(ema_t)) else np.nan

        # ── SMC 結構特徵（swing/atr 欄位缺失 → NaN → fillna(median) 降級）──
        fvg_size_atr = bos_dist_atr = bos_body_atr = np.nan
        atr_ok = atr and not np.isnan(atr) and atr > 0
        if atr_ok:
            opn = float(r.get("open", np.nan))
            if not np.isnan(opn) and close:
                bos_body_atr = abs(close - opn) / atr
            sw_hi = float(r.get("swing_high", np.nan))
            sw_lo = float(r.get("swing_low", np.nan))
            # 突破距離取「當下方向」：多頭 BOS 用 close−swing_high、空頭用 swing_low−close，
            # 兩者取有正值的一側（事件點本來就是某一側的 BOS 訊號）
            cands = []
            if not np.isnan(sw_hi):
                cands.append(close - sw_hi)
            if not np.isnan(sw_lo):
                cands.append(sw_lo - close)
            pos_c = [c for c in cands if c > 0]
            if pos_c:
                bos_dist_atr = max(pos_c) / atr
            elif cands:
                bos_dist_atr = max(cands) / atr    # 都非正 → 取較接近突破的一側（負值也有資訊）
            # FVG 缺口：i-2 根 vs 當根（與 se.smc_levels 同定義，兩方向取有缺口的一側）
            i = idx_pos.get(t)
            if i is not None and i >= 2 and high_v is not None and low_v is not None:
                gap_bull = low_v[i] - high_v[i - 2]     # >0 → 看漲缺口
                gap_bear = low_v[i - 2] - high_v[i]     # >0 → 看跌缺口
                gap = max(gap_bull, gap_bear)
                fvg_size_atr = (gap / atr) if gap > 0 else 0.0

        rows.append({
            "atr":        atr,
            "adx":        float(r.get("adx",        np.nan)),
            "rsi":        float(r.get("rsi",         np.nan)),
            "er":         float(r.get("er",          np.nan)),
            "choppiness": float(r.get("choppiness",  np.nan)),
            "atr_pct":    atr / close if (close and not np.isnan(atr)) else np.nan,
            "vol_z":      vol_z,
            "fib_score":  float(r.get("fib_score",  np.nan)),
            "ema_t_dist": ema_t_dist,
            "stoch_k":    float(r.get("stoch_k",    np.nan)),
            "stoch_d":    float(r.get("stoch_d",    np.nan)),
            "fib_ch_pos": float(r.get("fib_ch_pos", np.nan)),
            "fvg_size_atr": fvg_size_atr,
            "bos_dist_atr": bos_dist_atr,
            "bos_body_atr": bos_body_atr,
        })

    df = pd.DataFrame(rows, index=events)[FEATURE_COLS]
    df = df.fillna(df.median())
    return df


def train_filter(X: pd.DataFrame, y: pd.Series,
                 n_estimators: int = 100,
                 max_depth: int = 3,
                 seed: int = 42) -> XGBClassifier:
    """訓練 XGBoost 二元分類器（+1 vs 其他）。

    y 應為 Triple Barrier 標籤（+1 / -1 / 0）；
    模型把 +1 當 positive class，-1/0 合併為 negative class。

    scale_pos_weight = 負樣本數/正樣本數：+1（贏單）通常遠少於 -1/0，不加權時
    模型會學到「永遠猜負類」也能有高準確率的偷懶解（ML_TRAINING_GUIDE.md 記錄的
    fib_channel/SOL 0.950 準確率案例——比 baseline 0.961 還差，就是這個問題）。
    無正樣本（全負類，無法訓練有意義的模型）時退回 1.0，不除以零。
    """
    y_bin = (y == 1).astype(int)
    n_pos = int(y_bin.sum())
    n_neg = len(y_bin) - n_pos
    scale_pos_weight = (n_neg / n_pos) if n_pos > 0 else 1.0
    model = XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=0.1,
        use_label_encoder=False,
        eval_metric="logloss",
        scale_pos_weight=scale_pos_weight,
        random_state=seed,
        n_jobs=-1,
        verbosity=0,
    )
    model.fit(X.values, y_bin.values)
    return model


def signal_proba(model: XGBClassifier, X_row: pd.DataFrame) -> float:
    """回傳 +1（好交易）的機率，0~1 之間。"""
    return float(model.predict_proba(X_row.values)[0, 1])


def save_filter(model: XGBClassifier, path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)


def load_filter(path: str) -> XGBClassifier:
    with open(path, "rb") as f:
        return pickle.load(f)
