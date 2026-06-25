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

FEATURE_COLS = ["atr", "adx", "rsi", "er", "choppiness",
                "atr_pct", "vol_z"]


def extract_features(prepared: pd.DataFrame,
                     events: pd.DatetimeIndex) -> pd.DataFrame:
    """從 prepared DataFrame 的 events 時間點擷取特徵。"""
    rows = []
    for t in events:
        if t not in prepared.index:
            rows.append({c: np.nan for c in FEATURE_COLS})
            continue
        r = prepared.loc[t]
        close  = float(r.get("close", np.nan))
        atr    = float(r.get("atr", np.nan))
        vol    = float(r.get("volume", np.nan))

        # 成交量 z-score（用整個 prepared 的 volume 計算）
        if "volume" in prepared.columns:
            v_mean = prepared["volume"].mean()
            v_std  = prepared["volume"].std() + 1e-9
            vol_z  = (vol - v_mean) / v_std
        else:
            vol_z = 0.0

        rows.append({
            "atr":        atr,
            "adx":        float(r.get("adx", np.nan)),
            "rsi":        float(r.get("rsi", np.nan)),
            "er":         float(r.get("er", np.nan)),
            "choppiness": float(r.get("choppiness", np.nan)),
            "atr_pct":    atr / close if close and not np.isnan(atr) else np.nan,
            "vol_z":      vol_z,
        })

    df = pd.DataFrame(rows, index=events)[FEATURE_COLS]
    df = df.fillna(df.median())   # 缺失值用中位數補
    return df


def train_filter(X: pd.DataFrame, y: pd.Series,
                 n_estimators: int = 100,
                 max_depth: int = 3,
                 seed: int = 42) -> XGBClassifier:
    """訓練 XGBoost 二元分類器（+1 vs 其他）。

    y 應為 Triple Barrier 標籤（+1 / -1 / 0）；
    模型把 +1 當 positive class，-1/0 合併為 negative class。
    """
    y_bin = (y == 1).astype(int)
    model = XGBClassifier(
        n_estimators=n_estimators,
        max_depth=max_depth,
        learning_rate=0.1,
        use_label_encoder=False,
        eval_metric="logloss",
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
