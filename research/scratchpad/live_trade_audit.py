"""實盤交易全面稽核（2026-07-06）。

使用者反映：實盤很多虧損、感覺一直在跟市場反方向下單、交易過程有很多細節
沒注意到。這支腳本對 b1-b9 的實際成交紀錄做系統化稽核：

  ① 配對完整性：entry/exit 是否成對、數量是否吻合、有無孤兒出場（接管倉位）
  ② 損益一致性：記錄的 pnl vs (出場價-進場價)×qty×方向-手續費，抓記帳異常
  ③ 同棒重複進場（churn）：同一根 K 棒內進場多次＝盤中軟停損出場後立刻重進，
     回測引擎每根棒只評估一次做不到這件事 → 實盤與回測的真實分岔點
  ④ 出場方式分布：SL/TP/trail/signal/reconciled 各佔多少、各自平均損益
  ⑤ 市場情境：每筆進場當下 EMA20/50 排列、日線趨勢方向、進場後 3 根棒市場
     實際走向 → 量化「順勢單 vs 逆勢單」的比例與損益差
  ⑥ 回測重放對照：同一時段、同一設定跑回測，看回測會不會做出同樣的交易
     （實盤虧 = 策略本來就會虧？還是實盤引擎跟回測行為不一致？）

資料：research/live_audit/b*_trades.json（從 bot /trades 端點拉下的原始紀錄）
      research/klines_cache/*_4h_1095.csv（3年4h快取）
"""
import sys, os, json
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import pandas as pd
import numpy as np

HERE = os.path.dirname(__file__)
AUDIT_DIR = os.path.join(HERE, "..", "live_audit")
CACHE = os.path.join(HERE, "..", "klines_cache")

BOTS = {
    "b1": "SUIUSDT", "b2": "BTCUSDT", "b3": "ETHUSDT", "b4": "ARBUSDT",
    "b5": "XRPUSDT", "b6": "DOGEUSDT", "b7": "ADAUSDT", "b8": "DOTUSDT",
    "b9": "LINKUSDT",
}
TAKER_FEE = 0.0004   # 實盤 PnL 記帳用的單邊 taker 費率（config.taker_fee_rate）


def parse_ts(s):
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def load_klines(symbol):
    path = os.path.join(CACHE, f"{symbol}_4h_1095.csv")
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, index_col=0, parse_dates=True)


def bar_floor(dt):
    """真實時間 → 所屬 4h K 棒的開盤時間。"""
    return dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0)


def market_context(df, entry_dt, direction):
    """進場當下的市場情境（只用進場棒之前已收完的資料，不偷看未來）。"""
    bar = bar_floor(entry_dt)
    # 進場棒 = 訊號來自前一根收完的棒；情境用「進場棒之前」的資料算
    hist = df[df.index < bar]
    if len(hist) < 60:
        return None
    close = hist["close"]
    ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
    ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
    ema_dir = 1 if ema20 > ema50 else -1
    # 日線趨勢：把 4h 重採樣成日線，MA20/60 排列（只用已收完的日線）
    daily = close.resample("1D").last().dropna()
    if len(daily) >= 60:
        dma20 = daily.rolling(20).mean().iloc[-2]   # -2 = 昨日已收完
        dma60 = daily.rolling(60).mean().iloc[-2]
        daily_dir = 0 if (np.isnan(dma20) or np.isnan(dma60)) else (1 if dma20 > dma60 else -1)
    else:
        daily_dir = 0
    # 進場後 3 根棒市場實際走向（這是「事後」，用來評估是不是真的跟市場反向）
    future = df[df.index >= bar]
    fwd_move = None
    if len(future) >= 4:
        fwd_move = (future["close"].iloc[3] - future["close"].iloc[0]) / future["close"].iloc[0]
    return {
        "ema_dir": ema_dir, "daily_dir": daily_dir, "fwd_move_3bar": fwd_move,
        "with_ema": ema_dir == direction,
        "with_daily": (daily_dir == direction) if daily_dir != 0 else None,
        "mkt_went_with": (fwd_move is not None and
                          ((direction == 1 and fwd_move > 0) or (direction == -1 and fwd_move < 0))),
    }


