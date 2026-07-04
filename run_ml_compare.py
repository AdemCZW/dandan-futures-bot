"""ML Filter 有/無對比回測。

對四個 bot 配置分別跑：
  基準（無 filter） vs ML filter（proba ≥ threshold 才准進場）

用公開主網合約 API 抓 2 年真實資料，不需要 API key。

用法：
    python run_ml_compare.py                  # 預設四台 bot 配置
    python run_ml_compare.py --threshold 0.6  # 較嚴格的閾值
    python run_ml_compare.py --days 365       # 只跑 1 年
"""
from __future__ import annotations
import argparse
import json
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest, BacktestResult
from ml.ml_filter import extract_features, load_filter, signal_proba

FAPI    = "https://fapi.binance.com/fapi/v1/klines"
LIMIT   = 1500
MODELS  = {
    "fib_ema":       "models/fib_ema.pkl",
    "fib_channel":   "models/fib_channel.pkl",
    "trend_pullback":"models/trend_pullback.pkl",
}

BOT_CONFIGS = [
    {"name": "Bot1", "strategy": "fib_ema",       "symbol": "SOLUSDT", "interval": "15m"},
    {"name": "Bot2", "strategy": "fib_channel",    "symbol": "SOLUSDT", "interval": "15m"},
    {"name": "Bot3", "strategy": "trend_pullback", "symbol": "ETHUSDT", "interval": "1h"},
    {"name": "Bot4", "strategy": "trend_pullback", "symbol": "SOLUSDT", "interval": "1h"},
]


