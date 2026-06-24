"""產生一份「全時框 walk-forward 策略穩健性報告」(markdown + json)。

對每個時框用長歷史 CSV 跑全策略 walk-forward（樣本外）錦標賽，
輸出每時框完整排行 + 跨時框穩健度摘要，方便一次看完哪個策略在哪個時框最穩。

用法：
    python report_walkforward.py                 # fee 0.0005，輸出 walk_forward_report.md
    python report_walkforward.py --fee 0.0002
"""
from __future__ import annotations
import argparse
import json
from datetime import datetime, timezone

from config import Config
from core.market_analyst import load_klines
from core.quant_researcher import STRATEGIES
from backtest.tournament import run_walkforward_tournament

# (csv, 時框, train_bars, test_bars)
CASES = [
    ("btc_5m_futures_6mo.csv",  "5m",  2000, 500),
    ("btc_15m_futures_9mo.csv", "15m", 1500, 400),
    ("btc_1h_futures_12mo.csv", "1h",  1000, 250),
    ("btc_4h_futures_18mo.csv", "4h",   600, 150),
]


def _pf_str(r):
    pf = r.get("oos_profit_factor_raw")
    if pf is None:
        return "—"
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def build(fee: float, min_trades_oos: int) -> tuple[str, dict]:
    by_tf = {}
    for csv, tf, tr, te in CASES:
        df = load_klines(csv)
        cfg = Config(interval=tf, fee_rate=fee)
        res = run_walkforward_tournament(df, cfg, train_bars=tr, test_bars=te,
                                         min_trades_oos=min_trades_oos)
        res["_bars"] = len(df)
        res["_folds"] = res["ranked"][0].get("folds", 0) if res["ranked"] else 0
        by_tf[tf] = res

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    L = []
    L.append("# Walk-Forward 策略穩健性報告")
    L.append("")
    L.append(f"- 生成時間：{ts}")
    L.append(f"- 手續費：{fee*100:.2f}%/邊（真實期貨 taker 約 0.04–0.05%）")
    L.append(f"- 達標門檻：樣本外 ≥ {min_trades_oos} 筆交易")
    L.append("- 方法：每時框長歷史 CSV → walk-forward（訓練窗選**預設參數**→鎖定→"
             "只在未見過的測試窗評分→跨 fold 彙總樣本外 OOS）。**只有 OOS 指標可信**；"
             "樣本內漂亮不算數。")
    L.append("")

    # ── 跨時框穩健度摘要 ──
    def is_pos(tf, name):
        for r in by_tf[tf]["ranked"]:
            if r["strategy"] == name:
                return bool(r.get("eligible") and (r.get("oos_expectancy") or -9e9) > 0)
        return False

    rows = []
    for name in STRATEGIES:
        flags = {tf: is_pos(tf, name) for tf in ("5m", "15m", "1h", "4h")}
        rows.append((name, flags, sum(flags.values())))
    rows.sort(key=lambda x: x[2], reverse=True)

    L.append("## 摘要：跨時框樣本外穩健度（✅＝OOS 正期望且達標）")
    L.append("")
    L.append("| 策略 | 5m | 15m | 1h | 4h | 正 OOS 時框數 |")
    L.append("|---|:--:|:--:|:--:|:--:|:--:|")
    for name, flags, cnt in rows:
        cells = " | ".join("✅" if flags[tf] else "—" for tf in ("5m", "15m", "1h", "4h"))
        L.append(f"| `{name}` | {cells} | **{cnt}** |")
    L.append("")

    # ── 各時框完整排行 ──
    L.append("## 各時框完整 OOS 排行")
    for tf in ("5m", "15m", "1h", "4h"):
        res = by_tf[tf]
        npos = sum(1 for r in res["ranked"]
                   if r.get("eligible") and (r.get("oos_expectancy") or -9e9) > 0)
        L.append("")
        L.append(f"### {tf} — {res['_bars']} bars · ~{res['_folds']} folds · "
                 f"{npos} 個策略 OOS 正期望")
        L.append("")
        L.append("| # | 策略 | OOS交易 | 勝率 | 期望值 | PF | OOS報酬% | 達標 |")
        L.append("|--:|---|--:|--:|--:|--:|--:|:--:|")
        for i, r in enumerate(res["ranked"], 1):
            if "error" in r:
                L.append(f"| {i} | `{r['strategy']}` | — | — | — | — | — | ⚠️ |")
                continue
            ok = "✅" if r.get("eligible") and r["oos_expectancy"] > 0 else (
                "•" if r.get("eligible") else "trades<min")
            L.append(f"| {i} | `{r['strategy']}` | {r['oos_trades']} | "
                     f"{r['oos_win_rate']*100:.1f}% | {r['oos_expectancy']:.2f} | "
                     f"{_pf_str(r)} | {r['oos_return_compounded']*100:.1f} | {ok} |")
    L.append("")

    # ── 結論 ──
    champs = {tf: by_tf[tf]["champion"] for tf in ("5m", "15m", "1h", "4h")}
    L.append("## 結論")
    L.append("")
    for tf in ("5m", "15m", "1h", "4h"):
        c = champs[tf]
        if c and c["oos_expectancy"] > 0:
            L.append(f"- **{tf}**：OOS 冠軍 `{c['strategy']}` — 期望值 {c['oos_expectancy']}、"
                     f"PF {c['oos_profit_factor']}、勝率 {c['oos_win_rate']*100:.1f}%、{c['folds']} folds ✅")
        elif c:
            L.append(f"- **{tf}**：**無 OOS 正期望策略**（最佳僅 `{c['strategy']}` "
                     f"期望值 {c['oos_expectancy']}）→ 此時框沒有可信 edge，別碰")
        else:
            L.append(f"- **{tf}**：無策略通過樣本外門檻（此時框沒有可信 edge）")
    L.append("")
    top = rows[0]
    L.append(f"- **最穩健**：`{top[0]}`（在 {top[2]} 個時框 OOS 正期望）。")
    L.append("- 短線（5m/15m）若全軍覆沒，代表手續費+雜訊吃光 edge，"
             "真正可信的方向在較長時框。")

    report = "\n".join(L)
    payload = {"ts": ts, "fee": fee, "min_trades_oos": min_trades_oos,
               "by_tf": {tf: {"champion": by_tf[tf]["champion"],
                              "ranked": [{k: v for k, v in r.items()
                                          if k != "fold_records" and k != "oos_profit_factor_raw"}
                                         for r in by_tf[tf]["ranked"]]}
                         for tf in by_tf}}
    return report, payload


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--min-trades-oos", type=int, default=40)
    ap.add_argument("--out", default="walk_forward_report.md")
    args = ap.parse_args()

    print(f"[報告] 跑全時框 walk-forward（fee {args.fee}）…可能需數十秒")
    report, payload = build(args.fee, args.min_trades_oos)
    with open(args.out, "w") as fh:
        fh.write(report)
    with open(args.out.replace(".md", ".json"), "w") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    print(f"已寫入 {args.out} 與 {args.out.replace('.md', '.json')}\n")
    print(report)


if __name__ == "__main__":
    main()
