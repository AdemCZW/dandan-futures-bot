"""一次性執行進入點 — 每台 bot 跑「一輪 poll 迴圈」就退出。給 Railway cron / 排程用。

跟 run_multi_futures 完全共用 parse_bots_config / build_trader / BotWorker，只把
常駐的 `while` 迴圈換成「單輪」。單輪本體逐位元對齊 run_multi_futures.poll_loop：
  build（含 trader.restore()）→ 取 restored_last_bar → 抓 3 根 K 棒 → 軟停損/盈利保底
  → 新 K 棒才 on_bar_close、同一根只心跳。

為什麼安全（不重演 F2 同棒重複進場）：
  1. 倉位真相在交易所——restore() 每次啟動以合約實際持倉為準。
  2. 決策去重靠 restored_last_bar()：優先讀 state 檔 last_decision.ts（放在持久
     磁碟卷 → 每次執行間存活），退無可退才用交易日誌(Postgres)推斷。
  3. 出場在 run 之間由交易所掛單式硬停損/停利守護，離線也會觸發。

排程建議：每整點跑一次（`2 * * * *`）。interval=4h 時，多數 run 會落在同一根 K 棒
→ 只心跳＋跑軟停損後備，不決策；4h 收盤後那一次才決策。錯過的 run 只會跳過該根
（4h 策略可接受）。持久化：state 檔 → BOT_STATE_DIR（磁碟卷）；交易日誌 → DATABASE_URL
（Postgres）。兩者皆持久，臨時檔遺失也不影響去重。

用法：
    BOT_STATE_DIR=/data BOTS_CONFIG='[...]' \
    BINANCE_FUTURES_TESTNET_KEY=... BINANCE_FUTURES_TESTNET_SECRET=... \
    python run_once.py
"""
import os
import sys
import traceback

from run_multi_futures import BotWorker, build_trader, parse_bots_config

# state 檔放這個目錄——Railway 上設成磁碟卷掛載點（如 /data），本機預設當前目錄。
STATE_DIR = os.getenv("BOT_STATE_DIR", ".")


def _bots_config_defaults() -> dict:
    """與 run_multi_futures.main() 相同的 BOTS_CONFIG 每台後備值（沿用單台 env 慣例）。"""
    d = {
        "strategy": os.getenv("BOT_STRATEGY") or None,
        "interval": os.getenv("BOT_INTERVAL") or None,
        "leverage": int(os.getenv("BOT_LEV", "1")),
        "poll": int(os.getenv("BOT_POLL", "30")),
    }
    return {k: v for k, v in d.items() if v is not None}


def run_bot_once(worker, build_fn=build_trader, fetch_fn=None, log=print) -> str:
    """單台 bot 跑一輪 = run_multi_futures.poll_loop 迴圈本體的單次執行。

    build_fn / fetch_fn 可注入以利離線測試；預設用真實的 build_trader / fetch_klines。
    回傳 "decided"（決策了新 K 棒）或 "heartbeat"（同一根、只刷心跳）。
    """
    if fetch_fn is None:
        from core.market_analyst import fetch_klines as fetch_fn  # 延遲載入，利測試替換

    trader, data_client, cfg = build_fn(worker)                   # 內含 trader.restore()
    last_bar = getattr(trader, "restored_last_bar", lambda: None)()

    # 消費 close 旗標（手動平倉；cron 場景少用，保留與常駐版一致行為）
    if os.path.exists(worker.close_flag_path):
        try:
            os.remove(worker.close_flag_path)
        except OSError:
            pass
        log(f"[{worker.id}] 收到結算請求 {trader.manual_close()}")

    df = fetch_fn(data_client, cfg.symbol, cfg.interval, 3, futures=True)
    bar_time = df.index[-2]                                        # 倒數第二根＝最新已收盤
    live_price = float(df["close"].iloc[-1])

    # 每輪軟停損/盈利保底（交易所條件單偶發故障時的即時後備）
    soft = getattr(trader, "check_soft_stops", None)
    if soft is not None:
        reason = soft(live_price)
        if reason:
            log(f"[{worker.id}] 軟停損觸發（{reason}）@ {live_price}")
    if trader.check_profit_floor(live_price):
        log(f"[{worker.id}] 盈利保底觸發 {trader.manual_close()}")

    if bar_time == last_bar:
        trader._heartbeat(live_price)                             # 同一根：只刷 updated_at
        log(f"[{worker.id}] {cfg.symbol} 同一根K棒（{bar_time}）→ 心跳，不決策")
        return "heartbeat"

    trader.on_bar_close(bar_time)                                 # 新 K 棒：決策一次
    log(f"[{worker.id}] {cfg.symbol} 決策新K棒 {bar_time}（dir={getattr(trader, 'dir', '?')}）")
    return "decided"


def main(argv=None) -> None:
    raw = os.getenv("BOTS_CONFIG", "")
    try:
        bots = parse_bots_config(raw, defaults=_bots_config_defaults())
    except ValueError as e:
        print(f"[致命] BOTS_CONFIG 解析失敗：{e}", flush=True)
        sys.exit(2)

    failures = 0
    for conf in bots:
        worker = BotWorker(conf, state_dir=STATE_DIR)
        try:
            run_bot_once(worker)
        except Exception:                            # noqa: BLE001 — 一台失敗不拖累他台
            failures += 1
            print(f"[{worker.id}] 單輪致命錯誤：\n{traceback.format_exc()}", flush=True)

    print(f"[一次性] 完成 {len(bots)} 台，其中 {failures} 台失敗", flush=True)
    # 全部失敗 → 非零退出（排程器 UI 顯示紅燈）；部分成功 → 0（不因單台拖累整批）
    if bots and failures == len(bots):
        sys.exit(1)


if __name__ == "__main__":
    main()