def audit_bot(bot_id, symbol):
    path = os.path.join(AUDIT_DIR, f"{bot_id}_trades.json")
    if not os.path.exists(path):
        return None
    rows = json.load(open(path))
    if not rows:
        return {"bot": bot_id, "symbol": symbol, "rows": 0, "round_trips": [],
                "anomalies": [], "open_entry": None}
    rows = list(reversed(rows))          # API 最新在前 → 反轉成插入順序（真實時序）
    df = load_klines(symbol)

    round_trips, anomalies = [], []
    open_entry = None
    entries_per_bar = {}

    for r in rows:
        ts = parse_ts(r["ts"])
        side = r["side"]
        if side.startswith("entry"):
            direction = -1 if "short" in side else 1
            if open_entry is not None:
                anomalies.append(f"連續 entry 無 exit：{open_entry['ts']} 之後又 {r['ts']}（前筆可能被接管/漏記）")
            open_entry = {"ts": ts, "price": r["price"], "qty": r["qty"], "dir": direction}
            bar = bar_floor(ts) if ts else None
            if bar:
                entries_per_bar[bar] = entries_per_bar.get(bar, 0) + 1
        else:
            if open_entry is None:
                anomalies.append(f"孤兒 exit（無對應 entry，多半是接管的遺留倉位）：{r['ts']} {side} pnl={r['pnl']:+.2f}")
                continue
            e = open_entry
            open_entry = None
            qty_diff = abs(r["qty"] - e["qty"]) / max(e["qty"], 1e-12)
            expected = (r["price"] - e["price"]) * e["qty"] * e["dir"]
            expected -= (e["price"] + r["price"]) * e["qty"] * TAKER_FEE   # 雙邊手續費
            pnl_gap = r["pnl"] - expected
            ctx = market_context(df, e["ts"], e["dir"]) if (df is not None and e["ts"]) else None
            hold_h = ((parse_ts(r["ts"]) - e["ts"]).total_seconds() / 3600) if (e["ts"] and parse_ts(r["ts"])) else None
            round_trips.append({
                "entry_ts": e["ts"], "exit_ts": r["ts"], "dir": e["dir"],
                "entry_px": e["price"], "exit_px": r["price"], "qty": e["qty"],
                "pnl": r["pnl"], "exit_type": r["side"], "hold_h": hold_h,
                "qty_mismatch": qty_diff > 0.02, "pnl_gap": pnl_gap,
                "pnl_inconsistent": abs(pnl_gap) > max(1.0, abs(expected) * 0.2),
                "ctx": ctx,
            })
            if qty_diff > 0.02:
                anomalies.append(f"進出場數量不符：entry qty={e['qty']:.4g} vs exit qty={r['qty']:.4g}（{r['ts']}）")
            if abs(pnl_gap) > max(1.0, abs(expected) * 0.2):
                anomalies.append(f"損益記帳不一致：記錄 {r['pnl']:+.2f} vs 依價差推算 {expected:+.2f}（{r['ts']} {r['side']}）")

    churn_bars = {k: v for k, v in entries_per_bar.items() if v > 1}
    for bar, cnt in sorted(churn_bars.items()):
        anomalies.append(f"同棒重複進場 ×{cnt}：{bar}（盤中停損出場後立刻重進，回測做不到）")

    return {"bot": bot_id, "symbol": symbol, "rows": len(rows),
            "round_trips": round_trips, "anomalies": anomalies,
            "open_entry": open_entry, "churn_bars": churn_bars}


