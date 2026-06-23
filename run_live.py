"""模擬盤實時進入點：在【幣安測試網】上掛真的（虛擬）單。

整個流程把 6 個角色串起來：
  市場分析師 → 抓最新 K 線、偵測異常
  信號工程師 → 算指標
  量化研究員 → 產生進出場信號
  風控官     → 決定能不能進、進多少
  執行工程師 → 在測試網下單

⚠️ 全程 testnet=True，用的是虛擬資金，不會花到一毛真錢。
   即便如此，請先讓它在測試網跑數天～數週，觀察行為再說。

用法：
    1. 到 https://testnet.binance.vision/ 登入產生金鑰
    2. cp .env.example .env，填入金鑰
    3. python run_live.py
    Ctrl+C 結束。
"""
import time
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


def main():
    cfg = Config()
    if not cfg.api_key or not cfg.api_secret:
        print("找不到測試網金鑰。請複製 .env.example 成 .env 並填入金鑰。")
        return

    client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
    strat = build_strategy(cfg.strategy, **cfg.strategy_params)
    risk = RiskOfficer(cfg)
    execu = ExecutionEngineer(client, cfg.symbol)
    # 交易日誌：測試網每月重置，每筆成交另存本機 SQLite + CSV，不受影響
    journal = TradeJournal(db_path="trades.db", csv_path="trades.csv",
                           mode="live_testnet", symbol=cfg.symbol, strategy=cfg.strategy)

    last_bar_time = None

    print(f"[啟動] 測試網模擬盤 | {cfg.symbol} {cfg.interval} | 策略 {cfg.strategy}")
    print(f"交易日誌：trades.db / trades.csv（run_id={journal.run_id}）")
    base_bal0 = execu.balance(cfg.base_asset)
    print(f"目前餘額：{execu.balance(cfg.quote_asset):.2f} {cfg.quote_asset}, "
          f"{base_bal0:.6f} {cfg.base_asset}")

    # 重啟還原：讀回上次狀態，再以「交易所實際餘額」為準校正（餘額才是真相）
    price0 = float(fetch_klines(client, cfg.symbol, cfg.interval, 2)["close"].iloc[-1])
    dust = float(execu._filters["min_qty"])
    state, msg = reconcile(BotState.load(STATE_PATH), base_bal0, dust, price0, risk.exit_levels)
    state.symbol, state.strategy = cfg.symbol, cfg.strategy
    state.save(STATE_PATH)
    print(f"[狀態] {msg}")

    in_position = state.in_position
    entry_price, sl, tp = state.entry_price, state.sl, state.tp

    while True:
        try:
            df = strat.prepare(fetch_klines(client, cfg.symbol, cfg.interval, 200)).dropna()
            # 只在「新的一根 K 線收完」時做決策，避免同一根重複觸發
            bar_time = df.index[-2]
            if bar_time == last_bar_time:
                time.sleep(cfg.poll_seconds)
                continue
            last_bar_time = bar_time

            row = df.iloc[-2]            # 用已收完的那根
            price = float(df["close"].iloc[-1])  # 最新成交價

            # 風控官：持倉中的 SL/TP 保護性出場「永遠先執行」，不受暴量抑制——
            # 暴量/插針正是穿破停損的時刻，此時關掉停損與風控意圖相反。
            if in_position and (price <= sl or price >= tp):
                qty = execu.balance(cfg.base_asset)
                if qty > 0:
                    execu.market_sell(qty)
                    journal.log("exit_sltp", price, qty,
                                (price - entry_price) * qty, ts=bar_time)
                    print(f"[{bar_time}] 停損/停利出場 @ {price:.2f}")
                in_position = False
                BotState(symbol=cfg.symbol, strategy=cfg.strategy).save(STATE_PATH)

            # 暴量：只跳過「新進場/信號」，不碰上面的保護性停損。
            # 用 df.iloc[:-1] 排除「仍在形成、尚未收線」的當根，與下單決策的已收完那根對齊。
            if detect_anomaly(df.iloc[:-1]):
                print(f"[{bar_time}] 偵測到暴量異常，本輪跳過下單")
                time.sleep(cfg.poll_seconds)
                continue

            # 新 signal 契約回傳目標倉位（+1/0/-1）。現貨僅做多：
            # 目標 +1 才進場；目標非 +1（0 或 -1）一律平倉；空方信號無法在現貨執行，安全忽略。
            position = 1 if in_position else 0
            target = strat.signal(row, position)

            if not in_position and target == 1:
                quote_bal = execu.balance(cfg.quote_asset)
                atr_val = float(row["atr"]) if "atr" in row.index and not pd.isna(row["atr"]) else None
                decision = risk.check_entry(quote_bal, price, bar_time, atr=atr_val)
                if decision.allow:
                    ok, msg = execu.valid_order(decision.quantity, price)
                    if ok:
                        execu.market_buy(decision.quantity)
                        entry_price = price
                        sl, tp = risk.exit_levels(price, atr=atr_val)
                        in_position = True
                        journal.log("entry", price, decision.quantity, 0.0, ts=bar_time)
                        BotState(in_position=True, direction=1, entry_price=entry_price,
                                 sl=sl, tp=tp, qty=decision.quantity,
                                 symbol=cfg.symbol, strategy=cfg.strategy).save(STATE_PATH)
                        print(f"[{bar_time}] 進場買入 ~{decision.quantity} @ {price:.2f} "
                              f"(SL {sl:.2f} / TP {tp:.2f})")
                    else:
                        print(f"[{bar_time}] 風控通過但訂單不合法：{msg}")
                else:
                    print(f"[{bar_time}] 風控否決：{decision.reason}")

            elif in_position and target != 1:
                qty = execu.balance(cfg.base_asset)
                if qty > 0:
                    execu.market_sell(qty)
                    journal.log("exit_signal", price, qty,
                                (price - entry_price) * qty, ts=bar_time)
                    print(f"[{bar_time}] 信號出場 @ {price:.2f}")
                in_position = False
                BotState(symbol=cfg.symbol, strategy=cfg.strategy).save(STATE_PATH)

            time.sleep(cfg.poll_seconds)

        except KeyboardInterrupt:
            print("\n[結束] 使用者中斷")
            break
        except Exception:
            print("[錯誤]", traceback.format_exc())
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
