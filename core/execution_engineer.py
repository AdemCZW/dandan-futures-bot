"""執行工程師 /execution-engineer — 幣安 API、訂單執行。

只在【測試網】下單。負責：
  1. 讀 symbol filters，把數量/價格修到合法精度（新手最常卡這裡）
  2. 送市價單 / 限價單
  3. 查餘額

⚠️ 這個檔案永遠用 testnet=True。要切實盤是另一回事，且風險自負，
   本模板刻意不提供一鍵切換正式網的捷徑。
"""
from decimal import Decimal, ROUND_DOWN
from binance.client import Client


class ExecutionEngineer:
    def __init__(self, client: Client, symbol: str):
        self.client = client
        self.symbol = symbol
        self._filters = self._load_filters()

    def _load_filters(self) -> dict:
        info = self.client.get_symbol_info(self.symbol)
        if info is None:
            raise ValueError(f"未知的交易對：{self.symbol}（請確認拼字、以及測試網是否上架）")
        f = {x["filterType"]: x for x in info["filters"]}
        return {
            "step_size": Decimal(f["LOT_SIZE"]["stepSize"]),
            "min_qty": Decimal(f["LOT_SIZE"]["minQty"]),
            "tick_size": Decimal(f["PRICE_FILTER"]["tickSize"]),
            "min_notional": Decimal(
                f.get("NOTIONAL", f.get("MIN_NOTIONAL", {})).get("minNotional", "0")
            ),
        }

    def round_qty(self, qty: float) -> str:
        """無條件捨去到 stepSize，回傳「定點字串」。

        刻意不回 float：float 對極小值會變科學記號（如 1.23e-06），python-binance
        以 str() 送單會被幣安以非法字元/精度退單。定點字串保證合法。
        """
        step = self._filters["step_size"]
        q = (Decimal(str(qty)) / step).to_integral_value(ROUND_DOWN) * step
        return format(q, "f")

    def round_price(self, price: float) -> str:
        tick = self._filters["tick_size"]
        p = (Decimal(str(price)) / tick).to_integral_value(ROUND_DOWN) * tick
        return format(p, "f")

    def valid_order(self, qty: float, price: float) -> tuple[bool, str]:
        # 用「修整後」的數量檢查，確保被驗證的與實際送出的是同一個值
        # （否則原始 qty 過關、捨去後名目金額卻跌破 min_notional，仍會被交易所退單）。
        q = Decimal(self.round_qty(qty))
        if q < self._filters["min_qty"]:
            return False, f"數量 {q} 低於最小下單量 {self._filters['min_qty']}"
        notional = q * Decimal(str(price))
        if notional < self._filters["min_notional"]:
            return False, f"名目金額 {notional} 低於最小值 {self._filters['min_notional']}"
        return True, "ok"

    def market_buy(self, qty: float):
        qty = self.round_qty(qty)
        return self.client.create_order(
            symbol=self.symbol, side="BUY", type="MARKET", quantity=qty
        )

    def market_sell(self, qty: float):
        qty = self.round_qty(qty)
        return self.client.create_order(
            symbol=self.symbol, side="SELL", type="MARKET", quantity=qty
        )

    def balance(self, asset: str) -> float:
        b = self.client.get_asset_balance(asset=asset)
        return float(b["free"]) if b else 0.0
