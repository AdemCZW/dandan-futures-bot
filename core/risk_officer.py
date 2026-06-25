"""風控官 /risk-officer — 倉位大小、停損停利、限額管理。

策略只說「想不想進場」，能不能進、進多少、何時被迫出場，由這裡決定。
這層是把「漂亮回測」和「不會一夜歸零」分開的關鍵。
"""
from dataclasses import dataclass
from typing import List, Optional


def kelly_fraction(
    pnl_list: List[float],
    min_trades: int = 20,
    half_kelly: bool = True,
    max_kelly: float = 0.5,
) -> Optional[float]:
    """Kelly Criterion 最佳倉位比例。

    f* = p - q/b，其中 p=勝率, q=1-p, b=平均盈/虧比。
    half_kelly=True（預設）回傳 f*/2 以降低波動。
    回傳值夾在 [0, max_kelly]；樣本不足時回傳 None（保守退回預設）。
    """
    if len(pnl_list) < min_trades:
        return None

    wins   = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]

    if not wins:
        return 0.0

    p = len(wins) / len(pnl_list)
    q = 1.0 - p
    avg_win  = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

    if avg_loss == 0.0:
        f = 1.0
    else:
        b = avg_win / avg_loss
        f = p - q / b

    if half_kelly:
        f /= 2.0

    return max(0.0, min(f, max_kelly))


@dataclass
class RiskDecision:
    allow: bool        # 是否准許進場
    quantity: float    # 准許的下單數量（base asset，如 BTC）
    reason: str = ""


class RiskOfficer:
    def __init__(self, cfg):
        self.cfg = cfg
        self._daily_start_equity = None
        self._daily_key = None

    def mark_bar(self, ts, equity: float) -> None:
        """每根 K 線推進時呼叫：在日界用「當日第一根的總權益」當單日熔斷基準。

        回測引擎每根都呼叫，使熔斷以當日開盤權益為準，而非延遲到當日第一筆
        進場時才登記（那會在早盤已虧時把基準壓低、放寬保護）。未呼叫此方法的
        呼叫端（如 run_live）會退回 check_entry 內的延遲登記，與舊版相容。
        """
        day = str(ts)[:10]
        if self._daily_key != day:
            self._daily_key = day
            self._daily_start_equity = equity

    def position_size(self, equity: float, price: float, stop_price: float,
                      kelly_pct: Optional[float] = None) -> float:
        """固定比例風險法：本筆最多虧 risk_per_trade，反推可下數量。

        同時受 max_position_pct（單倉佔比上限）限制。
        kelly_pct 有值時以 Kelly 比例取代 cfg.max_position_pct 作為上限。
        """
        risk_amount = equity * self.cfg.risk_per_trade
        per_unit_loss = max(abs(price - stop_price), 1e-9)
        qty_by_risk = risk_amount / per_unit_loss

        leverage = max(getattr(self.cfg, "futures_leverage", 1), 1)
        pct = kelly_pct if kelly_pct is not None else self.cfg.max_position_pct
        max_notional = equity * pct * leverage
        qty_by_cap = max_notional / price

        return max(min(qty_by_risk, qty_by_cap), 0.0)

    def _stop_distance(self, price: float, atr) -> float:
        """停損距離：有 atr → atr_mult_sl×ATR（波動度自適應）；否則退回 stop_loss_pct×price。"""
        if atr is not None and atr > 0:
            return self.cfg.atr_mult_sl * atr
        return self.cfg.stop_loss_pct * price

    def check_entry(self, equity: float, price: float, ts, direction: int = 1,
                    atr=None, kelly_pct: Optional[float] = None) -> RiskDecision:
        """direction=+1 做多 / -1 做空（停損方向相反）。

        atr 有值時用 ATR 停損距離反推倉位（波動度歸一化）；atr=None 退回固定百分比，與舊版相同。
        """
        # 單日虧損熔斷
        day = str(ts)[:10]
        if self._daily_key != day:
            self._daily_key = day
            self._daily_start_equity = equity
        if self._daily_start_equity:
            dd = (equity - self._daily_start_equity) / self._daily_start_equity
            if dd <= -self.cfg.max_daily_loss_pct:
                return RiskDecision(False, 0.0, "觸發單日虧損熔斷，今日停手")

        # 做多停損在下方、做空停損在上方；距離由 ATR 或固定百分比決定
        dist = self._stop_distance(price, atr)
        stop_price = price - dist if direction == 1 else price + dist
        qty = self.position_size(equity, price, stop_price, kelly_pct=kelly_pct)
        if qty <= 0:
            return RiskDecision(False, 0.0, "風控算出倉位為 0")
        return RiskDecision(True, qty, "ok")

    def update_trailing_stop(self, prev_stop: float, extreme_since_entry: float,
                             atr, direction: int = 1) -> float:
        """Chandelier 追蹤停損：多單只升不降、空單只降不升（保護趨勢單浮盈）。

        多單 stop = max(prev_stop, 進場後最高高價 - chand_mult×ATR)
        空單 stop = min(prev_stop, 進場後最低低價 + chand_mult×ATR)
        atr 缺值（None / <=0）時不更新，回傳 prev_stop。
        """
        if atr is None or atr <= 0:
            return prev_stop
        band = self.cfg.chand_mult * atr
        if direction == 1:
            return max(prev_stop, extreme_since_entry - band)
        return min(prev_stop, extreme_since_entry + band)

    def exit_levels(self, entry_price: float, direction: int = 1, atr=None):
        """回傳 (停損價, 停利價)。direction=+1 做多 / -1 做空。

        atr 有值：停損 = entry ∓ atr_mult_sl×ATR，停利距離 = tp_R_mult×停損距離（恆定 R）。
        atr=None：退回固定百分比（stop_loss_pct / take_profit_pct），與舊版相同。
        """
        if atr is not None and atr > 0:
            sl_dist = self.cfg.atr_mult_sl * atr
            tp_dist = self.cfg.tp_R_mult * sl_dist
            if direction == 1:
                return entry_price - sl_dist, entry_price + tp_dist
            return entry_price + sl_dist, entry_price - tp_dist
        if direction == 1:
            sl = entry_price * (1 - self.cfg.stop_loss_pct)
            tp = entry_price * (1 + self.cfg.take_profit_pct)
        else:
            sl = entry_price * (1 + self.cfg.stop_loss_pct)
            tp = entry_price * (1 - self.cfg.take_profit_pct)
        return sl, tp
