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

# 各時框的合理輪詢間隔（秒）：時框越長、輪詢越疏（4h 不需每 30 秒打 API）
POLL_BY_TF = {"1m": 15, "5m": 30, "15m": 60, "30m": 120, "1h": 120, "4h": 300, "1d": 600}


def load_champion(path: str = "learning_oos_best.json",
                  fallback_strategy: str = "of_momentum",
                  fallback_interval: str = "4h") -> dict:
    """讀 learning_oos_best.json（walk-forward 樣本外最佳），回傳前進驗證要用的配置。

    檔案不存在/損毀/格式不符 → 退回 fallback（of_momentum / 4h 是 OOS 最穩健者）。
    回傳 {strategy, interval, params, symbol, source, oos}。
    """
    data = {}
    if os.path.exists(path):
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            data = {}
    if not isinstance(data, dict) or "strategy" not in data:
        return {"strategy": fallback_strategy, "interval": fallback_interval,
                "params": {}, "symbol": "BTCUSDT", "source": "fallback", "oos": {}}
    return {
        "strategy": data["strategy"],
        "interval": data.get("tf", fallback_interval),
        "params": data.get("params") or {},
        "symbol": data.get("symbol", "BTCUSDT"),
        "source": "learning_oos_best",
        "oos": {k: data.get(k) for k in ("oos_expectancy", "oos_profit_factor",
                                         "oos_win_rate", "folds")},
    }


def fetch_market_klines(source: str, symbol: str, interval: str,
                        limit: int = 300, client=None):
    """抓 K 線。source='mainnet' → 幣安公開【合約真實行情】（免金鑰、不下單）；
    其餘 → 既有 testnet client 路徑。兩者欄位相同（含 taker_base，供訂單流策略用）。"""
    if source == "mainnet":
        import urllib.request
        url = (f"https://fapi.binance.com/fapi/v1/klines"
               f"?symbol={symbol}&interval={interval}&limit={min(limit, 1500)}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read())
        cols = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]
        df = pd.DataFrame(raw, columns=cols)
        for c in ("open", "high", "low", "close", "volume", "taker_base"):
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df.set_index("open_time")[["open", "high", "low", "close", "volume", "taker_base"]]
    from core.market_analyst import fetch_klines
    return fetch_klines(client, symbol, interval, limit)


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
    ap.add_argument("--poll", type=int, default=None, help="輪詢秒數；未給則依時框自動")
    ap.add_argument("--equity", type=float, default=cfg.start_equity)
    ap.add_argument("--source", choices=["testnet", "mainnet"], default="testnet",
                    help="mainnet＝幣安公開合約真實行情（仍只本地模擬成交、不下單）")
    ap.add_argument("--from-best", action="store_true",
                    help="從 learning_oos_best.json 載入 walk-forward 驗證過的冠軍配置")
    ap.add_argument("--max-iters", type=int, default=0, help="跑 N 圈後停（0＝無限，供煙霧測試）")
    args = ap.parse_args()

    champ = None
    if args.from_best:
        champ = load_champion()
        args.strategy, args.interval, args.symbol = champ["strategy"], champ["interval"], champ["symbol"]
        cfg.strategy_params = champ["params"]
        print(f"[冠軍] 採用 {champ['source']}：{champ['strategy']} @ {champ['interval']} "
              f"params={champ['params']} OOS={champ.get('oos')}")
    cfg.strategy, cfg.symbol, cfg.interval = args.strategy, args.symbol, args.interval

    poll = args.poll if args.poll else POLL_BY_TF.get(cfg.interval, 30)
    args.poll = poll
    client = make_client("", "", testnet=True) if args.source == "testnet" else None
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

    print(f"[啟動] Paper 模擬盤（{args.source} 真實行情·本地成交·免金鑰·零真錢） | "
          f"{cfg.symbol} {cfg.interval} | 策略 {cfg.strategy} | poll {poll}s | "
          f"起始 {broker.equity(0):.2f} {cfg.quote_asset}")
    persist()

    iters = 0
    while True:
        if args.max_iters and iters >= args.max_iters:
            print(f"[結束] 達 max-iters={args.max_iters}")
            break
        iters += 1
        try:
            df = strat.prepare(
                fetch_market_klines(args.source, cfg.symbol, cfg.interval, 300, client)).dropna()
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
