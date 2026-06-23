"""Paper 模擬盤實時進入點 — 抓幣安測試網【真實即時行情】，本地模擬成交。

亮點：因為不真的下單，**連測試網金鑰都不用**（公開 K 線免金鑰）。全程虛擬、零真錢。
流程同 run_live：市場分析師→信號工程師→量化研究員→風控官，但成交交給 PaperBroker 本地模擬
（含手續費+滑點），每筆寫進交易日誌（mode=paper），並把模擬餘額/持倉持久化以便重啟還原。

用法：
    python run_paper.py                              # 用 config 預設（5m, ema_cross）
    python run_paper.py --interval 1m --poll 15      # 較快的 K 線、較密的輪詢（適合即時觀察）
    python run_paper.py --strategy zscore_revert
    Ctrl+C 結束。
"""
import argparse
import json
import os
import time
import traceback
from datetime import datetime, timezone

import pandas as pd

from config import Config
from core.market_analyst import make_client, fetch_klines, detect_anomaly
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from core.paper_broker import PaperBroker
from core.trade_journal import TradeJournal

STATE_PATH = "bot_state_paper.json"


def _save(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def _load() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def main():
    ap = argparse.ArgumentParser(description="Paper 模擬盤：真實行情 + 本地模擬成交（免金鑰）。")
    cfg = Config()
    ap.add_argument("--strategy", default=cfg.strategy)
    ap.add_argument("--symbol", default=cfg.symbol)
    ap.add_argument("--interval", default=cfg.interval)
    ap.add_argument("--poll", type=int, default=cfg.poll_seconds)
    ap.add_argument("--equity", type=float, default=cfg.start_equity)
    args = ap.parse_args()
    cfg.strategy, cfg.symbol, cfg.interval = args.strategy, args.symbol, args.interval

    client = make_client("", "", testnet=True)        # 公開行情免金鑰
    strat = build_strategy(cfg.strategy, **cfg.strategy_params)
    risk = RiskOfficer(cfg)
    broker = PaperBroker(cfg, quote_start=args.equity)
    journal = TradeJournal(db_path="trades.db", csv_path="trades_paper.csv",
                           mode="paper", symbol=cfg.symbol, strategy=cfg.strategy)

    in_position = False
    entry_price = sl = tp = 0.0
    last_bar = None
    last_decision = None        # 最近一根的逐關 SOP 決策（供即時監控顯示）

    # 重啟還原（含模擬餘額）
    st = _load()
    if st:
        broker.cash = st.get("cash", broker.cash)
        broker.base = st.get("base", broker.base)
        in_position = st.get("in_position", False)
        entry_price, sl, tp = st.get("entry_price", 0.0), st.get("sl", 0.0), st.get("tp", 0.0)
        print(f"[還原] 現金 {broker.cash:.2f} / 持幣 {broker.base:.6f} / 持倉 {in_position}")

    def persist(price=None):
        _save({"cash": broker.cash, "base": broker.base, "in_position": in_position,
               "entry_price": entry_price, "sl": sl, "tp": tp,
               "symbol": cfg.symbol, "strategy": cfg.strategy, "interval": cfg.interval,
               "last_price": price, "poll": args.poll, "last_decision": last_decision,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    print(f"[啟動] Paper 模擬盤（真實行情·本地成交·免金鑰） | {cfg.symbol} {cfg.interval} | "
          f"策略 {cfg.strategy} | 起始 {broker.equity(0):.2f} {cfg.quote_asset}")
    persist()

    while True:
        try:
            df = strat.prepare(fetch_klines(client, cfg.symbol, cfg.interval, 200)).dropna()
            bar_time = df.index[-2]
            if bar_time == last_bar:
                persist(float(df["close"].iloc[-1]))   # 心跳：每次輪詢刷新現價+時間戳供即時監控
                time.sleep(args.poll)
                continue
            last_bar = bar_time
            row = df.iloc[-2]
            price = float(df["close"].iloc[-1])
            pos_before = 1 if in_position else 0
            anomaly = bool(detect_anomaly(df.iloc[:-1]))
            ind = {k: (None if pd.isna(row[k]) else round(float(row[k]), 4))
                   for k in ("ema_fast", "ema_slow", "rsi", "atr", "zscore") if k in row}
            acts, risk_rec, target = [], None, None
            stopped = False

            # 風控官：持倉中的 SL/TP 保護性出場「永遠先執行」，不受暴量抑制——
            # 暴量/插針正是價格暴力穿破停損的時刻，此時關掉停損與風控意圖完全相反。
            if in_position and (price <= sl or price >= tp):
                r = broker.market_sell(broker.base, price)
                journal.log("exit_sltp", r["fill"], r["qty"], (r["fill"] - entry_price) * r["qty"], ts=bar_time)
                acts.append({"act": "exit_sltp", "price": round(r["fill"], 2)})
                in_position = False
                stopped = True
                print(f"[{bar_time}] 停損/停利出場 @ {r['fill']:.2f} | 權益 {broker.equity(price):.2f}")

            if anomaly:                                  # 暴量：只抑制「新進場/信號進出」，不碰上面的保護性停損
                acts.append({"act": "skip_anomaly"})
                print(f"[{bar_time}] 偵測到暴量異常，本輪跳過下單（價 {price:.2f}）")
            else:
                # 量化研究員：目標倉位
                position = 1 if in_position else 0
                target = strat.signal(row, position)

                # 風控官 + 執行工程師：依目標進出場。同根剛被停損 → 不立即原價回補（避免連環鋸齒）
                if not in_position and target == 1 and not stopped:
                    atr_val = float(row["atr"]) if "atr" in row and not pd.isna(row["atr"]) else None
                    decision = risk.check_entry(broker.balance(cfg.quote_asset), price, bar_time, atr=atr_val)
                    risk_rec = {"allow": bool(decision.allow), "qty": round(float(decision.quantity), 6), "reason": decision.reason}
                    if decision.allow and decision.quantity > 0:
                        r = broker.market_buy(decision.quantity, price)
                        entry_price = r["fill"]
                        sl, tp = risk.exit_levels(entry_price, atr=atr_val)
                        in_position = True
                        journal.log("entry", entry_price, r["qty"], 0.0, ts=bar_time)
                        acts.append({"act": "entry", "price": round(entry_price, 2), "qty": round(r["qty"], 6), "sl": round(sl, 2), "tp": round(tp, 2)})
                        print(f"[{bar_time}] 進場買入 {r['qty']:.6f} @ {entry_price:.2f} "
                              f"(SL {sl:.2f} / TP {tp:.2f}) | 權益 {broker.equity(price):.2f}")
                    else:
                        acts.append({"act": "rejected"})
                        print(f"[{bar_time}] 風控否決：{decision.reason}（價 {price:.2f}）")
                elif in_position and target != 1:
                    r = broker.market_sell(broker.base, price)
                    journal.log("exit_signal", r["fill"], r["qty"], (r["fill"] - entry_price) * r["qty"], ts=bar_time)
                    acts.append({"act": "exit_signal", "price": round(r["fill"], 2)})
                    in_position = False
                    print(f"[{bar_time}] 信號出場 @ {r['fill']:.2f} | 權益 {broker.equity(price):.2f}")
                else:
                    acts.append({"act": "hold" if in_position else "flat"})
                    print(f"[{bar_time}] {'持多' if in_position else '空手'} | 價 {price:.2f} | 權益 {broker.equity(price):.2f}")

            last_decision = {                            # 這一根的逐關 SOP 決策（供即時監控顯示）
                "ts": str(bar_time), "price": round(price, 2),
                "high": round(float(row["high"]), 2), "low": round(float(row["low"]), 2),
                "volume": round(float(row["volume"]), 2) if "volume" in row else None,
                "anomaly": anomaly, "ind": ind, "pos_before": pos_before,
                "target": target, "risk": risk_rec, "actions": acts,
                "pos_after": 1 if in_position else 0, "equity": round(broker.equity(price), 2),
            }
            persist(price)        # 每根更新狀態（現價+時間戳+本根決策），供即時監控讀取
            time.sleep(args.poll)

        except KeyboardInterrupt:
            print("\n[結束] 使用者中斷")
            break
        except Exception:
            print("[錯誤]", traceback.format_exc())
            time.sleep(args.poll)


if __name__ == "__main__":
    main()
