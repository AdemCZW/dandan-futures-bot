"""每日 forward 追蹤器（2026-07-07）——取代測試網 live bot。

使用者決策：不再花錢在 Railway 跑測試網 bot（資料被幽靈行情/部署churn污染、
測試網又每月重置+條件單壞掉）。改成每天在剛收完的**真實主網 4h K 線**上重跑
smc/4h/rr3 籃子回測，累積乾淨的樣本外 forward 紀錄。

為什麼這是真 forward test（不是重新 fit）：策略因果、每筆交易只用到當根為止的
資料；3 年回測驗證截止在 2026-07-06（信賴下界 +1.77），之後每天新增的棒都是
策略沒看過的資料。只要固定 FORWARD_START 錨點、累積之後的交易，就是誠實的
前進驗證——零成本、資料乾淨、無基礎設施 bug。

用法：
    python research/scratchpad/daily_forward_tracker.py          # 人看的報告
    python research/scratchpad/daily_forward_tracker.py --json   # 機器可讀（供每日 routine）
    python research/scratchpad/daily_forward_tracker.py --new-days 2   # 標記近2天新交易
"""
import sys, os, json, time, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import pandas as pd

from config import Config
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from backtest.backtester import run_backtest
from backtest.tournament import bootstrap_mean_lower_bound

