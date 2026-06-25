"""Triple Barrier Labeling（López de Prado, AFML chap. 3）。

每個 entry event 往前掃描，回傳第一個被觸碰的 barrier 的標籤：
  +1  → 上方獲利屏障（price ≥ entry × (1+pt)）
  -1  → 下方止損屏障（price ≤ entry × (1-sl)）
   0  → 垂直屏障（超過 vb 根 bar 都沒觸碰）

Args:
    close   : 收盤價 Series（DatetimeIndex）
    events  : 需要標記的 entry 時間點（DatetimeIndex）
    pt      : 獲利目標比例（e.g. 0.03 = 3%）
    sl      : 止損比例（e.g. 0.03 = 3%）
    vb      : 垂直屏障寬度（bar 數）

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
) -> pd.Series:
    labels = {}
    for t0 in events:
        if t0 not in close.index:
            labels[t0] = 0
            continue
        i0    = close.index.get_loc(t0)
        entry = close.iloc[i0]
        upper = entry * (1 + pt)
        lower = entry * (1 - sl)
        end   = min(i0 + vb, len(close) - 1)
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
