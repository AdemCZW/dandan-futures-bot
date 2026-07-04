"""回測工程師 /backtester — 歷史回測、績效分析。

把策略 + 風控套到歷史 K 線上，逐根模擬，算出績效指標。
重點是「誠實」：含手續費、用收盤後信號、不偷看未來。

⚠️ 回測漂亮 ≠ 實盤會賺。Threads 上 _xjhox 的提醒是對的：
   - 樣本要夠多（跨牛熊、上千筆交易），統計才有意義
   - 小心過度擬合（參數調到剛好貼合歷史）
   下面的 metrics 只是檢查最低門檻，不是賺錢保證。
"""
from dataclasses import dataclass, field
import numpy as np
import pandas as pd
from core.quant_researcher import Strategy
from core.risk_officer import RiskOfficer
from ml.ml_filter import extract_features, signal_proba


def interval_to_minutes(interval: str) -> float:
    """幣安 K 線週期字串 → 分鐘數。'15m'→15、'1h'→60、'4h'→240、'1d'→1440。

    未知格式 → 退回 60（視為 1h），不拋例外（年化只是相對比較，壞掉不該中斷回測）。
    """
    s = str(interval).strip().lower()
    units = {"m": 1.0, "h": 60.0, "d": 1440.0, "w": 10080.0}
    try:
        return float(s[:-1]) * units[s[-1]]
    except (ValueError, KeyError, IndexError):
        return 60.0


