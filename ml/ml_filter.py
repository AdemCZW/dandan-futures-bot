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
