"""WebSocket 版實時進入點（選項）— 用 K 線串流在「K 線一收完」的瞬間觸發決策，
比 30 秒輪詢更即時。下單/風控/狀態邏輯與 run_live.py 相同，只是觸發方式不同。

設計：WS 只當「K 線收完」的觸發器；指標仍用 REST 抓 200 根重算（與輪詢版同一條路徑），
避免自行維護滾動緩衝、降低出錯面。決策邏輯封在 LiveTrader，可離線用假物件測試；
只有 ThreadedWebsocketManager 的 socket 接線需要 live 測試網連線才能完整驗證。

⚠️ run_live.py（輪詢）才是已驗證的預設版本。一樣全程 testnet=True、虛擬資金。
用法：python run_live_ws.py（金鑰設定同 run_live.py）。Ctrl+C 結束。
"""
import traceback
import pandas as pd
from config import Config
from core.market_analyst import make_client, fetch_klines, detect_anomaly
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from core.execution_engineer import ExecutionEngineer
from core.trade_journal import TradeJournal
from core.bot_state import BotState, reconcile

STATE_PATH = "bot_state.json"


class LiveTrader:
    """每根 K 線收完時的決策 + 下單 + 狀態持久化。與 run_live.py 等價，可獨立測試。"""

    def __init__(self, cfg, client, strat, risk, execu, journal):
        self.cfg, self.client, self.strat = cfg, client, strat
        self.risk, self.execu, self.journal = risk, execu, journal
        self.in_position = False
        self.entry_price = self.sl = self.tp = 0.0

    def restore(self) -> None:
        """重啟還原：以交易所餘額為準校正持久化狀態。"""
        cfg = self.cfg
        base0 = self.execu.balance(cfg.base_asset)
        price0 = float(fetch_klines(self.client, cfg.symbol, cfg.interval, 2)["close"].iloc[-1])
        dust = float(self.execu._filters["min_qty"])
        state, msg = reconcile(BotState.load(STATE_PATH), base0, dust, price0, self.risk.exit_levels)
        state.symbol, state.strategy = cfg.symbol, cfg.strategy
        state.save(STATE_PATH)
        self.in_position = state.in_position
        self.entry_price, self.sl, self.tp = state.entry_price, state.sl, state.tp
        print(f"[狀態] {msg}")

    def _flat(self) -> None:
        self.in_position = False
        BotState(symbol=self.cfg.symbol, strategy=self.cfg.strategy).save(STATE_PATH)

    def on_bar_close(self, bar_time) -> None:
        cfg = self.cfg
        df = self.strat.prepare(fetch_klines(self.client, cfg.symbol, cfg.interval, 200)).dropna()
        row = df.iloc[-2]
        price = float(df["close"].iloc[-1])

        # 風控官：持倉中的 SL/TP 保護性出場「永遠先執行」，不受暴量抑制
        if self.in_position and (price <= self.sl or price >= self.tp):
            qty = self.execu.balance(cfg.base_asset)
            if qty > 0:
                self.execu.market_sell(qty)
                self.journal.log("exit_sltp", price, qty, (price - self.entry_price) * qty, ts=bar_time)
                print(f"[{bar_time}] 停損/停利出場 @ {price:.2f}")
            self._flat()

        if detect_anomaly(df.iloc[:-1]):          # 暴量：只跳過新進場/信號，不碰上面的保護性停損
            print(f"[{bar_time}] 偵測到暴量異常，本輪跳過下單")
            return

        position = 1 if self.in_position else 0
        target = self.strat.signal(row, position)

        if not self.in_position and target == 1:
            quote_bal = self.execu.balance(cfg.quote_asset)
            atr_val = float(row["atr"]) if "atr" in row.index and not pd.isna(row["atr"]) else None
            decision = self.risk.check_entry(quote_bal, price, bar_time, atr=atr_val)
            if decision.allow:
                ok, msg = self.execu.valid_order(decision.quantity, price)
                if ok:
                    self.execu.market_buy(decision.quantity)
                    self.entry_price = price
                    self.sl, self.tp = self.risk.exit_levels(price, atr=atr_val)
                    self.in_position = True
                    self.journal.log("entry", price, decision.quantity, 0.0, ts=bar_time)
                    BotState(True, 1, self.entry_price, self.sl, self.tp,
                             decision.quantity, cfg.symbol, cfg.strategy).save(STATE_PATH)
                    print(f"[{bar_time}] 進場買入 ~{decision.quantity} @ {price:.2f} "
                          f"(SL {self.sl:.2f} / TP {self.tp:.2f})")
                else:
                    print(f"[{bar_time}] 風控通過但訂單不合法：{msg}")
            else:
                print(f"[{bar_time}] 風控否決：{decision.reason}")

        elif self.in_position and target != 1:
            qty = self.execu.balance(cfg.base_asset)
            if qty > 0:
                self.execu.market_sell(qty)
                self.journal.log("exit_signal", price, qty, (price - self.entry_price) * qty, ts=bar_time)
                print(f"[{bar_time}] 信號出場 @ {price:.2f}")
            self._flat()


def main():
    cfg = Config()
    if not cfg.api_key or not cfg.api_secret:
        print("找不到測試網金鑰。請複製 .env.example 成 .env 並填入金鑰。")
        return

    from binance import ThreadedWebsocketManager   # 延後 import，缺金鑰時不需要

    client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
    strat = build_strategy(cfg.strategy, **cfg.strategy_params)
    risk = RiskOfficer(cfg)
    execu = ExecutionEngineer(client, cfg.symbol)
    journal = TradeJournal(db_path="trades.db", csv_path="trades.csv",
                           mode="live_testnet_ws", symbol=cfg.symbol, strategy=cfg.strategy)
    trader = LiveTrader(cfg, client, strat, risk, execu, journal)

    print(f"[啟動] WebSocket 測試網模擬盤 | {cfg.symbol} {cfg.interval} | 策略 {cfg.strategy}")
    print(f"交易日誌：trades.db / trades.csv（run_id={journal.run_id}）")
    trader.restore()

    twm = ThreadedWebsocketManager(api_key=cfg.api_key, api_secret=cfg.api_secret, testnet=True)
    twm.start()
    last_closed = [None]

    def handle(msg):
        try:
            k = msg.get("k") if isinstance(msg, dict) else None
            if not k or not k.get("x"):       # 只在 K 線收完（x=True）時動作
                return
            bt = k.get("t")
            if bt == last_closed[0]:
                return
            last_closed[0] = bt
            trader.on_bar_close(pd.to_datetime(bt, unit="ms"))
        except Exception:
            print("[錯誤]", traceback.format_exc())

    twm.start_kline_socket(callback=handle, symbol=cfg.symbol, interval=cfg.interval)
    print("[WS] 已訂閱 K 線串流，等待 K 線收完觸發決策…（Ctrl+C 結束）")
    try:
        twm.join()
    except KeyboardInterrupt:
        print("\n[結束] 使用者中斷")
        twm.stop()


if __name__ == "__main__":
    main()
