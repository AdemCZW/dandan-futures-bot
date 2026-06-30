"""多 bot 合併監督器 — 在單一進程跑多台 FuturesLiveTrader，省 Railway 常駐成本。

每台 bot：獨立 client / execu / state 檔 / close 旗標，跑在自己的 daemon 監督執行緒
（崩潰隔離 + 自動重啟）。單一 HTTP 伺服器以命名空間路由（/{id}/state、/{id}/trades、
/{id}/close）對外，/health 最先就緒。設定來自 BOTS_CONFIG（JSON 陣列）。

⚠️ 全程 testnet=True、虛擬資金、不碰真錢。沿用 run_live_futures 的所有交易/風控邏輯，
   本檔只負責「多台並存的隔離與監督」，不改變任何單台決策行為。
"""
import http.server
import json
import os
import re
import socketserver
import threading
import time
import traceback
import urllib.parse

from core.market_analyst import fetch_klines

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REQUIRED = ("id", "symbol", "strategy", "interval")
# 每台 bot 未指定時的後備值（與單台 run_live_futures 的環境變數預設一致）
_DEFAULTS = {
    "params": {},
    "leverage": 1,
    "poll": 30,
    "budget": None,
    "risk_per_trade": None,
    "cb_max_losses": 3,
    "cb_pause_hours": 24.0,
}