def bars_per_year(interval: str) -> float:
    """一年有幾根該週期的 K 棒（Sharpe 年化用）。365×24×60 / 週期分鐘數。"""
    return 365.0 * 24.0 * 60.0 / interval_to_minutes(interval)


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list = field(default_factory=list)
    interval: str = "1h"   # K 線週期，供 Sharpe 年化（OPT-09：年化基準須隨週期，不可用樣本長度）

    @property
    def total_return(self) -> float:
        e = self.equity_curve
        return e.iloc[-1] / e.iloc[0] - 1 if len(e) else 0.0

    @property
    def max_drawdown(self) -> float:
        e = self.equity_curve
        peak = e.cummax()
        return float(((e - peak) / peak).min()) if len(e) else 0.0

    @property
    def win_rate(self) -> float:
        wins = [t for t in self.trades if t["pnl"] > 0]
        return len(wins) / len(self.trades) if self.trades else 0.0

    @property
    def expectancy(self) -> float:
        """每筆平倉交易的平均盈虧（含費）。短線評估核心：>0 才有正期望。"""
        return sum(t["pnl"] for t in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        """毛利 / 毛損。>1 才賺錢；無虧損交易→+inf（有獲利）或 0（完全無交易）。

        這比純勝率誠實：高勝率配差盈虧比會讓 profit_factor 跌破 1，當場現形。
        """
        gross_profit = sum(t["pnl"] for t in self.trades if t["pnl"] > 0)
        gross_loss = -sum(t["pnl"] for t in self.trades if t["pnl"] < 0)
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def sharpe(self) -> float:
        e = self.equity_curve
        if len(e) < 3:
            return 0.0
        ret = e.pct_change().dropna()
        if ret.std() == 0:
            return 0.0
        # 年化用「每年 K 棒數」開根號，而非樣本長度——否則同策略樣本越長 Sharpe 假性越高、
        # 且 15m 與 1h 不可比（OPT-09）。bars_per_year 由 interval 推導。
        return float(ret.mean() / ret.std() * np.sqrt(bars_per_year(self.interval)))

    def summary(self) -> str:
        return (
            f"交易筆數 : {len(self.trades)}\n"
            f"總報酬   : {self.total_return:+.2%}\n"
            f"最大回撤 : {self.max_drawdown:.2%}\n"
            f"勝率     : {self.win_rate:.1%}\n"
            f"Sharpe   : {self.sharpe:.2f}"
        )


def run_backtest(df: pd.DataFrame, strategy: Strategy, risk: RiskOfficer, cfg,
                 trace: list | None = None,
                 ml_model=None, ml_threshold: float = 0.55) -> BacktestResult:
    """逐根模擬。支援多/空：策略回傳目標倉位（+1/0/-1），引擎自動進出場。

    僅做多策略（allow_short=False、只回 0/＋1）的路徑與舊版逐行等價。
    現金帳含雙邊手續費，故權益曲線（總報酬/回撤/Sharpe）對多空皆正確。

    trace：可選。傳入一個 list 時，逐根附上「決策軌跡」（指標/信號/風控/動作），
    供前端攤開每個位置的決策。此 hook 只讀值與 append，不影響任何計算或結果
    （trace=None 時與未加 hook 完全等價）。

    ml_model：可選（撿起 #55）。給定時，只在「新進場」（target 從非該方向轉為
    +1/-1）套用機率門檻——語意與 run_live_futures.py 的即時 ML 閘門一致：不擋
    既有倉位的續抱/出場。用 df.loc[:ts]（只到當根為止）擷取特徵，因果、無未來
    洩漏。ml_model=None（預設）→ 完全不影響現有行為，是走 walk-forward 錦標賽
    公平比較「有無 ML 過濾」的唯一入口（否則要另外寫一套評估邏輯，兩邊方法論
    可能悄悄不一致）。
    """
    df = strategy.prepare(df).dropna()
    equity = cfg.start_equity
    cash = equity
    position = 0          # +1 多 / -1 空 / 0 空手
    qty = 0.0             # 絕對數量（base asset）
    entry_price = 0.0
    sl = tp = 0.0
    highest = lowest = 0.0   # 進場後極值（Chandelier 追蹤停損用）
    chand_mult = getattr(cfg, "chand_mult", None)
    fee = cfg.fee_rate
    slip = getattr(cfg, "slippage", 0.0)   # 滑點：成交價往不利方向偏。預設 0＝不改既有結果
    allow_short = getattr(strategy, "allow_short", False)
    curve, trades = [], []

    # OPT-08：成交延遲（訊號 i 根、成交 i+fill_lag 根 open，對齊實盤）+ 資金費率逐根攤提。
    fill_lag = max(int(getattr(cfg, "fill_lag", 0)), 0)
    opens = df["open"].to_numpy() if fill_lag > 0 else None
    n_bars = len(df)
    fr8 = float(getattr(cfg, "funding_rate_per_8h", 0.0))
    bars_per_8h = (8 * 60) / interval_to_minutes(getattr(cfg, "interval", "1h"))
    funding_per_bar = fr8 / bars_per_8h if bars_per_8h > 0 else 0.0

    def close_position(px, side, ts):
        """以 px（含滑點）平掉目前倉位，記一筆交易，回到空手。"""
        nonlocal cash, position, qty, entry_price
        if position == 1:
            fill = px * (1 - slip)                           # 賣出滑點：成交更差
            proceeds = qty * fill * (1 - fee)
            pnl = proceeds - qty * entry_price * (1 + fee)   # 含進場+出場手續費，與空單對稱
            cash += proceeds
        else:  # position == -1，回補空單
            fill = px * (1 + slip)                           # 買回滑點：成交更差
            cost = qty * fill * (1 + fee)
            pnl = qty * entry_price * (1 - fee) - cost       # 多空對稱、含雙邊手續費
            cash -= cost
        trades.append({"ts": ts, "side": side, "price": fill,
                       "qty": qty, "pnl": pnl, "dir": position})
        position = 0
        qty = 0.0

    _IND = ("ema_fast", "ema_slow", "rsi", "atr", "zscore")

    for i, (ts, row) in enumerate(df.iterrows()):
        price = row["close"]
        # OPT-08：訊號驅動的進出場成交價。fill_lag>0 改用第 i+lag 根 open；無未來根→退回 close。
        if fill_lag > 0 and i + fill_lag < n_bars:
            signal_fill = float(opens[i + fill_lag])
        else:
            signal_fill = price
        # 每根推進：以當日首根的總權益為單日熔斷基準
        risk.mark_bar(ts, cash + position * qty * price)

        step = None
        if trace is not None:                          # 決策軌跡 hook（只讀值，不影響計算）
            step = {"ts": str(ts), "close": round(float(price), 2),
                    "high": round(float(row["high"]), 2), "low": round(float(row["low"]), 2),
                    "volume": round(float(row["volume"]), 2) if "volume" in row else None,
                    "ind": {k: (None if pd.isna(row[k]) else round(float(row[k]), 4))
                            for k in _IND if k in row},
                    "pos_before": int(position), "risk": None, "actions": []}

        # 1) 先檢查持倉的停損/停利（用本根高低價判斷有沒有被觸發）
        if position != 0:
            if position == 1:
                hit_sl, hit_tp = row["low"] <= sl, row["high"] >= tp
            else:
                hit_sl, hit_tp = row["high"] >= sl, row["low"] <= tp
            exit_price = sl if hit_sl else (tp if hit_tp else None)
            if exit_price is not None:
                close_position(exit_price, "exit_sltp", ts)
                if step is not None:
                    step["actions"].append({"act": "exit_sltp", "price": round(float(exit_price), 2),
                                            "hit": "sl" if hit_sl else "tp"})

        # 2) 策略目標倉位（吃已收完這根；position 已反映停損後狀態）
        target = strategy.signal(row, position)
        if target == -1 and not allow_short:
            target = 0     # 安全網：不支援做空的策略，-1 一律當平倉

        # ML 過濾閘門（撿起 #55）：只在「新進場」套用機率門檻，續抱/出場不受影響
        # （語意與 run_live_futures.py 即時閘門一致）。df.loc[:ts] 只用到當根為止
        # 的資料擷取特徵，因果、無未來洩漏。ml_model=None（預設）完全不影響行為。
        if ml_model is not None and target in (1, -1) and target != position:
            try:
                feats = extract_features(df.loc[:ts], pd.DatetimeIndex([ts]))
                p = signal_proba(ml_model, feats)
                if p < ml_threshold:
                    target = 0
            except Exception:                      # noqa: BLE001 — 過濾失敗不擋回測，退回無過濾行為
                pass

        if step is not None:
            step["target"] = int(target)

        # 3) 對齊目標倉位：先平反向倉，再開到目標方向
        if target != position:
            if position != 0:
                close_position(signal_fill, "exit_signal", ts)   # OPT-08：fill_lag 時於下一根成交
                if step is not None:
                    step["actions"].append({"act": "exit_signal", "price": round(float(signal_fill), 2)})
            # 進場用「這根已收盤」的 ATR 設停損停利與部位大小（缺 atr 欄→None→退回固定百分比）
            atr_val = row["atr"] if "atr" in row else None
            if target == 1:
                decision = risk.check_entry(cash, price, ts, direction=1, atr=atr_val)
                if step is not None:
                    step["risk"] = {"allow": bool(decision.allow),
                                    "qty": round(float(decision.quantity), 6), "reason": decision.reason}
                if decision.allow:
                    buy_qty = decision.quantity
                    fill = signal_fill * (1 + slip)            # 買進滑點（OPT-08：fill_lag 用下一根 open）
                    cost = buy_qty * fill * (1 + fee)
                    if cost <= cash and buy_qty > 0:
                        cash -= cost
                        position, qty, entry_price = 1, buy_qty, fill
                        highest = float(row["high"])      # Chandelier 進場後最高高價起點
                        sl, tp = risk.exit_levels(fill, direction=1, atr=atr_val)
                        trades.append({"ts": ts, "side": "entry", "price": fill,
                                       "qty": qty, "pnl": 0.0, "dir": 1})
                        if step is not None:
                            step["actions"].append({"act": "entry", "price": round(float(fill), 2),
                                                    "qty": round(float(qty), 6),
                                                    "sl": round(float(sl), 2), "tp": round(float(tp), 2)})
            elif target == -1 and allow_short:
                decision = risk.check_entry(cash, price, ts, direction=-1, atr=atr_val)
                if step is not None:
                    step["risk"] = {"allow": bool(decision.allow),
                                    "qty": round(float(decision.quantity), 6), "reason": decision.reason}
                if decision.allow and decision.quantity > 0:
                    sell_qty = decision.quantity
                    fill = signal_fill * (1 - slip)            # 放空滑點（OPT-08：fill_lag 用下一根 open）
                    cash += sell_qty * fill * (1 - fee)        # 放空收到（虛擬）價金
                    position, qty, entry_price = -1, sell_qty, fill
                    lowest = float(row["low"])        # Chandelier 進場後最低低價起點
                    sl, tp = risk.exit_levels(fill, direction=-1, atr=atr_val)
                    trades.append({"ts": ts, "side": "entry_short", "price": fill,
                                   "qty": qty, "pnl": 0.0, "dir": -1})
                    if step is not None:
                        step["actions"].append({"act": "entry_short", "price": round(float(fill), 2),
                                                "qty": round(float(qty), 6),
                                                "sl": round(float(sl), 2), "tp": round(float(tp), 2)})

        # 4) Chandelier 追蹤停損：用「這根已收盤」的極值與 ATR 更新停損，供「下一根」判定觸發
        #    （只升不降/只降不升、用上一根算出的 stop 判定本根，故無 look-ahead）。
        if position != 0 and chand_mult is not None and "atr" in row and not pd.isna(row["atr"]):
            atr_now = float(row["atr"])
            if position == 1:
                highest = max(highest, float(row["high"]))
                sl = risk.update_trailing_stop(sl, highest, atr_now, 1)
            else:
                lowest = min(lowest, float(row["low"]))
                sl = risk.update_trailing_stop(sl, lowest, atr_now, -1)

        # OPT-08：資金費率逐根攤提（持多付、持空收；funding_per_bar=0 時 no-op）
        if position != 0 and funding_per_bar != 0.0:
            cash -= position * abs(qty * price) * funding_per_bar

        equity = cash + position * qty * price
        if step is not None:
            if not step["actions"]:
                step["actions"].append({"act": "hold" if position != 0 else "flat"})
            step["pos_after"] = int(position)
            step["equity"] = round(float(equity), 2)
            trace.append(step)
        curve.append((ts, equity))

    # 收尾：最後若還有持倉，以最後價平倉。並用平倉後的「已實現現金」修正權益曲線末點——
    # 否則末點停在迴圈內以市值計、未扣出場手續費的浮動值，會讓 total_return/Sharpe 系統性高估。
    if position != 0:
        close_position(df["close"].iloc[-1], "exit_final", df.index[-1])
        if curve:
            curve[-1] = (curve[-1][0], cash)   # position 已歸 0，cash 即已實現權益

    eq = pd.Series([v for _, v in curve], index=[t for t, _ in curve])
    # 只保留真正平倉的交易來算勝率（entry / entry_short 不算）
    closed = [t for t in trades if t["side"].startswith("exit")]
    return BacktestResult(equity_curve=eq, trades=closed,
                          interval=getattr(cfg, "interval", "1h"))
