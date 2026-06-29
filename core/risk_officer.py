"""風控官 /risk-officer — 倉位大小、停損停利、限額管理。

策略只說「想不想進場」，能不能進、進多少、何時被迫出場，由這裡決定。
這層是把「漂亮回測」和「不會一夜歸零」分開的關鍵。
"""
import math
from dataclasses import dataclass
from typing import List, Optional


def _wilson_lower_bound(p_hat: float, n: int, z: float = 1.0) -> float:
    """二項比例的 Wilson 信賴區間下界（單邊）。

    小樣本時 p_hat（觀察勝率）噪音極大，直接拿去算 Kelly 會系統性高估倉位。
    用下界當「保守勝率」：樣本越少、下界離 p_hat 越遠（越保守）；樣本越多越接近 p_hat。
    z=1.0 約一個標準誤；z 越大越保守。
    """
    if n <= 0:
        return 0.0
    denom = 1.0 + z * z / n
    centre = p_hat + z * z / (2.0 * n)
    margin = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n))
    return (centre - margin) / denom


def kelly_fraction(
    pnl_list: List[float],
    min_trades: int = 30,
    half_kelly: bool = True,
    max_kelly: float = 0.5,
    z: float = 1.0,
) -> Optional[float]:
    """Kelly Criterion 最佳倉位比例（小樣本保守版，OPT-15）。

    f* = p - q/b，其中 p=勝率, q=1-p, b=平均盈/虧比。
    - 勝率 p 取 Wilson 信賴下界（z 個標準誤），小樣本不高估。
    - half_kelly=True（預設）回傳 f*/2 降低波動。
    - min_trades 預設 30（20 筆的 p、b 標準誤過大）。
    - 無虧損樣本（losses 為空）→ 回 None（無法估盈虧比，保守退回 budget），
      不再給滿格 f=1.0（原本的脆弱分支）。
    回傳值夾在 [0, max_kelly]；樣本不足或無法估計時回 None。
    """
    if len(pnl_list) < min_trades:
        return None

    wins   = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]

    if not wins:
        return 0.0
    if not losses:
        return None                       # 無虧損樣本 → 估不出盈虧比 → 保守退回 budget

    p = _wilson_lower_bound(len(wins) / len(pnl_list), len(pnl_list), z)
    q = 1.0 - p
    avg_win  = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
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
        self._equity_peak = None   # 運行期間淨值高點（峰值回撤熔斷用）

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
        kelly_pct 有值時取「Kelly 與 max_position_pct 的較小者」作為上限——Kelly 只會「收緊」、
        永遠不超過 --budget 換算的 max_position_pct（避免 Kelly 比例脫鉤膨脹到數倍餘額）。
        """
        risk_amount = equity * self.cfg.risk_per_trade
        per_unit_loss = max(abs(price - stop_price), 1e-9)
        qty_by_risk = risk_amount / per_unit_loss

        leverage = max(getattr(self.cfg, "futures_leverage", 1), 1)
        # Kelly 只在「有正訊號且比 budget 小」時縮倉；Kelly≤0（負期望/無訊號）退回 budget 上限，
        # 不把倉位歸零（避免一啟用 Kelly 就讓負期望的 bot 完全停止交易）。
        if kelly_pct is not None and kelly_pct > 0:
            pct = min(kelly_pct, self.cfg.max_position_pct)
        else:
            pct = self.cfg.max_position_pct
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
        # 峰值回撤熔斷（從運行期最高淨值跌落超過閾值 → 全停）
        if self._equity_peak is None or equity > self._equity_peak:
            self._equity_peak = equity
        max_dd = getattr(self.cfg, 'max_peak_drawdown_pct', 0.20)
        if max_dd > 0 and self._equity_peak:
            peak_dd = (equity - self._equity_peak) / self._equity_peak
            if peak_dd <= -max_dd:
                return RiskDecision(
                    False, 0.0,
                    f"觸發峰值回撤熔斷（從高點 {self._equity_peak:.0f} 下跌 {abs(peak_dd)*100:.1f}%）"
                )

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

        # 清算距離守衛（OPT-18）：停損距離佔比 ≥ 清算距離 → 停損會在強平後才觸發、形同失效。
        # 拒單（這種行情該降槓桿或不進，而非帶著無效停損進場）。1x 時清算距離≈99.5%，等同不生效。
        if getattr(self.cfg, "liq_guard_enabled", True) and price > 0:
            lev = max(int(getattr(self.cfg, "futures_leverage", 1)), 1)
            maint = float(getattr(self.cfg, "maint_margin_rate", 0.005))
            liq_dist_pct = max(1.0 / lev - maint, 0.0)
            if lev > 1 and (dist / price) >= liq_dist_pct:
                return RiskDecision(
                    False, 0.0,
                    f"ATR 停損距離 {dist / price * 100:.1f}% ≥ 清算距離 {liq_dist_pct * 100:.1f}%"
                    f"（{lev}x），停損將失效，拒單（建議降槓桿或等波動收斂）")

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

    def check_scale_out(
        self,
        entry_price: float,
        current_price: float,
        sl_dist: float,
        direction: int,
        already_scaled: bool,
        scale_r: float = 0.5,
    ) -> bool:
        """浮盈達到 scale_r 倍 R 時回傳 True，表示應部分獲利了結。

        sl_dist  進場時的停損距離（原始，不隨 Chandelier 移動）
        direction +1 多 / -1 空 / 0 空手
        already_scaled 本輪已執行過 scale-out 時回傳 False（防重複）
        """
        if already_scaled or direction == 0 or sl_dist <= 0:
            return False
        floating_r = (current_price - entry_price) * direction / sl_dist
        return floating_r >= scale_r

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
