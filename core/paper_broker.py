"""Paper broker — 本地模擬成交，不連任何交易所、不需金鑰。

抓「真實即時行情」由 market_analyst 負責（公開 K 線免金鑰）；這裡只負責「假裝成交」：
在傳入的市價上、套用手續費與滑點，更新本地模擬的 quote/base 餘額。全程虛擬、零真錢。

僅做多（對應現貨 run_live 的語意）。買進滑點往上、賣出滑點往下，與回測引擎一致。
"""
from __future__ import annotations


class PaperBroker:
    def __init__(self, cfg, quote_start: float | None = None):
        self.cfg = cfg
        self.fee = cfg.fee_rate
        self.slip = getattr(cfg, "slippage", 0.0)
        self.cash = float(quote_start if quote_start is not None else cfg.start_equity)  # quote(USDT)
        self.base = 0.0                                                                  # base(BTC)

    def balance(self, asset: str) -> float:
        if asset == self.cfg.quote_asset:
            return self.cash
        if asset == self.cfg.base_asset:
            return self.base
        return 0.0

    def equity(self, price: float) -> float:
        return self.cash + self.base * price

    def market_buy(self, qty: float, price: float) -> dict:
        """以市價（含買進滑點）買入，自動夾到現金買得起的量。回傳實際成交。"""
        fill = price * (1 + self.slip)
        unit = fill * (1 + self.fee)
        if qty * unit > self.cash:           # 現金不足 → 夾到買得起
            qty = self.cash / unit if unit > 0 else 0.0
        cost = qty * unit
        self.cash -= cost
        self.base += qty
        return {"qty": qty, "fill": fill, "cost": cost}

    def market_sell(self, qty: float, price: float) -> dict:
        """以市價（含賣出滑點）賣出，夾到目前持有量。回傳實際成交。"""
        fill = price * (1 - self.slip)
        qty = min(qty, self.base)
        proceeds = qty * fill * (1 - self.fee)
        self.cash += proceeds
        self.base -= qty
        return {"qty": qty, "fill": fill, "proceeds": proceeds}