def replay_backtest(symbol, start="2026-06-20"):
    """同時段回測重放：4h smc_structure + rr3（現行線上設定）。"""
    from config import Config
    from core.quant_researcher import build_strategy
    from core.risk_officer import RiskOfficer
    from backtest.backtester import run_backtest
    df = load_klines(symbol)
    if df is None:
        return []
    df = df[df.index >= "2026-04-01"]     # 留暖機
    cfg = Config(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                 fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                 funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0, tp_R_mult=3.0)
    res = run_backtest(df.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
    out = []
    cutoff = pd.Timestamp(start)
    for t in res.trades:
        ts = pd.Timestamp(t["ts"])
        if ts >= cutoff and t["side"].startswith("entry"):
            out.append({"ts": str(ts), "side": t["side"], "price": t["price"]})
    return out


if __name__ == "__main__":
    all_rt, all_anoms = [], []
    print("═" * 78)
    print("① ~ ④ 逐台稽核")
    print("═" * 78)
    for bot_id, symbol in BOTS.items():
        a = audit_bot(bot_id, symbol)
        if a is None or a["rows"] == 0:
            print(f"\n── {bot_id} {symbol}：無成交紀錄")
            continue
        print(f"\n── {bot_id} {symbol}（{a['rows']} 列，{len(a['round_trips'])} 回合"
              f"{'，尚有未平倉 entry' if a['open_entry'] else ''}）")
        for rt in a["round_trips"]:
            d = "多" if rt["dir"] == 1 else "空"
            ctx = rt["ctx"]
            ctx_s = ""
            if ctx:
                ctx_s = (f" | EMA{'順' if ctx['with_ema'] else '逆'}"
                         f" 日線{'順' if ctx['with_daily'] else ('逆' if ctx['with_daily'] is not None else '?')}"
                         f" 後市{'同向' if ctx['mkt_went_with'] else '反向'}")
            flags = []
            if rt["qty_mismatch"]: flags.append("⚠qty")
            if rt["pnl_inconsistent"]: flags.append("⚠pnl記帳")
            print(f"   {str(rt['entry_ts'])[:16]} {d} @{rt['entry_px']:.6g} → "
                  f"{rt['exit_type']:<15} @{rt['exit_px']:.6g}  pnl{rt['pnl']:+8.2f}"
                  f"  持{rt['hold_h']:.0f}h{ctx_s} {' '.join(flags)}")
        for an in a["anomalies"]:
            print(f"   ⚠ {an}")
        all_rt.extend([{**rt, "bot": bot_id} for rt in a["round_trips"]])
        all_anoms.extend([(bot_id, x) for x in a["anomalies"]])

    # ⑤ 彙總：順勢 vs 逆勢
    print("\n" + "═" * 78)
    print("⑤ 市場情境彙總（有市場資料可對照的回合）")
    print("═" * 78)
    ctxed = [rt for rt in all_rt if rt["ctx"]]
    for label, key in [("EMA20/50 排列", "with_ema"), ("日線 MA20/60 趨勢", "with_daily")]:
        w = [rt for rt in ctxed if rt["ctx"][key] is True]
        ag = [rt for rt in ctxed if rt["ctx"][key] is False]
        print(f"\n  依{label}：")
        print(f"    順勢單 {len(w):>2} 筆  總損益 {sum(r['pnl'] for r in w):+8.2f}")
        print(f"    逆勢單 {len(ag):>2} 筆  總損益 {sum(r['pnl'] for r in ag):+8.2f}")
    went_with = [rt for rt in ctxed if rt["ctx"]["mkt_went_with"]]
    went_against = [rt for rt in ctxed if not rt["ctx"]["mkt_went_with"]]
    print(f"\n  進場後 3 根棒市場實際走向：")
    print(f"    同向（方向看對）{len(went_with):>2} 筆  總損益 {sum(r['pnl'] for r in went_with):+8.2f}")
    print(f"    反向（方向看錯）{len(went_against):>2} 筆  總損益 {sum(r['pnl'] for r in went_against):+8.2f}")

    # 出場方式分布
    print(f"\n  出場方式分布：")
    by_type = {}
    for rt in all_rt:
        by_type.setdefault(rt["exit_type"], []).append(rt["pnl"])
    for t, pnls in sorted(by_type.items()):
        print(f"    {t:<16}{len(pnls):>3} 筆  合計 {sum(pnls):+8.2f}  平均 {sum(pnls)/len(pnls):+7.2f}")

    # ⑥ 回測重放對照
    print("\n" + "═" * 78)
    print("⑥ 回測重放對照（同時段 2026-06-20 起、同設定 smc/4h/rr3，回測會怎麼做）")
    print("═" * 78)
    for bot_id, symbol in BOTS.items():
        if symbol == "LINKUSDT":
            continue                     # b9 是雙均線，另案
        bt = replay_backtest(symbol)
        live_entries = [rt for rt in all_rt if rt["bot"] == bot_id]
        print(f"  {bot_id} {symbol:<9} 回測進場 {len(bt)} 次 {[t['ts'][:16] for t in bt]}"
              f" ｜ 實盤回合 {len(live_entries)} 次")

    print("\n  異常彙總：")
    for bot_id, an in all_anoms:
        print(f"    [{bot_id}] {an}")
