"""執行工程師（合約版）— 在幣安【合約測試網】下單，支援原生做空。

⚠️ 全程 testnet=True，指向 https://testnet.binancefuture.com，**虛擬資金、不碰真錢**。
   與規格硬性規則一致：只連測試網、不提供切換正式網的捷徑。
   合約測試網金鑰與現貨測試網【完全獨立】，需在 testnet.binancefuture.com 另外產生。

與現貨版 ExecutionEngineer 的差異：
  - 用 futures_* API；可開空單（SELL 開倉），現貨做不到。
  - 平倉用 reduceOnly 市價單（只減倉、不會反向開過頭）。
  - 預設槓桿 1（不放大），降低爆倉風險。資金費率／強制平倉等合約細節不在本模板範圍。
"""
from decimal import Decimal, ROUND_DOWN


class FuturesExecutionEngineer:
    def __init__(self, client, symbol: str, leverage: int = 1, set_leverage: bool = True):
        self.client = client
        self.symbol = symbol
        self.leverage = leverage
        self._filters = self._parse_filters(client.futures_exchange_info(), symbol)
        if set_leverage:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)

    @staticmethod
    def _parse_filters(info: dict, symbol: str) -> dict:
        """從 futures_exchange_info 解析某 symbol 的精度/最小量。純函式，方便測試。"""
        syms = [s for s in info.get("symbols", []) if s.get("symbol") == symbol]
        if not syms:
            raise ValueError(f"合約測試網找不到交易對：{symbol}（請確認拼字與是否上架）")
        f = {x["filterType"]: x for x in syms[0]["filters"]}
        # 合約的 MIN_NOTIONAL 欄位名是 "notional"；保險起見也接受 "minNotional"
        nf = f.get("MIN_NOTIONAL", f.get("NOTIONAL", {}))
        notional = nf.get("notional", nf.get("minNotional", "0"))
        return {
            "step_size": Decimal(f["LOT_SIZE"]["stepSize"]),
            "min_qty": Decimal(f["LOT_SIZE"]["minQty"]),
            "tick_size": Decimal(f["PRICE_FILTER"]["tickSize"]),
            "min_notional": Decimal(notional),
        }

    def round_qty(self, qty: float) -> str:
        step = self._filters["step_size"]
        q = (Decimal(str(qty)) / step).to_integral_value(ROUND_DOWN) * step
        return format(q, "f")

    def round_price(self, price: float) -> str:
        tick = self._filters["tick_size"]
        p = (Decimal(str(price)) / tick).to_integral_value(ROUND_DOWN) * tick
        return format(p, "f")

    def valid_order(self, qty: float, price: float) -> tuple[bool, str]:
        q = Decimal(self.round_qty(qty))
        if q < self._filters["min_qty"]:
            return False, f"數量 {q} 低於最小下單量 {self._filters['min_qty']}"
        notional = q * Decimal(str(price))
        if notional < self._filters["min_notional"]:
            return False, f"名目金額 {notional} 低於最小值 {self._filters['min_notional']}"
        return True, "ok"

    def order_params(self, side: str, qty: float, reduce_only: bool = False) -> dict:
        """建立 futures_create_order 參數（純函式，方便測試）。side='BUY'/'SELL'。"""
        p = {"symbol": self.symbol, "side": side, "type": "MARKET",
             "quantity": self.round_qty(qty)}
        if reduce_only:
            p["reduceOnly"] = "true"        # 平倉：只減倉
        return p

    def open_long(self, qty: float):
        return self.client.futures_create_order(**self.order_params("BUY", qty))

    def open_short(self, qty: float):
        return self.client.futures_create_order(**self.order_params("SELL", qty))

    def close(self, qty: float, current_dir: int):
        """平掉現有部位：多單→SELL reduceOnly；空單→BUY reduceOnly。"""
        side = "SELL" if current_dir == 1 else "BUY"
        return self.client.futures_create_order(**self.order_params(side, qty, reduce_only=True))

    # ── 交易所掛單式停損/停利（硬停損：bot 當機/熔斷期間仍由交易所守護）──
    def stop_order_params(self, current_dir: int, trigger_price: float,
                          order_type: str = "STOP_MARKET") -> dict:
        """建立 STOP_MARKET / TAKE_PROFIT_MARKET 條件單參數（純函式，方便測試）。

        side 永遠是「平倉側」（多單→SELL、空單→BUY）；觸發方向由 order_type 決定：
          - STOP_MARKET：價格不利穿越 stopPrice 觸發（多單跌破、空單突破）→ 停損
          - TAKE_PROFIT_MARKET：價格有利穿越 stopPrice 觸發（多單突破、空單跌破）→ 停利
        closePosition='true'：整倉市價平、倉位歸零後幣安自動撤銷此單（免管 qty 精度、scale-out 後也對）。
        workingType='CONTRACT_PRICE'：以最新成交價判定，對齊本地軟停損（用 close 比價）。
        """
        side = "SELL" if current_dir == 1 else "BUY"
        return {
            "symbol": self.symbol, "side": side, "type": order_type,
            "stopPrice": self.round_price(trigger_price),
            "closePosition": "true", "workingType": "CONTRACT_PRICE",
        }

    def place_stop(self, current_dir: int, stop_price: float):
        """掛保護性停損單（STOP_MARKET, closePosition）。回傳含 orderId 的下單回應。"""
        return self.client.futures_create_order(
            **self.stop_order_params(current_dir, stop_price, "STOP_MARKET"))

    def place_take_profit(self, current_dir: int, tp_price: float):
        """掛停利單（TAKE_PROFIT_MARKET, closePosition）。回傳含 orderId 的下單回應。"""
        return self.client.futures_create_order(
            **self.stop_order_params(current_dir, tp_price, "TAKE_PROFIT_MARKET"))

    def cancel_order(self, order_id):
        """撤單（薄包裝）。已成交/不存在的單會由 client 丟錯，由呼叫端決定是否容忍。"""
        return self.client.futures_cancel_order(symbol=self.symbol, orderId=order_id)

    def cancel_all_stops(self):
        """撤掉本 symbol 所有掛單（平倉收尾用，清乾淨殘留條件單）。"""
        return self.client.futures_cancel_all_open_orders(symbol=self.symbol)

    def open_orders(self) -> list:
        """本 symbol 目前掛單清單（重啟對帳用）。"""
        return self.client.futures_get_open_orders(symbol=self.symbol)

    def position_amt(self) -> float:
        """帶號持倉量：+多 / -空 / 0。"""
        info = self.client.futures_position_information(symbol=self.symbol)
        for p in info:
            if p.get("symbol") == self.symbol:
                return float(p.get("positionAmt", 0.0))
        return 0.0

    def balance(self, asset: str = "USDT") -> float:
        for b in self.client.futures_account_balance():
            if b.get("asset") == asset:
                return float(b.get("balance", 0.0))
        return 0.0

    def mark_price(self) -> float:
        return float(self.client.futures_symbol_ticker(symbol=self.symbol)["price"])
