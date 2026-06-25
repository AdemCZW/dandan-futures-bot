"""PurgedKFold — 時序感知 CV，防止 label overlap 造成資料洩漏。

核心概念（AFML chap. 7）：
  1. Purge：把 train set 中 label 結束時間落入 test 視窗的樣本移除。
  2. Embargo：test 結束後再排除 pct_embargo 比例的 train 樣本，
              避免特徵序列相關性造成的近似洩漏。

Args:
    n_splits    : fold 數（同 KFold）
    t1          : pd.Series，index = 樣本時間，value = label 結束時間
    pct_embargo : embargo 比例（佔整體樣本數的百分比，如 0.01 = 1%）
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.model_selection import KFold


class PurgedKFold:
    def __init__(self, n_splits: int = 5, t1: pd.Series | None = None,
                 pct_embargo: float = 0.0):
        self.n_splits     = n_splits
        self.t1           = t1
        self.pct_embargo  = pct_embargo

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(self, X: pd.DataFrame, y=None, groups=None):
        if self.t1 is None:
            raise ValueError("t1 must be provided")
        idx    = np.arange(len(X))
        kf     = KFold(n_splits=self.n_splits)
        embargo_n = int(len(X) * self.pct_embargo)

        for _, test_idx in kf.split(idx):
            test_start = X.index[test_idx[0]]
            test_end   = X.index[test_idx[-1]]

            # purge: remove train samples whose label-end bleeds into test window
            # embargo: remove train samples just after test_end
            embargo_cutoff = (X.index[min(test_idx[-1] + embargo_n, len(X) - 1)]
                              if embargo_n > 0 else test_end)

            train_idx = []
            for i in range(len(X)):
                if i in set(test_idx):
                    continue
                t_start_i = X.index[i]
                t_end_i   = self.t1.iloc[i]
                # purge: label bleeds into test
                if t_end_i > test_start and t_start_i < test_end:
                    continue
                # embargo: just after test window
                if test_end < t_start_i <= embargo_cutoff:
                    continue
                train_idx.append(i)

            yield np.array(train_idx), test_idx
