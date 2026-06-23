"""策略錦標賽 — 回測驅動的「自我學習」核心。

把所有策略跑在同一段【真實 K 線】上，依【期望值】(預設) 排序，挑出當前最佳。
這是使用者選的學習機制：策略錦標賽自動晉升最佳（不靠脆弱的 ML），
評估指標以「每筆期望值 / 盈虧比」為主、勝率為輔——純勝率會被高勝率低盈虧比的假象騙到。

短線真實 paper 跑 15–20 分鐘只會有 0–2 筆交易、算不出有意義勝率；
所以學習訊號來自「近期真實 K 線的快速回測」(幾秒上百筆)，live paper 只做前進驗證。

用法（CLI）：
    python -m backtest.tournament --csv btc_5m_futures_6mo.csv --tail 8000 --interval 5m
    python -m backtest.tournament --csv btc_5m_futures_6mo.csv --fee 0.0005 --objective expectancy
"""
from __future__ import annotations
import json

from backtest.backtester import run_backtest
from core.quant_researcher import STRATEGIES, build_strategy
from core.risk_officer import RiskOfficer

# objective 名稱 → 用來排序的指標欄位（profit_factor 用保留 inf 的 raw 欄）
_SCORE_KEY = {
    "expectancy": "expectancy",
    "profit_factor": "profit_factor_raw",
    "sharpe": "sharpe",
    "return": "total_return",
    "total_return": "total_return",
    "win_rate": "win_rate",
}


def evaluate(df, name: str, cfg, params: dict | None = None) -> dict:
    """跑一次回測，回傳該策略在這段資料上的績效指標 dict（JSON 安全）。"""
    res = run_backtest(df.copy(), build_strategy(name, **(params or {})),
                       RiskOfficer(cfg), cfg)
    pf = res.profit_factor
    return {
        "strategy": name,
        "params": params or {},
        "trades": len(res.trades),
        "win_rate": round(res.win_rate, 4),
        "expectancy": round(res.expectancy, 4),
        # JSON 安全：inf → None（無虧損交易）；raw 保留原值供排序
        "profit_factor": (None if pf == float("inf") else round(pf, 4)),
        "profit_factor_raw": pf,
        "total_return": round(res.total_return, 4),
        "max_drawdown": round(res.max_drawdown, 4),
        "sharpe": round(res.sharpe, 4),
    }


def rank(results: list[dict], objective: str = "expectancy",
         min_trades: int = 20) -> list[dict]:
    """依 objective 遞減排序；交易數 < min_trades 者分數沉底（eligible=False）。

    每筆附上 'eligible' 與 'score'。score 為排序用分數（不合格 → -inf）。
    """
    skey = _SCORE_KEY.get(objective, "expectancy")
    out = []
    for r in results:
        eligible = r.get("trades", 0) >= min_trades
        raw = r.get(skey)
        if raw is None:
            raw = float("-inf")
        out.append({**r, "eligible": eligible,
                    "score": (raw if eligible else float("-inf"))})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def run_tournament(df, cfg, names: list[str] | None = None, min_trades: int = 20,
                   objective: str = "expectancy",
                   param_overrides: dict | None = None) -> dict:
    """把 names（預設全部）跑一輪錦標賽，回傳排行榜 + 當前 champion。

    單一策略丟例外不會炸掉整場（記 error、沉底）。champion = 排序後第一個合格者，
    全不合格 → None（代表這段資料上沒有達標策略，學習迴圈應「繼續搜索」）。
    """
    names = names or list(STRATEGIES)
    param_overrides = param_overrides or {}
    results = []
    for name in names:
        try:
            results.append(evaluate(df, name, cfg, param_overrides.get(name)))
        except Exception as e:                                   # noqa: BLE001
            results.append({"strategy": name, "trades": 0,
                            "error": f"{type(e).__name__}: {e}",
                            "expectancy": float("-inf")})
    ranked = rank(results, objective, min_trades)
    champion = next((r for r in ranked if r["eligible"]), None)
    return {"objective": objective, "min_trades": min_trades,
            "n_strategies": len(names), "ranked": ranked, "champion": champion}


def format_table(result: dict) -> str:
    """把錦標賽結果排成可讀表格（CLI / 報告用）。"""
    lines = [f"{'#':>2} {'strategy':22s} {'trades':>6s} {'win%':>6s} "
             f"{'expct':>9s} {'PF':>6s} {'ret%':>8s} {'maxDD%':>7s}"]
    for i, r in enumerate(result["ranked"], 1):
        if "error" in r:
            lines.append(f"{i:>2} {r['strategy']:22s}  ERROR {r['error'][:40]}")
            continue
        pf = r["profit_factor_raw"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        flag = "" if r["eligible"] else "  (trades<min)"
        lines.append(f"{i:>2} {r['strategy']:22s} {r['trades']:6d} "
                     f"{r['win_rate']*100:6.1f} {r['expectancy']:9.3f} {pf_s:>6s} "
                     f"{r['total_return']*100:8.2f} {r['max_drawdown']*100:7.2f}{flag}")
    champ = result["champion"]
    lines.append("")
    lines.append(f"champion: {champ['strategy']} (expectancy={champ['expectancy']}, "
                 f"PF={champ['profit_factor']})" if champ else
                 "champion: 無（此段資料上無策略達標 → 繼續搜索 / 調參 / 換時框）")
    return "\n".join(lines)


def save(result: dict, path: str, stamp: str | None = None) -> None:
    """把錦標賽結果寫成 JSON（供 live paper / 前端 / 學習迴圈讀取）。"""
    payload = {**result}
    if stamp:
        payload["timestamp"] = stamp
    # profit_factor_raw 可能是 inf → 移除，只留 JSON 安全的 profit_factor
    for r in payload.get("ranked", []):
        r.pop("profit_factor_raw", None)
    if payload.get("champion"):
        payload["champion"] = {k: v for k, v in payload["champion"].items()
                               if k != "profit_factor_raw"}
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    import os
    os.replace(tmp, path)


def _main():
    import argparse
    from datetime import datetime, timezone
    from config import Config
    from core.market_analyst import load_klines

    ap = argparse.ArgumentParser(description="策略錦標賽：回測排行（期望值排序）。")
    ap.add_argument("--csv", required=True, help="歷史 K 線 CSV（market_analyst.save_klines 格式）")
    ap.add_argument("--tail", type=int, default=8000, help="只取最後 N 根（近期）")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--fee", type=float, default=None, help="覆寫手續費率（單邊），例如 0.0005")
    ap.add_argument("--slippage", type=float, default=None)
    ap.add_argument("--objective", default="expectancy",
                    choices=list(_SCORE_KEY))
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--out", default="tournament_result.json")
    args = ap.parse_args()

    df = load_klines(args.csv)
    if args.tail:
        df = df.tail(args.tail)
    kw = {"interval": args.interval}
    if args.fee is not None:
        kw["fee_rate"] = args.fee
    if args.slippage is not None:
        kw["slippage"] = args.slippage
    cfg = Config(**kw)

    print(f"[錦標賽] {args.csv} | 近 {len(df)} 根 {args.interval} | "
          f"fee={cfg.fee_rate} slip={cfg.slippage} | 目標={args.objective}")
    result = run_tournament(df, cfg, objective=args.objective, min_trades=args.min_trades)
    print(format_table(result))
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(result, args.out, stamp=stamp)
    print(f"\n已寫入 {args.out}")


if __name__ == "__main__":
    _main()
