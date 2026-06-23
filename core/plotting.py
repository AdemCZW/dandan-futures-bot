"""視覺化 — 把回測 / 最佳化結果畫成圖（matplotlib，存 PNG）。

純繪圖、不參與任何交易決策。用 Agg backend，適合無視窗環境直接存檔。
標籤刻意用英文，避免不同系統缺 CJK 字型而出現豆腐方塊。
"""
from __future__ import annotations
import matplotlib
matplotlib.use("Agg")            # 無視窗環境：直接存檔，不開 GUI
import matplotlib.pyplot as plt
import numpy as np


def plot_equity(result, path: str = "equity.png", title: str = "Equity curve") -> str:
    """權益曲線 + 回撤雙圖。result 為 BacktestResult。回傳存檔路徑。"""
    eq = result.equity_curve
    if len(eq) == 0:
        raise ValueError("equity_curve 是空的，沒東西可畫")
    peak = eq.cummax()
    dd = (eq - peak) / peak * 100.0

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(eq.index, eq.values, color="#2563eb", lw=1.3)
    ax1.axhline(float(eq.iloc[0]), color="#9ca3af", lw=0.8, ls="--")
    ax1.set_ylabel("Equity")
    ax1.set_title(
        f"{title}   |   return {result.total_return * 100:+.2f}%   "
        f"maxDD {result.max_drawdown * 100:.2f}%   "
        f"win {result.win_rate * 100:.1f}%   Sharpe {result.sharpe:.2f}   "
        f"trades {len(result.trades)}", fontsize=11)
    ax1.grid(alpha=0.25)

    ax2.fill_between(dd.index, dd.values, 0, color="#dc2626", alpha=0.35)
    ax2.set_ylabel("Drawdown %")
    ax2.set_xlabel("Time")
    ax2.grid(alpha=0.25)

    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


def plot_heatmap(df, xcol: str, ycol: str, metric: str = "sharpe",
                 path: str = "heatmap.png", title: str = "") -> str:
    """參數掃描熱圖：xcol × ycol 上的 metric（其餘參數取平均邊際化）。

    一眼看出績效對參數有多敏感——只有一兩格特別亮、隔壁就暗 = 過擬合的味道。
    df 為 sweep() 回傳的表。回傳存檔路徑。
    """
    piv = df.pivot_table(index=ycol, columns=xcol, values=metric, aggfunc="mean")
    piv = piv.sort_index(ascending=True)
    data = np.ma.masked_invalid(piv.values.astype(float))   # 遮掉 -inf/NaN

    fig, ax = plt.subplots(figsize=(8.5, 6))
    cmap = plt.get_cmap("RdYlGn").with_extremes(bad="#d1d5db")   # 被遮的格子畫灰
    im = ax.imshow(data, aspect="auto", cmap=cmap, origin="lower")

    ax.set_xticks(range(len(piv.columns)))
    ax.set_xticklabels([str(c) for c in piv.columns])
    ax.set_yticks(range(len(piv.index)))
    ax.set_yticklabels([str(i) for i in piv.index])
    ax.set_xlabel(xcol)
    ax.set_ylabel(ycol)
    ax.set_title(title or f"{metric} heatmap  ({ycol} x {xcol})", fontsize=11)

    for i in range(data.shape[0]):
        for j in range(data.shape[1]):
            v = data[i, j]
            if v is not np.ma.masked and np.isfinite(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                        fontsize=8, color="#111827")

    fig.colorbar(im, ax=ax, label=metric)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
