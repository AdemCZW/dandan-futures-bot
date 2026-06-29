"""Triple Barrier Labeling（López de Prado, AFML chap. 3）。

每個 entry event 往前掃描，回傳第一個被觸碰的 barrier 的標籤：
  +1  → 上方獲利屏障（price ≥ entry × (1+pt)）
  -1  → 下方止損屏障（price ≤ entry × (1-sl)）
   0  → 垂直屏障（超過 vb 根 bar 都沒觸碰）

Args:
    close    : 收盤價 Series（DatetimeIndex）
    events   : 需要標記的 entry 時間點（DatetimeIndex）
    pt       : 獲利目標比例（e.g. 0.03 = 3%）；ATR 模式下作為下限
    sl       : 止損比例（e.g. 0.03 = 3%）；ATR 模式下作為下限
    vb       : 垂直屏障寬度（bar 數）
    atr      : ATR Series（選填）；有值時改用 atr_mult×ATR/close 計算屏障
    atr_mult : ATR 倍數（預設 2.0）；pt/sl 作為最小下限

Returns:
    pd.Series，index = events，values ∈ {-1, 0, +1}
"""
from __future__ import annotations
import pandas as pd


def label_triple_barrier(
    close: pd.Series,
    events: pd.DatetimeIndex,
    pt: float,
    sl: float,
    vb: int,
    atr: pd.Series | None = None,
    atr_mult: float = 2.0,
) -> pd.Series:
    """Triple Barrier Labeling。

    atr 有值時，屏障距離改為 atr_mult × ATR[t0] / entry（ATR 自適應），
    pt/sl 退化為最小下限，避免低波動期屏障過窄。
    """
    labels = {}
    for t0 in events:
        if t0 not in close.index:
            labels[t0] = 0
            continue
        i0    = close.index.get_loc(t0)
        entry = close.iloc[i0]

        if atr is not None and t0 in atr.index:
            atr_val = float(atr.loc[t0])
            if atr_val > 0 and entry > 0:
                dist = atr_mult * atr_val / entry
                pt_eff = max(pt, dist)
                sl_eff = max(sl, dist)
            else:
                pt_eff, sl_eff = pt, sl
        else:
            pt_eff, sl_eff = pt, sl

        upper  = entry * (1 + pt_eff)
        lower  = entry * (1 - sl_eff)
        end    = min(i0 + vb, len(close) - 1)
        window = close.iloc[i0 + 1 : end + 1]

        label = 0
        for price in window:
            if price >= upper:
                label = 1
                break
            if price <= lower:
                label = -1
                break
        labels[t0] = label

    return pd.Series(labels, name="label")