CORE8 = ["SUIUSDT", "BTCUSDT", "ETHUSDT", "ARBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "DOTUSDT"]
CACHE = os.path.join(os.path.dirname(__file__), "..", "klines_cache")
FORWARD_START = "2026-07-06"     # 乾淨 forward 錨點（F1/F2 修復 + 3年驗證截止日）
BACKTEST_LB = 1.77               # 3年回測驗證的信賴下界（forward 要對照的基準）
FAPI = "https://fapi.binance.com/fapi/v1/klines"
DAYS = 1095
os.makedirs(CACHE, exist_ok=True)


def make_cfg(**overrides):
    base = dict(interval="4h", risk_per_trade=0.003, futures_leverage=3,
                fee_rate=0.0005, slippage=0.0002, fill_lag=1,
                funding_rate_per_8h=0.0001, max_daily_loss_pct=10.0, tp_R_mult=3.0)
    base.update(overrides)
    return Config(**base)


# ── 純函式（TDD）──────────────────────────────────────────────────────
def merge_klines(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合併舊+新 K 線：去重（重疊棒取新值）、時間正序。任一為空安全處理。"""
    if new is None or new.empty:
        return old
    if old is None or old.empty:
        return new.sort_index()
    combined = pd.concat([old, new])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def trades_since(closed, start_ts) -> list:
    """已平倉交易中，ts >= start_ts 的部分（含起點當天）。"""
    start = pd.Timestamp(start_ts)
    return [t for t in closed if pd.Timestamp(t["ts"]) >= start]


def forward_report(pnls) -> dict:
    """一組平倉 pnl → 筆數/總損益/期望/bootstrap 信賴下界。"""
    if not pnls:
        return {"n": 0, "total": 0.0, "expectancy": None, "lb": None}
    total = float(sum(pnls))
    return {
        "n": len(pnls),
        "total": round(total, 2),
        "expectancy": round(total / len(pnls), 4),
        "lb": round(bootstrap_mean_lower_bound(list(pnls)), 4),
    }


def closed_trades(trades) -> list:
    return [t for t in trades if str(t.get("side", "")).startswith("exit")]


# ── 網路（每日增量抓真實 4h K 線）────────────────────────────────────
def fetch_incremental(symbol, interval="4h", opener=None):
    """讀快取 → 只抓「最後一根之後」的新棒 → merge + 存檔 → 回傳完整 df。

    首次（無快取）抓滿 DAYS 天。opener 可注入（測試/離線）；預設 urlopen。
    """
    opener = opener or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20))
    cache = os.path.join(CACHE, f"{symbol}_{interval}_{DAYS}.csv")
    old = pd.read_csv(cache, index_col=0, parse_dates=True) if os.path.exists(cache) else pd.DataFrame()
    end_ms = int(time.time() * 1000)
    if not old.empty:
        start_ms = int(old.index[-1].timestamp() * 1000) + 1
    else:
        start_ms = end_ms - DAYS * 24 * 3600 * 1000
    rows, cur = [], start_ms
    while cur < end_ms:
        url = f"{FAPI}?symbol={symbol}&interval={interval}&startTime={cur}&endTime={end_ms}&limit=1500"
        try:
            with opener(url) as r:
                batch = json.loads(r.read())
        except Exception as e:                      # noqa: BLE001
            print(f"  [warn] {symbol} 抓取失敗：{e}"); break
        if not batch:
            break
        rows.extend(batch); cur = int(batch[-1][6]) + 1; time.sleep(0.05)
    if rows:
        cols = ["open_time", "open", "high", "low", "close", "volume", "ct", "qv", "n", "tb", "tq", "ig"]
        nd = pd.DataFrame(rows, columns=cols)
        for c in ("open", "high", "low", "close", "volume"):
            nd[c] = nd[c].astype(float)
        nd["open_time"] = pd.to_datetime(nd["open_time"], unit="ms", utc=True)
        nd = nd.drop_duplicates("open_time").set_index("open_time")
        nd.index = nd.index.tz_localize(None)
        new = nd[["open", "high", "low", "close", "volume"]]
    else:
        new = pd.DataFrame()
    merged = merge_klines(old, new)
    if not merged.empty:
        merged.to_csv(cache)
    return merged


def run_basket(data):
    """對每幣跑 smc/4h/rr3 回測，回傳所有已平倉交易（各筆標 symbol）。"""
    cfg = make_cfg()
    all_closed = []
    per_symbol = {}
    for s, df in data.items():
        if df is None or len(df) < 100:
            continue
        res = run_backtest(df.copy(), build_strategy("smc_structure"), RiskOfficer(cfg), cfg)
        cl = closed_trades(res.trades)
        for t in cl:
            t["symbol"] = s
        all_closed.extend(cl)
        per_symbol[s] = cl
    return all_closed, per_symbol


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="每日 forward 追蹤（真實 4h K 線籃子回測）")
    ap.add_argument("--json", action="store_true", help="輸出機器可讀 JSON（供每日 routine）")
    ap.add_argument("--new-days", type=int, default=1, help="標記近 N 天新出現的交易")
    ap.add_argument("--no-fetch", action="store_true", help="不抓新資料，用現有快取")
    args = ap.parse_args()

    print(f"每日 forward 追蹤（錨點 {FORWARD_START}，回測基準下界 +{BACKTEST_LB}）")
    print("═" * 70)
    data = {}
    for s in CORE8:
        if args.no_fetch:
            p = os.path.join(CACHE, f"{s}_4h_{DAYS}.csv")
            df = pd.read_csv(p, index_col=0, parse_dates=True) if os.path.exists(p) else None
        else:
            df = fetch_incremental(s)
        if df is not None and not df.empty:
            data[s] = df
            print(f"  {s:10s} {len(df)} 根，最新 {df.index[-1]}")

    all_closed, per_symbol = run_basket(data)
    fwd = trades_since(all_closed, FORWARD_START)
    fwd_pnls = [t["pnl"] for t in fwd]

    now = pd.Timestamp(datetime.now(timezone.utc).replace(tzinfo=None))
    new_cut = now - pd.Timedelta(days=args.new_days)
    new_trades = trades_since(all_closed, new_cut)

    rep = forward_report(fwd_pnls)
    print("\n── 累計 forward（{} 起，樣本外真實資料）──".format(FORWARD_START))
    if rep["n"] == 0:
        print("  尚無 forward 平倉交易（4h 慢，錨點後可能還沒觸發訊號）")
    else:
        lb_ok = "✅ 維持顯著" if (rep["lb"] or -9) > 0 else "⚠ 尚不顯著（樣本仍小）"
        print(f"  {rep['n']} 筆  總損益 {rep['total']:+.2f}  期望 {rep['expectancy']:+.3f}  "
              f"信賴下界 {rep['lb']:+.3f}  {lb_ok}")
        print(f"  對照：3年回測驗證下界 +{BACKTEST_LB}（forward 樣本夠大後應趨近）")

    print(f"\n── 近 {args.new_days} 天新交易 ──")
    if not new_trades:
        print("  無")
    else:
        for t in sorted(new_trades, key=lambda x: str(x["ts"])):
            print(f"  {str(t['ts'])[:16]} {t.get('symbol','?'):10s} {t['side']:<12} pnl {t['pnl']:+.2f}")

    if args.json:
        out = {"forward_start": FORWARD_START, "backtest_lb": BACKTEST_LB,
               "cumulative": rep,
               "new_trades": [{"ts": str(t["ts"]), "symbol": t.get("symbol"),
                               "side": t["side"], "pnl": round(t["pnl"], 2)}
                              for t in sorted(new_trades, key=lambda x: str(x["ts"]))]}
        print("\nFORWARD_JSON")
        print(json.dumps(out, ensure_ascii=False, indent=2))