def fetch_klines(symbol: str, interval: str, days: int) -> pd.DataFrame:
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    rows: list = []
    cur = start_ms
    while cur < end_ms:
        url = (f"{FAPI}?symbol={symbol}&interval={interval}"
               f"&startTime={cur}&endTime={end_ms}&limit={LIMIT}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            batch = json.loads(r.read())
        if not batch:
            break
        rows.extend(batch)
        cur = int(batch[-1][6]) + 1
        time.sleep(0.08)
    cols = ["open_time","open","high","low","close","volume",
            "close_time","qv","trades","tb","tq","ig"]
    df = pd.DataFrame(rows, columns=cols)
    for c in ("open","high","low","close","volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.drop_duplicates("open_time").set_index("open_time")
    df.index = df.index.tz_localize(None)
    return df[["open","high","low","close","volume"]]


class MLFilteredStrategy:
    """包裝任意 Strategy，在新進場前過 ML 機率閘。

    只過濾「從空手→有部位」的進場，出場/換向不攔截。
    """
    def __init__(self, base, model, threshold: float = 0.55):
        self.base      = base
        self.model     = model
        self.threshold = threshold
        self._prepared: pd.DataFrame | None = None
        # 透傳 strategy 的屬性（backtester 會存取這些）
        self.name        = getattr(base, "name", "filtered")
        self.allow_short = getattr(base, "allow_short", False)
        self.regime_pref = getattr(base, "regime_pref", "any")

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        result = self.base.prepare(df)
        self._prepared = result
        return result

    def signal(self, row, position: int) -> int:
        target = self.base.signal(row, position)

        # 非新進場（空手→空手 / 已有倉位的出場或維持）不攔
        if target == position or target == 0:
            return target

        # 從有倉位換向：先平再進，這裡攔「再進」那一步
        # backtester 會先平倉再呼叫一次 check_entry，所以 position 此時仍舊
        # → 實際上 target != 0 且 target != position 就是「想開新倉」

        if self._prepared is None:
            return target   # 還沒跑 prepare，放行

        try:
            t = row.name if hasattr(row, "name") else None
            if t is None or t not in self._prepared.index:
                return target
            X_row = extract_features(self._prepared, pd.DatetimeIndex([t]))
            proba = signal_proba(self.model, X_row)
            if proba < self.threshold:
                return 0    # 機率不夠，攔截進場
        except Exception:
            pass            # 任何異常都放行（保守安全原則）

        return target


@dataclass
class CompareRow:
    name: str; strategy: str; symbol: str; interval: str
    base_ret: float; base_dd: float; base_wr: float
    base_trades: int; base_sharpe: float
    filt_ret: float; filt_dd: float; filt_wr: float
    filt_trades: int; filt_sharpe: float
    blocked: int; threshold: float

    def delta_ret(self) -> float:
        return self.filt_ret - self.base_ret

    def delta_dd(self) -> float:
        return self.filt_dd - self.base_dd   # 負數 = 回撤縮小 = 好

    def delta_wr(self) -> float:
        return self.filt_wr - self.base_wr


def fmt(v: float, pct: bool = True, sign: bool = False) -> str:
    if pct:
        s = f"{v*100:.2f}%"
    else:
        s = f"{v:.3f}"
    if sign and v > 0:
        s = "+" + s
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=None,
                    help="固定閾值（不設則用中位數，即過濾最差 50%% 的訊號）")
    ap.add_argument("--pct", type=float, default=50.0,
                    help="百分位數閾值：只讓 top-(100-pct)%% 的訊號通過（預設 50）")
    ap.add_argument("--days",      type=int,   default=730,
                    help="回測天數（預設 730 = 2 年）")
    args = ap.parse_args()

    results: list[CompareRow] = []

    for cfg in BOT_CONFIGS:
        name     = cfg["name"]
        strat_n  = cfg["strategy"]
        symbol   = cfg["symbol"]
        interval = cfg["interval"]

        model_path = MODELS.get(strat_n, "")
        if not os.path.exists(model_path):
            print(f"[{name}] 找不到模型 {model_path}，跳過")
            continue

        print(f"\n{'='*60}")
        print(f"[{name}] {strat_n} · {symbol} · {interval}")

        print(f"  抓 {args.days} 天 K 線…", end=" ", flush=True)
        df = fetch_klines(symbol, interval, args.days)
        print(f"{len(df)} 根")

        c = Config()
        c.symbol, c.interval, c.strategy = symbol, interval, strat_n

        # ── 基準回測（無 filter）──
        strat_base = build_strategy(strat_n)
        risk_base  = RiskOfficer(c)
        base = run_backtest(df, strat_base, risk_base, c)
        print(f"  [基準]   報酬={fmt(base.total_return)}  "
              f"回撤={fmt(base.max_drawdown)}  "
              f"勝率={fmt(base.win_rate)}  "
              f"交易={len(base.trades)}筆  "
              f"Sharpe={base.sharpe:.3f}")

        # ── 動態計算閾值（百分位數或固定值）──
        model = load_filter(model_path)
        if args.threshold is not None:
            threshold = args.threshold
        else:
            # 用全部訊號的機率分布計算百分位數閾值
            from backtest.vbt_optimize import signals_from_prepared
            strat_tmp  = build_strategy(strat_n)
            prepared   = strat_tmp.prepare(df).dropna()
            le, _, se, _ = signals_from_prepared(prepared, strat_n)
            events = prepared.index[le | se]
            if len(events) > 0:
                X_all  = extract_features(prepared, events)
                probas = model.predict_proba(X_all.values)[:, 1]
                threshold = float(np.percentile(probas, args.pct))
            else:
                threshold = 0.50
        print(f"  [閾值]   {threshold:.4f}（top {100-args.pct:.0f}% 訊號通過）")

        # ── ML filter 回測 ──
        strat_filt = MLFilteredStrategy(build_strategy(strat_n), model, threshold)
        risk_filt  = RiskOfficer(c)
        filt = run_backtest(df, strat_filt, risk_filt, c)
        blocked = len(base.trades) - len(filt.trades)
        print(f"  [filter] 報酬={fmt(filt.total_return)}  "
              f"回撤={fmt(filt.max_drawdown)}  "
              f"勝率={fmt(filt.win_rate)}  "
              f"交易={len(filt.trades)}筆  "
              f"Sharpe={filt.sharpe:.3f}  "
              f"攔截≈{blocked}筆")

        δr = filt.total_return - base.total_return
        δd = filt.max_drawdown - base.max_drawdown
        symbol_r = "↑" if δr > 0 else ("↓" if δr < 0 else "─")
        symbol_d = "↓" if δd < 0 else ("↑" if δd > 0 else "─")
        print(f"  △報酬={fmt(δr,sign=True)}  △回撤={fmt(δd,sign=True)} "
              f"  {symbol_r}報酬 {symbol_d}回撤")

        results.append(CompareRow(
            name=name, strategy=strat_n, symbol=symbol, interval=interval,
            base_ret=base.total_return, base_dd=base.max_drawdown,
            base_wr=base.win_rate,      base_trades=len(base.trades),
            base_sharpe=base.sharpe,
            filt_ret=filt.total_return, filt_dd=filt.max_drawdown,
            filt_wr=filt.win_rate,      filt_trades=len(filt.trades),
            filt_sharpe=filt.sharpe,
            blocked=blocked, threshold=threshold,
        ))

    # ── 總結表 ──
    print(f"\n{'='*60}")
    print(f"{'':6} {'':16} {'基準報酬':>8} {'filter':>8} {'△報酬':>8} "
          f"{'△回撤':>8} {'△勝率':>8} {'攔截筆':>7}")
    for r in results:
        print(f"{r.name:<6} {r.strategy:<16} "
              f"{fmt(r.base_ret):>8} "
              f"{fmt(r.filt_ret):>8} "
              f"{fmt(r.delta_ret(),sign=True):>8} "
              f"{fmt(r.delta_dd(),sign=True):>8} "
              f"{fmt(r.delta_wr(),sign=True):>8} "
              f"{r.blocked:>7}")

    improved = sum(1 for r in results if r.delta_ret() > 0)
    pct_str  = f"top {100-args.pct:.0f}%" if args.threshold is None else f"閾值={args.threshold}"
    print(f"\n結論：{improved}/{len(results)} 個 bot 在 filter 後報酬提升（{pct_str}）")
    if improved < len(results) // 2 + 1:
        print("→ 多數 bot 未受益，不建議接入 live bot")
    else:
        print("→ 多數 bot 受益，可考慮接入 live bot（建議先在測試網驗證）")


if __name__ == "__main__":
    main()