def parse_bots_config(raw, defaults=None):
    """解析 BOTS_CONFIG（JSON 陣列）成正規化 bot 設定 list。

    每元素 merged = {**_DEFAULTS, **defaults, **entry}，再驗證：
      - 必填 id / symbol / strategy / interval 皆非空
      - id 限 [A-Za-z0-9_-]（會變成 state 檔名與路由前綴 → 擋路徑穿越）且不重複
      - params 為 dict；leverage / poll 轉 int
    任何問題一律 raise ValueError（附明確訊息），由 main() 印出並保持存活供診斷，
    絕不讓格式錯誤導致進程靜默空轉（HTTP /health 綠燈卻沒有 bot 在跑）。
    """
    if not raw or not str(raw).strip():
        raise ValueError("BOTS_CONFIG 為空（需 JSON 陣列描述各台 bot）")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"BOTS_CONFIG 不是有效 JSON：{e}") from e
    if not isinstance(data, list):
        raise ValueError("BOTS_CONFIG 必須是 JSON 陣列")
    if not data:
        raise ValueError("BOTS_CONFIG 陣列為空（至少要一台 bot）")

    base = {**_DEFAULTS, **(defaults or {})}
    bots, seen = [], set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"BOTS_CONFIG[{i}] 不是物件")
        merged = {**base, **entry}
        for field in _REQUIRED:
            if not merged.get(field):
                raise ValueError(f"BOTS_CONFIG[{i}] 缺必填欄位 '{field}'")
        bid = str(merged["id"])
        if not _ID_RE.match(bid):
            raise ValueError(
                f"BOTS_CONFIG[{i}] id '{bid}' 含非法字元（只允許英數/底線/連字號）")
        if bid in seen:
            raise ValueError(f"BOTS_CONFIG 出現重複 id '{bid}'")
        seen.add(bid)
        if not isinstance(merged.get("params"), dict):
            raise ValueError(f"BOTS_CONFIG[{i}] params 必須是物件（策略參數 dict）")
        try:
            merged["leverage"] = int(merged["leverage"])
            merged["poll"] = int(merged["poll"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"BOTS_CONFIG[{i}] leverage/poll 必須是整數：{e}") from e
        bots.append(merged)
    return bots


class BotWorker:
    """單台 bot 在合併進程中的「身分與隔離資源」載體。

    每台獨立：state 檔（bot_state_{id}.json）、close 旗標（close_request_{id}.flag）、
    close Event（HTTP 緒 set → 該台主迴圈醒來平倉）。trader 於監督執行緒 init 成功後填入；
    restarts / last_error 供監督與診斷觀察。所有路徑都帶 id → 多台並存絕不互相覆蓋。
    """

    def __init__(self, conf: dict, state_dir: str = "."):
        self.id = conf["id"]
        self.symbol = conf["symbol"]
        self.strategy = conf["strategy"]
        self.interval = conf["interval"]
        self.conf = conf
        self.state_path = os.path.join(state_dir, f"bot_state_{self.id}.json")
        self.close_flag_path = os.path.join(state_dir, f"close_request_{self.id}.flag")
        self.close_event = threading.Event()
        self.trader = None          # 監督執行緒 init 成功後填入 FuturesLiveTrader
        self.restarts = 0           # 監督器重啟次數（崩潰隔離觀察用）
        self.last_error = None      # 最近一次致命錯誤摘要（診斷用）


def _worker_trades_bytes(worker: BotWorker, query: str) -> bytes:
    """GET /{id}/trades → 用「該台」strategy+symbol 過濾近期成交，回 JSON bytes。

    關鍵：兩台同策略（trend_pullback）只差 symbol，必須同時用 symbol 過濾，
    否則 ETH 路由會撈到 SOL 的紀錄。read_trades_db 於呼叫時 import（利於測試替換）。
    """
    from core.trade_journal import read_trades_db
    qs = urllib.parse.parse_qs(query)
    limit = int(qs.get("limit", ["50"])[0])
    mode = qs.get("mode", [None])[0]
    try:
        rows = read_trades_db(limit=limit, mode=mode,
                              strategy=worker.strategy, symbol=worker.symbol)
        return json.dumps(rows, default=str).encode()
    except Exception:                       # noqa: BLE001 — 查詢失敗回空陣列，不讓前端 500
        return b"[]"


def route_get(workers: dict, path: str):
    """純函式路由（GET）→ (status, body_bytes)。不碰 socket，便於離線測試。

    /health            → 永遠 200（即使所有 bot 還在 init / 已崩潰重啟中）
    /{id}/state        → 該台 state 檔位元組（缺檔回 {}）
    /{id}/trades?...   → 該台近期成交（依 strategy+symbol 過濾）
    其餘 / 未知 id      → 404
    """
    parsed = urllib.parse.urlparse(path)
    p = parsed.path
    if p == "/health":
        return 200, b'{"ok":true}'
    segs = [s for s in p.split("/") if s]
    if len(segs) != 2:
        return 404, b'{"error":"not found"}'
    wid, action = segs
    worker = workers.get(wid)
    if worker is None:
        return 404, b'{"error":"unknown bot id"}'
    if action == "state":
        try:
            with open(worker.state_path, "rb") as f:
                return 200, f.read()
        except OSError:                     # 缺檔/讀取失敗 → 空物件（前端容忍）
            return 200, b"{}"
    if action == "trades":
        return 200, _worker_trades_bytes(worker, parsed.query)
    return 404, b'{"error":"not found"}'


def route_post(workers: dict, path: str, token_header, env_token):
    """純函式路由（POST）→ (status, payload_dict)。

    /{id}/close：授權通過 → 寫「該台」close 旗標 + set「該台」Event（絕不誤觸他台），
    由該台主迴圈在主執行緒實際平倉（HTTP 緒不直接下單，無跨緒競態）。
    授權：env_token 為空 → 端點停用（403）；token_header 須與 env_token 完全相符。
    """
    from datetime import datetime, timezone
    parsed = urllib.parse.urlparse(path)
    segs = [s for s in parsed.path.split("/") if s]
    if len(segs) != 2 or segs[1] != "close":
        return 404, {"ok": False, "msg": "not found"}
    worker = workers.get(segs[0])
    if worker is None:
        return 404, {"ok": False, "msg": "unknown bot id"}
    if not env_token or token_header != env_token:
        return 403, {"ok": False, "msg": "未授權（CLOSE_TOKEN 未設或不符）"}
    try:
        with open(worker.close_flag_path, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        worker.close_event.set()
        return 200, {"ok": True, "queued": True, "msg": "已排入平倉，下一輪執行"}
    except OSError as e:
        return 500, {"ok": False, "msg": f"寫入平倉旗標失敗：{e}"}


# ── 監督執行緒 + 崩潰隔離 ───────────────────────────────────────────────
def _log(bot_id, msg, detail="") -> None:
    """統一日誌：每行前綴 bot id，多台交錯輸出仍可辨識來源。"""
    print(f"[{bot_id}] {msg}" + (f" {detail}" if detail else ""), flush=True)


def _interruptible_sleep(secs, stop_event, close_event=None) -> None:
    """睡 secs 秒，但 stop（關機）或 close（手動平倉）事件觸發時提早醒來。

    以小切片輪詢 stop_event.wait，確保關機與手動平倉的反應延遲 ≤ 切片（1 秒），
    不像固定 time.sleep 會卡滿整個 poll 週期。
    """
    step = 1.0
    waited = 0.0
    while waited < secs:
        if stop_event.is_set():
            return
        if close_event is not None and close_event.is_set():
            return
        stop_event.wait(min(step, secs - waited))
        waited += min(step, secs - waited)


def poll_loop(worker, trader, client, cfg, stop_event, sleep_fn, log) -> None:
    """單台 bot 的輪詢主迴圈（與單台 run_live_futures 主迴圈邏輯一致）。

    每輪：消費 close 旗標（主執行緒平倉，無跨緒競態）→ 抓 3 根 K 棒 → 新 K 棒觸發
    on_bar_close、否則只刷心跳。逐輪 try/except：暫態錯誤（網路抖動等）吞掉續跑，
    不讓單輪失敗中斷整台；真正的致命錯誤往上拋給 supervise 重建。
    stop_event 設定時乾淨退出（關機/重啟）。
    """
    last_bar = None
    while not stop_event.is_set():
        try:
            if os.path.exists(worker.close_flag_path):
                worker.close_event.clear()          # 先清事件，避免 sleep 立即返回空轉
                try:
                    os.remove(worker.close_flag_path)
                except OSError:
                    pass
                log(worker.id, "收到結算請求", trader.manual_close())
            df = fetch_klines(client, cfg.symbol, cfg.interval, 3, futures=True)
            bar_time = df.index[-2]
            live_price = float(df["close"].iloc[-1])
            if bar_time == last_bar:
                trader._heartbeat(live_price)        # 同一根 K 棒：只刷 updated_at，前端綠燈
            else:
                last_bar = bar_time
                trader.on_bar_close(bar_time)
        except Exception:                            # noqa: BLE001 — 暫態錯誤吞掉續跑（韌性）
            log(worker.id, "迴圈錯誤", traceback.format_exc())
        sleep_fn(cfg.poll_seconds, stop_event, worker.close_event)


def supervise(worker, build_fn, run_fn, stop_event, sleep_fn, log, max_backoff=60) -> None:
    """單台 bot 的監督生命週期：build（init）→ run（poll 迴圈）→ 致命錯 backoff 後重建。

    崩潰隔離核心：每台跑在自己的執行緒，這層把「init 失敗」與「poll 迴圈逃逸的致命例外」
    都接住，指數退避後重建，永不放棄（交易所 API 暫時不可用也會持續重試），且絕不波及他台。
    stop_event 設定 → 乾淨結束。restarts/last_error 供觀察。
    """
    backoff = 1.0
    while not stop_event.is_set():
        try:
            trader, client, cfg = build_fn(worker)
            worker.trader = trader
            backoff = 1.0                            # init 成功 → 退避歸零
            run_fn(worker, trader, client, cfg, stop_event, sleep_fn, log)
        except Exception as e:                       # noqa: BLE001 — 致命錯不拖垮進程/他台
            worker.restarts += 1
            worker.last_error = repr(e)
            log(worker.id, f"監督層致命錯誤（第 {worker.restarts} 次），backoff {backoff:.0f}s 後重建",
                traceback.format_exc())
            sleep_fn(backoff, stop_event, worker.close_event)
            backoff = min(backoff * 2, max_backoff)


def build_trader(worker):
    """從 worker.conf 建構 (trader, client, cfg)，沿用單台 main() 的初始化路徑。

    每台獨立 client / execu / journal（CSV 分檔，DB 共用但以 strategy+symbol 過濾）/
    state 檔（worker.state_path）。金鑰缺失或初始化失敗 → raise，由 supervise 退避重試。
    """
    from config import Config
    from core.market_analyst import make_client
    from core.quant_researcher import build_strategy
    from core.risk_officer import RiskOfficer
    from core.futures_execution_engineer import FuturesExecutionEngineer
    from core.trade_journal import TradeJournal
    import run_live_futures as R

    conf = worker.conf
    cfg = Config()
    cfg.strategy = conf["strategy"]
    cfg.symbol = conf["symbol"]
    cfg.interval = conf["interval"]
    cfg.futures_leverage = conf["leverage"]
    cfg.poll_seconds = conf["poll"]
    cfg.strategy_params = {**cfg.strategy_params, **(conf.get("params") or {})}
    if conf.get("risk_per_trade") is not None:
        cfg.risk_per_trade = float(conf["risk_per_trade"])

    if not cfg.futures_api_key or not cfg.futures_api_secret:
        raise RuntimeError(f"[{worker.id}] 缺合約測試網金鑰（BINANCE_FUTURES_TESTNET_*）")

    client = make_client(cfg.futures_api_key, cfg.futures_api_secret, testnet=True)
    execu = FuturesExecutionEngineer(client, cfg.symbol, leverage=cfg.futures_leverage)
    balance = execu.balance(cfg.quote_asset)
    budget = conf.get("budget")
    if budget is not None and balance > 0:
        cfg.max_position_pct = min(float(budget) / balance, 1.0)
    strat = build_strategy(cfg.strategy, **cfg.strategy_params)
    risk = RiskOfficer(cfg)
    journal = TradeJournal(db_path="trades.db", csv_path=f"trades_{worker.id}.csv",
                           mode="live_futures_testnet", symbol=cfg.symbol, strategy=cfg.strategy)
    ml_path = os.getenv("ML_FILTER_PATH", f"models/{cfg.strategy}.pkl")
    trader = R.FuturesLiveTrader(
        cfg, client, strat, risk, execu, journal,
        cb_max_losses=int(conf.get("cb_max_losses", 3)),
        cb_pause_hours=float(conf.get("cb_pause_hours", 24)),
        ml_model_path=ml_path,
        ml_threshold=float(os.getenv("ML_THRESHOLD", "0.55")),
        state_path=worker.state_path)
    trader.restore()
    _log(worker.id, f"啟動 {cfg.symbol} {cfg.interval} {cfg.strategy} "
                    f"槓桿{cfg.futures_leverage}x | 餘額 {balance:.2f}")
    return trader, client, cfg


class Supervisor:
    """持有所有 BotWorker，為每台起一條 daemon 監督執行緒。"""

    def __init__(self, workers: dict, stop_event: threading.Event | None = None):
        self.workers = workers
        self.stop_event = stop_event or threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> list:
        for w in self.workers.values():
            t = threading.Thread(
                target=supervise,
                args=(w, build_trader, poll_loop, self.stop_event,
                      _interruptible_sleep, _log),
                daemon=True, name=f"bot-{w.id}")
            t.start()
            self.threads.append(t)
        return self.threads


# ── HTTP 伺服器（命名空間路由，最先就緒）─────────────────────────────────
def start_http_server(workers: dict, port: int):
    """單一 port 開多 bot 命名空間端點。ThreadingTCPServer：儀表板併發輪詢多端點不互卡。

    最先啟動：即使所有 bot 還在 init 或崩潰重啟中，/health 仍 200 → Railway 不 kill-loop。
    """
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            status, body = route_get(workers, self.path)
            self._send(status, body)

        def do_POST(self):
            token = os.getenv("CLOSE_TOKEN", "")
            status, payload = route_post(
                workers, self.path, self.headers.get("X-Close-Token"), token)
            self._send(status, json.dumps(payload).encode())

        def _send(self, status, body):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = socketserver.ThreadingTCPServer(("0.0.0.0", port), _Handler)
    server.daemon_threads = True
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[多bot狀態API] 監聽 :{port}  /health  /<id>/state  /<id>/trades  /<id>/close",
          flush=True)
    return server


def main():
    raw = os.getenv("BOTS_CONFIG", "")
    # 共用後備值：未在 BOTS_CONFIG 每台指定時，沿用單台慣用的環境變數
    defaults = {
        "strategy": os.getenv("BOT_STRATEGY") or None,
        "interval": os.getenv("BOT_INTERVAL") or None,
        "leverage": int(os.getenv("BOT_LEV", "1")),
        "poll": int(os.getenv("BOT_POLL", "30")),
    }
    defaults = {k: v for k, v in defaults.items() if v is not None}

    workers: dict = {}
    try:
        bots = parse_bots_config(raw, defaults=defaults)
        workers = {b["id"]: BotWorker(b, state_dir=".") for b in bots}
    except ValueError as e:
        print(f"[致命] BOTS_CONFIG 解析失敗：{e}", flush=True)

    # HTTP「最先」起（即使 config 壞或 bots 還沒 init），確保 healthcheck 立即通過
    port = os.getenv("PORT")
    if port:
        try:
            start_http_server(workers, int(port))
        except Exception as e:                       # noqa: BLE001
            print(f"[警告] 狀態伺服器啟動失敗：{e}", flush=True)

    if not workers:
        print("[致命] 無有效 bot 設定，進程保持存活供診斷（healthcheck 仍綠）", flush=True)
        while True:
            time.sleep(30)

    sup = Supervisor(workers)
    sup.start()
    print(f"[多bot] 已啟動 {len(workers)} 台：{', '.join(workers)}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[結束] 使用者中斷", flush=True)
        sup.stop_event.set()


if __name__ == "__main__":
    main()
