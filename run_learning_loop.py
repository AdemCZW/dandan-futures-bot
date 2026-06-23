"""自我學習迴圈 — 定時抓【真實行情】跑策略錦標賽 + 輕量參數搜尋，記錄最佳配置。

使用者選的機制：策略錦標賽自動晉升最佳（期望值排序），不靠脆弱 ML。
每個 cycle：對每個 (symbol, timeframe) 抓幣安公開合約 K 線（真實價格、免金鑰、
不下單），跑全策略錦標賽，再對「本輪最佳策略」做一次小網格參數搜尋試圖再進步。
全部寫進 learning_log.jsonl（逐輪）與 learning_best.json（歷來最佳正期望配置）。

注意：抓的是【公開行情資料】（mainnet fapi，與圖表/大戶分頁同源），不做任何下單、
不碰金鑰、零真錢。執行交易一律只在 testnet（本迴圈根本不執行交易）。

用法：
    python run_learning_loop.py --once                      # 跑一輪就停（煙霧測試）
    python run_learning_loop.py --minutes 18                # 每 18 分一輪，持續學習
    python run_learning_loop.py --minutes 18 --fee 0.0005   # 真實期貨 taker 費率
"""
from __future__ import annotations
import argparse
import itertools
import json
import os
import time
import traceback
import urllib.request
from datetime import datetime, timezone

import pandas as pd

from config import Config
from backtest.tournament import run_tournament, evaluate, rank

FAPI = "https://fapi.binance.com/fapi/v1/klines"
LOG_PATH = "learning_log.jsonl"
BEST_PATH = "learning_best.json"

# 本輪最佳策略會被丟進對應小網格再搜尋一次（只掃一個策略，控制每輪時間）。
SWEEP_GRIDS = {
    "of_momentum":         {"cvd_fast": [8, 10, 14], "cvd_slow": [25, 30, 40]},
    "rsi2_connors":        {"rsi_lo": [3, 5, 10], "rsi_hi": [90, 95, 97]},
    "supertrend":          {"period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]},
    "heikin_ashi_momo":    {"ema_len": [100, 200], "min_run": [2, 3], "wick_frac": [0.1, 0.15]},
    "macd_scalp":          {"adx_min": [12, 18, 25], "ema_trend_period": [50, 100]},
    "bb_squeeze_breakout": {"squeeze_pct": [0.1, 0.2, 0.3], "adx_min": [15, 20, 25]},
    "vwap_band_reversion": {"k": [1.8, 2.2, 2.6], "wick_frac": [0.4, 0.5, 0.6]},
    "zscore_ls":           {"window": [20, 50], "entry_z": [1.5, 2.0, 2.5]},
    "donchian":            {"entry_period": [10, 20, 30], "exit_period": [5, 10]},
}


def fetch_fapi(symbol: str, interval: str, limit: int = 1000) -> pd.DataFrame:
    """幣安公開【合約】K 線（mainnet，真實價格，免金鑰、不下單）。"""
    url = f"{FAPI}?symbol={symbol}&interval={interval}&limit={min(limit, 1500)}"
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


def _grid(space: dict) -> list[dict]:
    keys = list(space)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(space[k] for k in keys))]


def param_search(df, cfg, name: str, min_trades: int) -> dict | None:
    """對單一策略跑小網格參數搜尋，回傳依期望值最佳的合格組合（無則 None）。"""
    space = SWEEP_GRIDS.get(name)
    if not space:
        return None
    rows = []
    for params in _grid(space):
        try:
            rows.append(evaluate(df, name, cfg, params))
        except Exception:                                       # noqa: BLE001
            continue
    ranked = rank(rows, objective="expectancy", min_trades=min_trades)
    best = next((r for r in ranked if r["eligible"]), None)
    return best


