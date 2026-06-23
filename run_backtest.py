"""回測進入點：用歷史 K 線評估策略，完全不碰下單。

用法：
    python run_backtest.py                 # 用 config 預設策略
    python run_backtest.py zscore_revert   # 指定策略

抓資料只用公開行情，不需要金鑰（但有金鑰也可以）。
"""
import argparse
from config import Config
from core.market_analyst import make_client, fetch_klines
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from core.trade_journal import TradeJournal
from backtest.backtester import run_backtest


def main():
    ap = argparse.ArgumentParser(description="純回測：用歷史 K 線評估策略，完全不下單。")
    ap.add_argument("strategy", nargs="?", default=None,
                    help="策略名稱（預設用 config 的設定）")
    ap.add_argument("--journal", action="store_true",
                    help="把回測產生的交易傾印到 trades.db / backtest_trades.csv")
    ap.add_argument("--plot", action="store_true",
                    help="把權益曲線 + 回撤存成 PNG（equity_<策略>.png）")
    ap.add_argument("--report", action="store_true",
                    help="產生可用瀏覽器打開的單檔 HTML 報表（report_<策略>.html）")
    args = ap.parse_args()

    cfg = Config()
    if args.strategy:
        cfg.strategy = args.strategy

    client = make_client(cfg.api_key, cfg.api_secret, testnet=True)
    # limit 最多 1000 根。要更長歷史請改用 get_historical_klines 分批抓。
    df = fetch_klines(client, cfg.symbol, cfg.interval, limit=1000)
    print(f"抓到 {len(df)} 根 {cfg.interval} K 線："
          f"{df.index[0]} ~ {df.index[-1]}")

    strat = build_strategy(cfg.strategy, **cfg.strategy_params)
    risk = RiskOfficer(cfg)
    result = run_backtest(df, strat, risk, cfg)

    print(f"\n=== 策略：{cfg.strategy} | {cfg.symbol} {cfg.interval} ===")
    print(result.summary())

    if args.journal:
        with TradeJournal(db_path="trades.db", csv_path="backtest_trades.csv",
                          mode="backtest", symbol=cfg.symbol,
                          strategy=cfg.strategy) as j:
            n = j.log_trades(result.trades)
            run_id = j.run_id
        print(f"\n已傾印 {n} 筆回測交易 → trades.db / backtest_trades.csv（run_id={run_id}）")

    if args.plot:
        from core.plotting import plot_equity
        path = f"equity_{cfg.strategy}.png"
        plot_equity(result, path, title=f"{cfg.strategy} {cfg.symbol} {cfg.interval}")
        print(f"\n已存權益曲線圖 → {path}")

    if args.report:
        from core.report import build_report
        path = f"report_{cfg.strategy}.html"
        build_report(result, title=f"{cfg.strategy} {cfg.symbol} {cfg.interval}", out=path)
        print(f"\n已產生 HTML 報表 → {path}（直接用瀏覽器打開）")

    print("\n提醒：1000 根 5m 只有約 3.5 天，樣本遠遠不夠。")
    print("認真評估前，至少跑跨牛熊、上千筆交易的長歷史，並小心過度擬合。")


if __name__ == "__main__":
    main()