def one_cycle(symbols, timeframes, fee, min_trades, limit) -> dict:
    """跑一輪：每個 (symbol, tf) 錦標賽 + 本輪最佳策略的參數搜尋。"""
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scans = []
    overall_best = None        # (expectancy, record) 跨 sym/tf 的最佳正期望 champion

    for sym in symbols:
        for tf in timeframes:
            try:
                df = fetch_fapi(sym, tf, limit)
            except Exception as e:                              # noqa: BLE001
                scans.append({"symbol": sym, "tf": tf, "error": f"{type(e).__name__}: {e}"})
                continue
            cfg = Config(symbol=sym, interval=tf, fee_rate=fee)
            res = run_tournament(df, cfg, min_trades=min_trades)
            champ = res["champion"]
            elig = [r for r in res["ranked"] if r["eligible"]]
            n_pos = sum(1 for r in elig if r["expectancy"] > 0)
            scan = {"symbol": sym, "tf": tf, "bars": len(df),
                    "n_eligible": len(elig), "n_positive": n_pos,
                    "champion": _slim(champ)}

            # 對本輪最佳策略再做一次參數搜尋（學習更好的參數）
            if champ:
                refined = param_search(df, cfg, champ["strategy"], min_trades)
                if refined:
                    scan["refined"] = _slim(refined)
                    cand = refined if refined["expectancy"] > (champ["expectancy"] or -9e9) else champ
                else:
                    cand = champ
                if cand["expectancy"] > 0:
                    rec = {"symbol": sym, "tf": tf, "fee": fee, **_slim(cand), "ts": stamp}
                    if overall_best is None or cand["expectancy"] > overall_best[0]:
                        overall_best = (cand["expectancy"], rec)
            scans.append(scan)

    cycle = {"ts": stamp, "fee": fee, "min_trades": min_trades, "scans": scans,
             "best_this_cycle": overall_best[1] if overall_best else None}
    return cycle


def _slim(r: dict | None) -> dict | None:
    if not r:
        return None
    return {k: r.get(k) for k in ("strategy", "params", "trades", "win_rate",
                                  "expectancy", "profit_factor", "total_return",
                                  "max_drawdown", "sharpe")}


def _update_best(cycle: dict) -> dict | None:
    """把本輪最佳與歷來最佳（learning_best.json）比較，保留期望值最高者。"""
    best = None
    if os.path.exists(BEST_PATH):
        try:
            with open(BEST_PATH) as fh:
                best = json.load(fh)
        except (json.JSONDecodeError, OSError):
            best = None
    cand = cycle.get("best_this_cycle")
    if cand and (best is None or cand["expectancy"] > best.get("expectancy", -9e9)):
        best = cand
        tmp = BEST_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(best, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, BEST_PATH)
    return best


def _append_log(cycle: dict) -> None:
    with open(LOG_PATH, "a") as fh:
        fh.write(json.dumps(cycle, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description="自我學習迴圈（回測錦標賽，真實行情資料）。")
    ap.add_argument("--symbols", default="BTCUSDT", help="逗號分隔")
    ap.add_argument("--timeframes", default="5m,15m,1h,4h", help="逗號分隔")
    ap.add_argument("--fee", type=float, default=0.0005, help="單邊手續費（真實期貨 taker≈0.05%）")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--limit", type=int, default=1000, help="每時框抓幾根 K 線")
    ap.add_argument("--minutes", type=float, default=18.0, help="每輪間隔（分）")
    ap.add_argument("--once", action="store_true", help="只跑一輪就停")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    print(f"[學習迴圈] symbols={symbols} tf={timeframes} fee={args.fee} "
          f"min_trades={args.min_trades} 每輪={args.minutes}分 once={args.once}")
    cycle_i = 0
    while True:
        cycle_i += 1
        try:
            cycle = one_cycle(symbols, timeframes, args.fee, args.min_trades, args.limit)
            _append_log(cycle)
            best = _update_best(cycle)
            btc = cycle.get("best_this_cycle")
            line = (f"[#{cycle_i} {cycle['ts']}] " +
                    "; ".join(f"{s['symbol']}/{s['tf']}:"
                              f"{(s.get('champion') or {}).get('strategy','—')}"
                              f"({s.get('n_positive',0)}+/{s.get('n_eligible',0)})"
                              for s in cycle["scans"] if "error" not in s))
            print(line, flush=True)
            if btc:
                print(f"    本輪最佳: {btc['symbol']}/{btc['tf']} {btc['strategy']} "
                      f"E={btc['expectancy']} PF={btc['profit_factor']} win={btc['win_rate']}", flush=True)
            if best:
                print(f"    歷來最佳: {best['symbol']}/{best['tf']} {best['strategy']} "
                      f"E={best['expectancy']} PF={best['profit_factor']} win={best['win_rate']}", flush=True)
        except Exception:                                       # noqa: BLE001
            print("[錯誤]", traceback.format_exc(), flush=True)

        if args.once:
            break
        time.sleep(args.minutes * 60)


if __name__ == "__main__":
    main()
