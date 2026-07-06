"""合約測試網實時進入點 — 支援【真正做空】（target +1/-1/0 都會執行）。

與 run_live.py（現貨、僅做多）的差別：合約可開空單，故 zscore_ls 等策略的 -1 信號
會真的開空、而非被忽略。決策封在 FuturesLiveTrader，可離線用假物件測試；
ThreadedWebsocketManager 不用，改輪詢以利驗證。

⚠️ 全程 testnet=True，指向 https://testnet.binancefuture.com，**虛擬資金、不碰真錢**。
   合約測試網金鑰與現貨測試網【完全獨立】，需在 testnet.binancefuture.com 另外產生。
   先在測試網跑數天～數週、觀察停損/熔斷/換邊/還原都正常再說。

用法：
    1. 到 https://testnet.binancefuture.com 產生金鑰
    2. 在 .env 填入 BINANCE_FUTURES_TESTNET_API_KEY / _SECRET
    3. python run_live_futures.py   （Ctrl+C 結束）
"""
import argparse
import http.server
import json
import math
import os
import socketserver
import threading
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd

from config import Config
from core.market_analyst import make_client, make_data_client, fetch_klines, detect_anomaly
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from core.futures_execution_engineer import FuturesExecutionEngineer
from core.trade_journal import TradeJournal
from core.bot_state import BotState, detect_testnet_reset
from core.circuit_breaker import CircuitBreaker
from core.directional_guard import DirectionalChannelGuard

STATE_PATH = "bot_state_futures.json"
CLOSE_REQUEST_PATH = "close_request.flag"   # 手動平倉旗標：HTTP 緒寫、主迴圈讀（避免跨緒下單競態）
_CLOSE_EVENT = threading.Event()            # HTTP 緒 set() → 主迴圈從 wait() 立即醒來執行平倉


def _interval_freq(interval: str) -> str:
    """幣安 K 線週期字串 → pandas floor/Timedelta 頻率（"15m"→"15min"、"1d"→"1D"、"4h"→"4h"）。"""
    unit = interval[-1].lower()
    return interval[:-1] + {"m": "min", "h": "h", "d": "D"}.get(unit, unit)


def _close_authorized(header_token: str | None, env_token: str | None) -> bool:
    """手動平倉端點授權：未設 CLOSE_TOKEN（空）→ 停用；否則 header 須完全相符。

    用 hmac.compare_digest 恆時比較，避免逐字元比對的 timing 洩漏面。
    """
    import hmac
    if not env_token:
        return False
    return hmac.compare_digest(str(header_token or ""), str(env_token))


def parse_bot_params(raw: str | None) -> dict:
    """解析 BOT_PARAMS 環境變數（JSON 字串）成策略參數 dict。

    空字串 / None / 無效 JSON → 回空 dict，不崩潰。
    用法：BOT_PARAMS='{"use_htf_filter": true, "htf_ema_period": 200}'
    """
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, ValueError):
        print(f"[警告] BOT_PARAMS 解析失敗，忽略：{raw!r}")
        return {}


class FuturesLiveTrader:
    """合約多/空決策 + 下單 + 狀態持久化。dir：+1 多 / -1 空 / 0 空手。可獨立測試。"""

    def __init__(self, cfg, client, strat, risk, execu, journal,
                 cb_max_losses: int = 3, cb_pause_hours: float = 24,
                 ml_model_path: str | None = None,
                 ml_threshold: float = 0.55,
                 state_path: str | None = None,
                 exchange_stop_enabled: bool | None = None,
                 dcg_enabled: bool | None = None,
                 dcg_max_losses: int | None = None,
                 dcg_cooldown_bars: int | None = None,
                 data_client=None):
        # state_path 預設 None → 解析「當前」模組全域 STATE_PATH（而非定義時綁定），
        # 既保留單台部署相容（含既有測試的 monkeypatch.setattr(M, "STATE_PATH", …)），
        # 又讓多 bot 監督器能為每台傳入獨立檔路徑，避免共用一檔互相覆蓋。
        self.state_path = state_path if state_path is not None else STATE_PATH
        # exchange_stop / dcg 旗標預設 None → 退回 os.getenv（單台行為不變）；
        # 多 bot 監督器為每台顯式傳入，避免合併進程共用同一份 env 把各台旗標綁死
        # （例：只有 Bot2 要 DCG_ENABLED=1，不能讓他台也被開啟）。
        self.cfg, self.client, self.strat = cfg, client, strat
        self.risk, self.execu, self.journal = risk, execu, journal
        # 行情資料 client（訊號/指標/軟停損判斷）：預設同執行 client（向後相容）；
        # 部署時傳入主網公開 client → decisions 在主網座標、fills 在測試網（稽核 F1）。
        self.data_client = data_client if data_client is not None else client
        self.dir = 0
        self.entry_price = self.sl = self.tp = 0.0
        self.qty = 0.0                      # 本地追蹤的持倉量（避免開倉後立刻讀帶號倉位的最終一致性問題）
        self._last_risk = None              # _open() 執行後暫存風控決策供 SOP 讀取
        self.peak = self.trough = 0.0       # 進場後極值（Chandelier 追蹤停損用）
        self._peak_pnl = 0.0               # 進場後最高浮盈 USDT（盈利保底用）
        self._profit_above_since = None    # 浮盈首次超標的時間（持續計時用）
        self._scaled_out = False            # Scale-out：本輪持倉已部分獲利了結
        self._scaled_pnl = 0.0             # F4：scale-out 已實現獲利（出場時合併給熔斷判淨損益）
        self._entry_sl_dist = 0.0          # 進場時的原始停損距離（scale-out 閾值用）
        # 交易所掛單式硬停損（EXCHANGE_STOP_ENABLED；預設關，逐台 env 開）：
        # bot 當機/熔斷/網路斷期間仍由交易所端 STOP_MARKET/TAKE_PROFIT_MARKET 守護倉位。
        self._exchange_stop = (
            bool(exchange_stop_enabled) if exchange_stop_enabled is not None
            else os.getenv("EXCHANGE_STOP_ENABLED", "0").lower() in ("1", "true", "yes"))
        self._stop_oid = None               # 交易所停損單 orderId
        self._tp_oid = None                 # 交易所停利單 orderId
        self._stop_sl = None                # 已掛停損單對應的 sl 價（變動時才 cancel/replace）
        self.cb = CircuitBreaker(max_losses=cb_max_losses, pause_hours=cb_pause_hours)
        # 方向感知通道護欄（fib_channel reversion 連虧防呆）；預設停用，需 env 或參數開啟
        self._dcg = DirectionalChannelGuard(
            max_losses=int(dcg_max_losses if dcg_max_losses is not None
                           else os.getenv("DCG_MAX_LOSSES", "3")),
            cooldown_bars=int(dcg_cooldown_bars if dcg_cooldown_bars is not None
                              else os.getenv("DCG_COOLDOWN_BARS", "8")),
            enabled=(bool(dcg_enabled) if dcg_enabled is not None
                     else os.getenv("DCG_ENABLED", "0").lower() in ("1", "true", "yes")))
        # PortfolioGuard：跨 bot 同向暴露控制；max_notional 由 env 設定（預設 15000 USDT）
        from core.portfolio_guard import PortfolioGuard
        self._guard = PortfolioGuard()
        self._guard_max = float(os.getenv("PORTFOLIO_MAX_NOTIONAL", "15000"))
        # OPT-13 集中度子上限（預設關＝None，與舊行為相容；Railway 設 env 才生效）：
        #   PORTFOLIO_SYMBOL_MAX_NOTIONAL：同 symbol（或相關性桶）同向名目上限
        #   PORTFOLIO_CORR_SYMBOLS：逗號分隔的相關性桶，如 "SOLUSDT,ETHUSDT"（高相關視為同一風險源）
        _sym_cap = os.getenv("PORTFOLIO_SYMBOL_MAX_NOTIONAL", "").strip()
        self._guard_symbol_cap = float(_sym_cap) if _sym_cap else None
        _corr = os.getenv("PORTFOLIO_CORR_SYMBOLS", "").strip()
        self._guard_corr_symbols = [s.strip().upper() for s in _corr.split(",") if s.strip()] or None
        # OPT-05 組合層回撤 kill-switch（預設 0＝關，與舊行為相容）：四台合計淨值自峰值
        # 回落超過此比例 → 暫停所有 bot 開新倉（只擋進場、不碰既有倉），補單台熔斷看不到的組合風險。
        self._guard_max_dd = float(os.getenv("PORTFOLIO_MAX_DRAWDOWN", "0") or "0")
        # ML Filter：若模型檔存在則載入；不存在則靜默跳過（不影響原有邏輯）
        self._ml_model    = None
        self._ml_threshold = ml_threshold
        if ml_model_path and os.path.exists(ml_model_path):
            try:
                from ml.ml_filter import load_filter
                self._ml_model = load_filter(ml_model_path)
                print(f"[ML Filter] 已載入模型：{ml_model_path}（門檻 {ml_threshold:.0%}）")
            except Exception as e:
                print(f"[ML Filter] 載入失敗，跳過：{e}")

    def _save(self) -> None:
        cb_dict = self.cb.to_dict()
        bs = BotState(in_position=self.dir != 0, direction=self.dir, entry_price=self.entry_price,
                      sl=self.sl, tp=self.tp, qty=abs(self.qty),
                      symbol=self.cfg.symbol, strategy=self.cfg.strategy,
                      cb_consecutive_losses=cb_dict["consecutive_losses"],
                      cb_paused_until=cb_dict["paused_until"] or "",
                      dcg_state=json.dumps(self._dcg.to_dict()),
                      scaled_out=self._scaled_out,
                      scaled_pnl=self._scaled_pnl,
                      entry_sl_dist=self._entry_sl_dist,
                      stop_oid=str(self._stop_oid or ""),
                      tp_oid=str(self._tp_oid or ""))
        # Merge into existing file to preserve display fields (mode, interval, last_price, …)
        # that _write_sop() manages — otherwise a save() between SOP calls strips "mode"
        # and live_status() defaults to "paper", fetching from local DB → trades vanish.
        existing: dict = {}
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path) as fh:
                    raw = json.load(fh)
                    if isinstance(raw, dict):
                        existing = raw
        except Exception:
            pass
        existing.update(asdict(bs))
        existing["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(existing, fh, default=str)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def _fetch_bars(self) -> int:
        """這台 bot 每輪要抓幾根 K 棒：依策略最長回看週期動態決定（OPT-03）。

        只抓 200 根會讓 200EMA（trend_pullback）暖機嚴重不足。warmup_bars() 估出
        ≥4× 最長週期；夾在 [200, 1500]（1500 是幣安合約 klines 單次上限）。
        策略無此方法（極舊或測試替身）→ 退回 200，與舊行為相容。
        """
        try:
            n = int(self.strat.warmup_bars())
        except Exception:                       # noqa: BLE001 — 無 warmup_bars → 退回舊預設
            n = 200
        return max(200, min(n, 1500))

    def _round_trip_fee(self, qty, entry_px, exit_px) -> float:
        """這筆平倉應扣的雙邊 taker 手續費＝|qty| × (進場名目 + 出場名目) × taker 費率。

        OPT-01：實盤 PnL 原本完全不扣手續費 → 寫進 journal → 餵 Kelly 高估盈虧比放大倉位。
        在每個出場記帳點扣掉這筆費用，讓 journal 與 Kelly 吃到較真實的淨值。
        （此為已成交費用的事後扣除，純記帳、不碰下單、不引入前視。資金費率另案處理。）
        """
        rate = getattr(self.cfg, "taker_fee_rate", 0.0)
        return abs(qty) * (abs(entry_px) + abs(exit_px)) * rate

    def _latest_atr(self):
        """抓最近已收盤那根的 ATR（供 restore 重建停損用）；失敗則回 None 退回固定百分比。"""
        try:
            df = self.strat.prepare(
                fetch_klines(self.data_client, self.cfg.symbol, self.cfg.interval,
                             self._fetch_bars(), futures=True)).dropna()
            if len(df) >= 2 and "atr" in df.columns:
                v = df["atr"].iloc[-2]
                return float(v) if not pd.isna(v) else None
        except Exception:                   # noqa: BLE001 — restore 容錯，抓不到就退回固定 %
            pass
        return None

    def restore(self) -> None:
        """重啟還原：以合約實際帶號持倉為準，狀態檔補 entry/SL/TP。"""
        amt = self.execu.position_amt()
        dust = float(self.execu._filters["min_qty"])
        st = BotState.load(self.state_path)
        if abs(amt) > dust:
            self.dir = 1 if amt > 0 else -1
            self.qty = abs(amt)
            side = "多" if self.dir == 1 else "空"
            if st.in_position and st.direction == self.dir:
                self.entry_price, self.sl, self.tp = st.entry_price, st.sl, st.tp
                msg = f"還原{side}單：entry {self.entry_price:.2f} / SL {self.sl:.2f} / TP {self.tp:.2f}"
            else:
                price = self.execu.mark_price()
                self.entry_price = price
                self.sl, self.tp = self.risk.exit_levels(price, self.dir, atr=self._latest_atr())
                msg = (f"⚠️ 帳上有{side}倉但無對應狀態：以標記價 {price:.2f} 估 entry、"
                       f"重設 SL {self.sl:.2f}/TP {self.tp:.2f}（建議人工確認）")
            # Chandelier 極值必須以還原的 entry 為起點 —— 否則 trough/peak 殘留 0，
            # 下一根 trailing 會用 min(sl, 0+chand*atr) 把空單 SL 砸成 ~3×ATR（停損形同失效）。
            self.peak = self.trough = self.entry_price
            # 還原 scale-out 狀態，避免重啟後再次觸發已做過的 scale-out
            self._scaled_out = st.scaled_out
            self._scaled_pnl = st.scaled_pnl        # F4：重啟不歸零，出場時仍能合併判淨損益
            self._entry_sl_dist = st.entry_sl_dist
            self._stop_oid = st.stop_oid or None
            self._tp_oid = st.tp_oid or None
            self._stop_sl = self.sl if self._stop_oid else None
        else:
            self.dir, self.entry_price, self.sl, self.tp, self.qty = 0, 0.0, 0.0, 0.0, 0.0
            self.peak = self.trough = 0.0
            self._stop_oid = self._tp_oid = self._stop_sl = None
            msg = "空手啟動（合約無未平倉部位）"
        # 從狀態檔還原 Circuit Breaker
        st = BotState.load(self.state_path)
        self.cb = CircuitBreaker.from_dict(
            {"consecutive_losses": st.cb_consecutive_losses,
             "paused_until": st.cb_paused_until or None},
            max_losses=self.cb.max_losses, pause_hours=self.cb.pause_hours)
        # 從狀態檔還原方向感知通道護欄（保留封鎖/冷卻狀態，重啟不放水）
        try:
            dcg_data = json.loads(st.dcg_state) if st.dcg_state else {}
        except (json.JSONDecodeError, TypeError):
            dcg_data = {}
        self._dcg = DirectionalChannelGuard.from_dict(
            dcg_data, max_losses=self._dcg.max_losses,
            cooldown_bars=self._dcg.cooldown_bars, enabled=self._dcg.enabled)
        # 交易所掛單式硬停損：還原持倉且啟用 → 撤殘單、依還原 SL/TP 重掛，確保重啟後仍有交易所端保護
        if self._exchange_stop and self.dir != 0:
            try:
                self.execu.cancel_all_stops()
            except Exception as e:                  # noqa: BLE001
                print(f"[掛單] 重啟撤殘單失敗：{e}")
            # 等撤單真正完成再掛新單：撤單回應 ≠ 交易所端已撤完，立刻掛會被 -4130 拒
            # （「同方向已有 closePosition 單」），實測曾造成掛單全失敗 → 倉位裸奔到下一根 K 棒。
            import time as _time
            for _ in range(6):
                try:
                    if not self.execu.open_orders():
                        break
                except Exception:                   # noqa: BLE001 — 查詢失敗不擋流程，交給 -4130 認養兜底
                    break
                _time.sleep(0.5)
            self._stop_oid = self._tp_oid = self._stop_sl = None
            self._place_protective("restore")
        self._backfill_orphan_exit()                # 補記漏記的平倉（狀態檔遺失＋交易所已平倉）
        self._save()
        print(f"[狀態] {msg}")

    def _backfill_orphan_exit(self) -> None:
        """重啟還原後補記漏記的平倉，避免幽靈持倉與損益遺失。

        情境：狀態檔被 `railway up` 清空、且交易所端 STOP/TP 在停機期間觸發平倉，
        下一輪對帳來不及跑 → DB 只剩沒有配對 exit 的 entry。重啟後 restore() 只看
        交易所現況判為空手，該筆交易與其損益就永久消失（前端還會顯示幽靈「持有中」）。

        修法：DB 依 (strategy, symbol) 撈自己的紀錄（唯一對應本 bot），若推算出仍有
        未平倉 entry、但 restore 後本地為空手（self.dir==0）→ 補記一筆 exit_reconciled。
        PnL 以當前標記價估算（與本系統其餘虛擬記帳一致；四台共用同帳戶無法用交易所
        realizedPnl 歸屬單一 bot）。冪等：補記後 DB 已配對，再次重啟不會重複補。
        """
        if self.dir != 0:                           # 交易所仍有倉（已還原）→ 不補記
            return
        try:
            from core.trade_journal import read_trades_db, implied_open_position
            rows = read_trades_db(limit=200, strategy=self.cfg.strategy,
                                  symbol=self.cfg.symbol)
            op = implied_open_position(rows)
            if not op:
                return
            mark = float(self.execu.mark_price())
            qty = float(op["qty"])
            d = int(op["dir"])
            pnl = ((mark - op["price"]) * qty * d
                   - self._round_trip_fee(qty, op["price"], mark))
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.journal.log("exit_reconciled", mark, qty, pnl, ts=now)
            print(f"[對帳補記] DB 顯示未平倉 {op['side']} @ {op['price']:.4f} 但交易所空手 "
                  f"→ 以標記價 {mark:.4f} 補記平倉（估 pnl {pnl:+.2f}）")
        except Exception as e:                      # noqa: BLE001 — 補記失敗不可擋啟動
            print(f"[對帳補記] 略過（讀取/推算失敗）：{e}")

    def _classify_exit(self, price) -> str:
        """細分 SL/TP 觸發的結單原因（只分類標籤，不改變平倉行為）：
          exit_tp        觸及固定停利目標
          exit_trail     停損已移到成本之上（吊燈鎖利）→ 移動停利出場（獲利）
          exit_breakeven 停損在成本附近 → 保本出場（約略打平，多為 scale-out 後）
          exit_sl        跌破成本 → 真停損（虧損）
        """
        d = self.dir
        if (d == 1 and price >= self.tp) or (d == -1 and price <= self.tp):
            return "exit_tp"
        pnl_per = (price - self.entry_price) * d        # 帶號：>0 獲利 / <0 虧損
        tol = abs(self.entry_price) * 0.0005            # 0.05% 內視為打平（保本）
        if pnl_per > tol:
            return "exit_trail"
        if pnl_per >= -tol:
            return "exit_breakeven"
        return "exit_sl"

    # ── 交易所掛單式硬停損：掛單 / 換單 / 撤單 / 對帳（皆受 _exchange_stop 旗標控制）──
    def _rounded_sl(self):
        """掛到交易所的 SL 價（對齊 tick）。比較用，避免 sub-tick 漂移造成每根無謂換單。"""
        try:
            return self.execu.round_price(self.sl)
        except Exception:                           # noqa: BLE001 — execu 無 round_price 時退回原值
            return self.sl

    def _recover_oid(self, order_type):
        """掛單回應遺漏 orderId（如 HTTP 2xx 空 body）時，從交易所掛單清單反查補回，防孤兒疊單。"""
        try:
            for o in self.execu.open_orders() or []:
                if str(o.get("type")) == order_type:
                    return o.get("orderId")
        except Exception:                           # noqa: BLE001
            pass
        return None

    def _place_protective(self, bar_time) -> None:
        """進場後掛 STOP_MARKET@sl + TAKE_PROFIT_MARKET@tp（closePosition）。失敗不影響本地軟停損。"""
        if not self._exchange_stop or self.dir == 0:
            return
        # 現價已穿越 SL/TP（掛了會被幣安 -2021 'would immediately trigger' 拒）→ 改直接市價平倉，不留裸倉
        try:
            cur = float(self.execu.mark_price())
        except Exception:                           # noqa: BLE001 — 取價失敗退回原本 try/except 掛單
            cur = None
        if cur is not None:
            crossed_sl = (self.dir == 1 and cur <= self.sl) or (self.dir == -1 and cur >= self.sl)
            crossed_tp = (self.dir == 1 and cur >= self.tp) or (self.dir == -1 and cur <= self.tp)
            if crossed_sl or crossed_tp:
                reason = "exit_tp" if crossed_tp else self._classify_exit(cur)
                print(f"[{bar_time}] [掛單] 現價 {cur:.2f} 已穿越 {'TP' if crossed_tp else 'SL'}，改市價平倉")
                self._go_flat(cur, bar_time, reason)
                return
        self._try_place_stop()
        self._try_place_tp()
        if self._stop_oid or self._tp_oid:
            print(f"[{bar_time}] 交易所掛單保護 STOP@{self.sl:.2f}(#{self._stop_oid}) / "
                  f"TP@{self.tp:.2f}(#{self._tp_oid})")

    def _try_place_stop(self) -> None:
        """掛 STOP 三段式：closePosition → 被拒認養既有單 → 再失敗改帶量 reduceOnly。

        -4130（每方向僅一張 closePosition 單）的兩種來源都蓋到：
        撤舊→掛新競態（舊單稍後消失，認養即可）、testnet 幽靈單（查不到撤不掉，
        認養無果 → 帶量單不受唯一性限制，保護不中斷）。全失敗仍有軟停損後備。
        """
        err = None
        try:
            r = self.execu.place_stop(self.dir, self.sl)
            self._stop_oid = r.get("orderId") if isinstance(r, dict) else None
            if self._stop_oid is None:
                self._stop_oid = self._recover_oid("STOP_MARKET")   # 空 body → 反查補回
            self._stop_sl = self._rounded_sl()
            return
        except Exception as e:                      # noqa: BLE001
            err = e
            self._stop_oid = self._recover_oid("STOP_MARKET")
            if self._stop_oid is not None:
                self._stop_sl = self._rounded_sl()
                print(f"[掛單] 掛停損被拒，認養既有單 #{self._stop_oid}：{e}")
                return
        try:
            r = self.execu.place_stop(self.dir, self.sl, qty=abs(self.qty))
            self._stop_oid = r.get("orderId") if isinstance(r, dict) else None
            if self._stop_oid is None:
                self._stop_oid = self._recover_oid("STOP_MARKET")   # 2xx 空 body → 反查
            self._stop_sl = self._rounded_sl()
            print(f"[掛單] closePosition 被拒 → 帶量 reduceOnly STOP(#{self._stop_oid})")
        except Exception as e2:                     # noqa: BLE001
            self._stop_oid = None
            print(f"[掛單] 掛停損失敗（仍有軟停損後備）：{err} / 帶量後備：{e2}")

    def _try_place_tp(self) -> None:
        """掛 TP 三段式（同 _try_place_stop）：closePosition → 認養 → 帶量 reduceOnly。"""
        err = None
        try:
            r = self.execu.place_take_profit(self.dir, self.tp)
            self._tp_oid = r.get("orderId") if isinstance(r, dict) else None
            if self._tp_oid is None:
                self._tp_oid = self._recover_oid("TAKE_PROFIT_MARKET")
            return
        except Exception as e:                      # noqa: BLE001
            err = e
            self._tp_oid = self._recover_oid("TAKE_PROFIT_MARKET")
            if self._tp_oid is not None:
                print(f"[掛單] 掛停利被拒，認養既有單 #{self._tp_oid}：{e}")
                return
        try:
            r = self.execu.place_take_profit(self.dir, self.tp, qty=abs(self.qty))
            self._tp_oid = r.get("orderId") if isinstance(r, dict) else None
            if self._tp_oid is None:
                self._tp_oid = self._recover_oid("TAKE_PROFIT_MARKET")   # 2xx 空 body → 反查
            print(f"[掛單] closePosition 被拒 → 帶量 reduceOnly TP(#{self._tp_oid})")
        except Exception as e2:                     # noqa: BLE001
            self._tp_oid = None
            print(f"[掛單] 掛停利失敗：{err} / 帶量後備：{e2}")

    def _cancel_protective(self) -> None:
        """撤掉本地記錄的 STOP/TP 掛單（容忍已成交/不存在）。平倉收尾用。"""
        if not self._exchange_stop:
            return
        for oid in (self._stop_oid, self._tp_oid):
            if oid is not None:
                try:
                    self.execu.cancel_order(oid)
                except Exception as e:              # noqa: BLE001 — 多半已觸發/不存在，忽略
                    print(f"[掛單] 撤單 {oid} 失敗（多半已觸發）：{e}")
        self._stop_oid = self._tp_oid = self._stop_sl = None

    def _sync_protective_stop(self, bar_time) -> None:
        """self.sl 變動後（吊燈/scale-out 移成本）→ cancel 舊 STOP、掛新 STOP。

        兼作每根 K 棒的「自癒」：STOP 缺（曾掛失敗）→ 補掛；TP 缺 → 一併補掛
        （原版 TP 掛失敗後永不再試 → 只剩每根 K 棒的軟停利）。
        """
        if not self._exchange_stop or self.dir == 0:
            return
        # TP 自癒：缺單就補（獨立於 STOP 換單邏輯；原版 TP 掛失敗後永不再試）
        if self._tp_oid is None and self.tp:
            self._try_place_tp()
            if self._tp_oid:
                print(f"[{bar_time}] [掛單] 補掛停利 TP@{self.tp:.2f}(#{self._tp_oid})")
        if self._stop_sl is not None and self._rounded_sl() == self._stop_sl:
            return                                  # 對齊 tick 後同價 → 不換單（避免 sub-tick churn）
        if self._stop_oid is not None:
            try:
                self.execu.cancel_order(self._stop_oid)
            except Exception as e:                  # noqa: BLE001
                print(f"[掛單] 換停損撤舊單失敗：{e}")
            self._stop_oid = None
        self._try_place_stop()
        if self._stop_oid:
            print(f"[{bar_time}] 移動停損→交易所換單 STOP@{self.sl:.2f}(#{self._stop_oid})")

    def _reconcile_exit(self, price, bar_time) -> str:
        """交易所掛單已平倉（本地以為持倉、實際 amt≈0）→ 補記平倉、清狀態，不重複下市價單。

        判 fill：優先查交易所成交真相（哪張 oid FILLED + avgPrice），消除「用現價猜方向」在
        wick/whipsaw 時把停損誤記成停利、PnL 正負號翻轉的問題；查不到才退回用現價保守判。
        以 _classify_exit(fill) 細分（停利目標/移動停利/保本/真停損）。回傳結單原因字串。
        """
        d = self.dir
        fill = reason = None
        # ① 交易所真相：哪張條件單 FILLED，用其 avgPrice
        for oid, kind in ((self._tp_oid, "tp"), (self._stop_oid, "stop")):
            if oid is None:
                continue
            try:
                o = self.execu.get_order(oid)
            except Exception:                       # noqa: BLE001 — execu 無 get_order/查單失敗 → 跳過
                o = None
            if o and str(o.get("status")) == "FILLED":
                ap = o.get("avgPrice") or o.get("price")
                try:
                    fill = float(ap) if ap not in (None, "", "0", 0) else None
                except (TypeError, ValueError):
                    fill = None
                if fill is None:
                    fill = self.tp if kind == "tp" else self.sl
                reason = "exit_tp" if kind == "tp" else self._classify_exit(fill)
                break
        # ② fallback：查不到真相 → 用現價保守判（原邏輯）
        if fill is None:
            hit_tp = (d == 1 and price >= self.tp) or (d == -1 and price <= self.tp)
            fill = self.tp if hit_tp else self.sl
            reason = "exit_tp" if hit_tp else self._classify_exit(fill)
        pnl = ((fill - self.entry_price) * abs(self.qty) * d
               - self._round_trip_fee(self.qty, self.entry_price, fill))   # 扣雙邊 taker 費（OPT-01）
        self.journal.log(reason, fill, abs(self.qty), pnl, ts=bar_time)
        # F4：熔斷/護欄以「整筆淨損益」計（剩餘倉 pnl + scale-out 已實現獲利），
        # 否則 scale-out 賺一半、剩餘半倉小虧的淨賺交易會被記成連虧 → 假熔斷停機。
        net_pnl = pnl + self._scaled_pnl
        self.cb.record_trade(net_pnl)
        self._dcg.record_trade(d, net_pnl)
        print(f"[{bar_time}] [對帳] 交易所掛單已平倉 {reason} @ {fill:.2f}（pnl {pnl:+.2f}）")
        # 殘留掛單（closePosition 成交後幣安已自動撤另一張）→ 清本地記錄
        self._stop_oid = self._tp_oid = self._stop_sl = None
        self.dir, self.entry_price, self.sl, self.tp, self.qty = 0, 0.0, 0.0, 0.0, 0.0
        self.peak = self.trough = 0.0
        self._peak_pnl = 0.0                        # F3：漏清會讓下一筆新倉被盈利保底用舊峰盈秒平
        self._profit_above_since = None
        self._scaled_out = False
        self._scaled_pnl = 0.0                      # F4：出場後歸零，不污染下一筆的熔斷判定
        self._entry_sl_dist = 0.0
        self._guard.clear_position(self.cfg.strategy, self.cfg.symbol)
        self._save()
        return reason

    def _go_flat(self, price, bar_time, reason) -> None:
        self._cancel_protective()                   # 平倉前先撤殘留掛單，避免幽靈單
        # 用本地追蹤的 self.qty 而非 position_amt()：testnet API 在 scale_out 後
        # 有時間差，position_amt() 可能回 0 → close 被跳過 → 交易所倉位殘留 → 幽靈倉疊加。
        qty = abs(self.qty)
        if qty == 0:
            qty = abs(self.execu.position_amt())    # 兜底：self.qty 異常為 0 時才問交易所
        if qty > 0:
            self.execu.close(qty, self.dir)
            pnl = ((price - self.entry_price) * qty * self.dir          # dir 帶號 → 多空 pnl 方向正確
                   - self._round_trip_fee(qty, self.entry_price, price))   # 扣雙邊 taker 費（OPT-01）
            self.journal.log(reason, price, qty, pnl, ts=bar_time)
            # F4：熔斷/護欄以整筆淨損益計（剩餘倉 + scale-out 已實現），防淨賺被記連虧
            net_pnl = pnl + self._scaled_pnl
            self.cb.record_trade(net_pnl)             # Circuit Breaker 記錄本筆「淨」損益
            self._dcg.record_trade(self.dir, net_pnl)  # 方向感知通道護欄記錄（self.dir 此時仍是平倉方向）
            print(f"[{bar_time}] {reason} @ {price:.2f}")
        self.dir, self.entry_price, self.sl, self.tp, self.qty = 0, 0.0, 0.0, 0.0, 0.0
        self.peak = self.trough = 0.0
        self._peak_pnl = 0.0
        self._profit_above_since = None
        self._scaled_out = False
        self._scaled_pnl = 0.0                      # F4：出場後歸零，不污染下一筆的熔斷判定
        self._entry_sl_dist = 0.0
        self._guard.clear_position(self.cfg.strategy, self.cfg.symbol)
        self._save()

    def check_soft_stops(self, live_price: float):
        """每輪 poll 的軟停損/停利（以即時價）。回傳結單原因或 None。

        原本 SL/TP 只在每根 K 棒收盤檢查，盤中靠交易所掛單保護；但 testnet 條件單
        系統偶發失效（-4130 幽靈單 + 帶量單 2xx 空 body 實際未掛），倉位會裸奔一整根
        K 棒（4h）。此後備把保護粒度降到 poll 間隔（30-60s）。與交易所掛單並存安全：
        若掛單先成交，_go_flat 的 reduceOnly 平倉會被 -2022 拒、由對帳收尾，不會反向開倉。
        """
        if self.dir == 0 or not self.qty or not self.sl:
            return None
        hit = ((self.dir == 1 and (live_price <= self.sl or live_price >= self.tp)) or
               (self.dir == -1 and (live_price >= self.sl or live_price <= self.tp)))
        if not hit:
            return None
        reason = self._classify_exit(live_price)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[軟停損] 即時價 {live_price:.4f} 觸及 {'SL' if 'sl' in reason or 'breakeven' in reason or 'trail' in reason else 'TP'} → 市價平倉（{reason}）")
        self._go_flat(live_price, now, reason)
        return reason

    def check_profit_floor(self, live_price: float) -> bool:
        """盈利保底：浮盈達閾值並持續 profit_sustain_seconds 秒才結算（默認 5 秒）。

        觸發條件：
        1. 浮盈 >= min_profit_close_usdt 持續 sustain 秒 → 確認結算
        2. 峰盈曾達 70%、現在回落超 50% 且仍在盈 → 保底結算（無需計時）
        浮盈未達閾值則重置計時；設 0 則停用整個功能。
        """
        min_u = float(self.cfg.strategy_params.get("min_profit_close_usdt", 0))
        if min_u <= 0 or self.dir == 0 or not self.qty:
            self._profit_above_since = None
            return False
        sustain = float(self.cfg.strategy_params.get("profit_sustain_seconds", 5))
        floating_pnl = (live_price - self.entry_price) * abs(self.qty) * self.dir
        if floating_pnl > self._peak_pnl:
            self._peak_pnl = floating_pnl
        now = datetime.now(timezone.utc)
        if floating_pnl >= min_u:
            if self._profit_above_since is None:
                self._profit_above_since = now
                print(f"[盈利保底] 浮盈 {floating_pnl:.2f}U ≥ {min_u:.2f}U，開始計時 {sustain:.0f}s")
            elif (now - self._profit_above_since).total_seconds() >= sustain:
                print(f"[盈利保底] 持續 {(now-self._profit_above_since).total_seconds():.0f}s → 結算 "
                      f"浮盈 {floating_pnl:.2f}U")
                return True
        else:
            if self._profit_above_since is not None:
                print(f"[盈利保底] 浮盈 {floating_pnl:.2f}U 跌出閾值，計時重置")
            self._profit_above_since = None
            if self._peak_pnl >= min_u * 0.7 and 0 < floating_pnl < self._peak_pnl * 0.5:
                print(f"[盈利保底] 峰盈 {self._peak_pnl:.2f}U 回落至 {floating_pnl:.2f}U → 保底結算")
                return True
        return False

    def manual_close(self, now=None, reason="exit_manual"):
        """手動結算：使用者透過儀表板按鈕平掉當前持倉。

        ⚠️ 由主輪詢迴圈在主執行緒呼叫（HTTP 緒只寫旗標），與 on_bar_close 同緒、無下單競態。
        close-only 語意：只平倉、不暫停 bot，下一根若符合訊號可照常再進場。
        熔斷暫停中也照平（使用者明確要結算，不受 is_paused 擋）。
        """
        if self.dir == 0:
            return {"ok": False, "msg": "目前空手，無倉可平"}
        if now is None:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            price = self.execu.mark_price()
        except Exception:                       # noqa: BLE001 — 取價失敗退回進場價估算
            price = self.entry_price or 0.0
        closed_dir, qty = self.dir, abs(self.qty)
        self._go_flat(price, now, reason)
        print(f"[手動平倉] 已平 {'多' if closed_dir == 1 else '空'} {qty} @ {price:.2f}")
        return {"ok": True, "closed_dir": closed_dir, "qty": qty,
                "price": round(price, 2), "reason": reason}

    def _kelly_pct(self) -> float | None:
        """從 DB 讀近期平倉紀錄，計算 half-Kelly 倉位比例。樣本不足時回傳 None。

        ⚠️ 平倉與否記在 `side` 欄（exit_*），不是 `mode` 欄（mode 永遠是 live_futures_testnet）。
        早期誤用 mode="exit" 過濾 → 永遠 0 筆 → Kelly 形同停用；此處改以 side 前綴篩平倉。
        過濾鍵用已解析的 self.cfg（CLI 可覆蓋 env），避免 env 與 cfg 分歧造成跨 bot 污染。
        """
        try:
            from core.trade_journal import read_trades_db
            from core.risk_officer import kelly_fraction
            # F5：一併以本 bot 的 journal mode 過濾——同策略同幣種的本機 paper 交易
            # 寫進同一個 PG 時，不加 mode 篩會污染實盤 Kelly 倉位計算。
            mode = getattr(self.journal, "mode", None) or "live_futures_testnet"
            rows = read_trades_db(limit=200, mode=mode,
                                  strategy=self.cfg.strategy, symbol=self.cfg.symbol)
            pnl = [r["pnl"] for r in rows
                   if r.get("pnl") is not None and str(r.get("side", "")).startswith("exit")]
            # OPT-15：min_trades 提到 30（20 筆噪音過大）；加槓桿 bot 用 quarter-Kelly(0.25) 上限。
            lev = max(int(getattr(self.cfg, "futures_leverage", 1)), 1)
            max_k = 0.25 if lev > 1 else 0.5
            return kelly_fraction(pnl, min_trades=30, max_kelly=max_k)
        except Exception:
            return None

    def _open(self, price, bar_time, direction, atr=None) -> None:
        cfg = self.cfg
        bal = self.execu.balance(cfg.quote_asset)
        kelly_pct = self._kelly_pct()
        decision = self.risk.check_entry(bal, price, bar_time, direction=direction, atr=atr,
                                         kelly_pct=kelly_pct)
        if decision.allow and self._guard_max_dd > 0:
            # OPT-05：組合層回撤熔斷（先於暴露檢查；只擋新倉）
            pf_ok, pf_reason = self._guard.check_portfolio_drawdown(self._guard_max_dd)
            if not pf_ok:
                decision = type(decision)(False, 0.0, pf_reason)
        if decision.allow:
            notional = decision.quantity * price
            ok, reason = self._guard.check_exposure(
                self.cfg.strategy, direction, notional, self._guard_max,
                own_symbol=self.cfg.symbol,
                symbol_cap=self._guard_symbol_cap,
                corr_symbols=self._guard_corr_symbols)
            if not ok:
                decision = type(decision)(False, 0.0, reason)
        kelly_tag = f" Kelly={kelly_pct:.1%}" if kelly_pct is not None else ""
        self._last_risk = {"allow": bool(decision.allow),
                           "qty": round(float(decision.quantity), 6),
                           "reason": decision.reason + kelly_tag}
        if not decision.allow:
            print(f"[{bar_time}] 風控否決：{decision.reason}")
            return
        ok, msg = self.execu.valid_order(decision.quantity, price)
        if not ok:
            print(f"[{bar_time}] 風控通過但訂單不合法：{msg}")
            return
        if direction == 1:
            resp = self.execu.open_long(decision.quantity)
            side = "entry"
        else:
            resp = self.execu.open_short(decision.quantity)
            side = "entry_short"
        # F3：進場價以交易所實際成交均價為真相（journal/SL/TP/pnl 一致），
        # 拿不到（testnet 偶發空回應）才退回訊號棒收盤價，維持舊行為。
        fill = self.execu.fill_price(resp)
        price = fill if fill is not None else price
        self.dir = direction
        self.qty = decision.quantity                # 用本地下單量，不在開倉後立刻讀帶號倉位
        self.entry_price = price
        self.peak = self.trough = price             # Chandelier 進場後極值起點
        self.sl, self.tp = self.risk.exit_levels(price, direction, atr=atr)
        self._entry_sl_dist = abs(price - self.sl)  # 原始停損距離，scale-out 閾值計算用
        self._scaled_out = False                    # 新倉重設
        self._scaled_pnl = 0.0                      # F4：新倉從零起算
        self.journal.log(side, price, decision.quantity, 0.0, ts=bar_time)
        self._guard.upsert_position(self.cfg.strategy, self.cfg.symbol,
                                    direction, decision.quantity, price)
        self._place_protective(bar_time)            # 進場後掛交易所硬停損/停利（旗標關時 no-op）
        self._save()
        verb = "進場做多" if direction == 1 else "進場做空"
        print(f"[{bar_time}] {verb} ~{decision.quantity} @ {price:.2f} "
              f"(SL {self.sl:.2f} / TP {self.tp:.2f})")

    def on_bar_close(self, bar_time) -> None:
        cfg = self.cfg
        df = self.strat.prepare(
            fetch_klines(self.data_client, cfg.symbol, cfg.interval,
                         self._fetch_bars(), futures=True)).dropna()
        if len(df) < 2:                     # 指標暖機不足（如單調行情 swing 未確認）→ 本輪不決策
            print(f"[{bar_time}] 指標暖機不足（dropna 後僅 {len(df)} 根），本輪跳過")
            return
        row = df.iloc[-2]
        price = float(df["close"].iloc[-1])
        pos_before = self.dir
        anomaly = bool(detect_anomaly(df.iloc[:-1]))
        acts, risk_rec, target = [], None, None

        # 交易所掛單對帳：啟用且本地以為持倉、但交易所實際已無倉（STOP/TP 觸發平倉）
        # → 補記平倉、清狀態，不重複下市價單（防 bot 當機/熔斷期間的裸奔缺口）。
        if self._exchange_stop and self.dir != 0:
            dust = float(self.execu._filters["min_qty"])
            if abs(self.execu.position_amt()) <= dust:
                reason = self._reconcile_exit(price, bar_time)
                acts.append({"act": reason, "price": round(price, 2), "reconciled": True})

        # 方向感知通道護欄：每根 K 棒推進冷卻、依通道方向(fib_ch_dir)解封
        ch_dir = row.get("fib_ch_dir") if hasattr(row, "get") else (
            row["fib_ch_dir"] if "fib_ch_dir" in row.index else 0)
        try:
            ch_dir = 0 if ch_dir is None or pd.isna(ch_dir) else int(ch_dir)
        except (TypeError, ValueError):
            ch_dir = 0
        self._dcg.on_bar(ch_dir)

        # 信號工程師：擷取本根指標供 SOP 面板顯示（含 regime 閘門判斷依據）
        ind = {}
        for k in ("fib_pos", "fib_382", "fib_618", "rsi", "atr", "ema_fast", "ema_slow",
                  "ema_trend", "zscore", "er", "chop", "adx",
                  "st_dir", "supertrend", "taker_ratio_s",
                  "dc_upper", "dc_lower", "dc_exit_long", "dc_exit_short"):
            if k in row.index:
                v = row[k]
                ind[k] = None if pd.isna(v) else round(float(v), 4)
        if "regime" in row.index and row["regime"] is not None:   # regime 是字串（trend/range），不取整
            ind["regime"] = str(row["regime"])

        # 風控官：方向性停損停利「永遠先執行」，不受暴量抑制
        if self.dir == 1 and (price <= self.sl or price >= self.tp):
            reason = self._classify_exit(price)
            self._go_flat(price, bar_time, reason)
            acts.append({"act": reason, "price": round(price, 2)})
        elif self.dir == -1 and (price >= self.sl or price <= self.tp):
            reason = self._classify_exit(price)
            self._go_flat(price, bar_time, reason)
            acts.append({"act": reason, "price": round(price, 2)})

        # 熔斷暫停：上方「方向性停損停利 + 對帳」已先執行（持倉不裸奔），此後只擋新進場/加碼
        if self.cb.is_paused():
            print(f"[{bar_time}] [熔斷] 暫停中（停損停利已先檢查），跳過新進場/加碼")
            acts.append({"act": "cb_paused"})
            self._write_sop(price, bar_time, row, ind, pos_before, target, risk_rec, acts, anomaly)
            return

        if anomaly:
            acts.append({"act": "skip_anomaly"})
            print(f"[{bar_time}] 偵測到暴量異常，本輪跳過下單")
        else:
            target = self.strat.signal(row, self.dir)
            if target != self.dir:
                if self.dir != 0:
                    self._go_flat(price, bar_time, "exit_signal")
                    acts.append({"act": "exit_signal", "price": round(price, 2)})
                if target in (1, -1) and self._dcg.blocks(target):
                    side_txt = "做多" if target == 1 else "做空"
                    print(f"[{bar_time}] [通道護欄] 連續{side_txt}虧損暫停中，跳過進場")
                    acts.append({"act": "dcg_blocked", "dir": target})
                    target = 0
                if target in (1, -1):
                    self._last_risk = None
                    atr_val = float(row["atr"]) if "atr" in row.index and not pd.isna(row["atr"]) else None
                    # ML Filter 機率門檻（模型未載入則直接通過）
                    if self._ml_model is not None:
                        try:
                            from ml.ml_filter import extract_features, signal_proba
                            feats = extract_features(df.iloc[:-1], pd.DatetimeIndex([row.name]))
                            p = signal_proba(self._ml_model, feats)
                            if p < self._ml_threshold:
                                print(f"[{bar_time}] ML Filter 否決（p={p:.2f} < {self._ml_threshold:.2f}）")
                                acts.append({"act": "ml_rejected", "proba": round(p, 3)})
                                target = 0
                        except Exception as e:
                            print(f"[{bar_time}] ML Filter 推論失敗，允許通過：{e}")
                    if target in (1, -1):
                        self._open(price, bar_time, target, atr=atr_val)
                    risk_rec = self._last_risk
                    if self.dir == target:      # _open 成功
                        side = "entry" if target == 1 else "entry_short"
                        acts.append({"act": side, "price": round(price, 2),
                                     "qty": self.qty, "sl": round(self.sl, 2), "tp": round(self.tp, 2)})
                    else:
                        reason = risk_rec.get("reason", "驗證失敗") if risk_rec else "未知"
                        acts.append({"act": "rejected", "reason": reason})
            else:
                dir_txt = {1: "持多", -1: "持空", 0: "空手"}[self.dir]
                acts.append({"act": "hold" if self.dir != 0 else "flat"})
                print(f"[{bar_time}] {dir_txt} | 價 {price:.2f}")

        # Scale-out：浮盈達 0.5R 時平一半倉、停損移到進場成本（保本）
        # use_scale_out=false → 跳過（均值回歸策略直接等 TP 全出，不提前分批）
        _use_scale_out = self.cfg.strategy_params.get("use_scale_out", True)
        if _use_scale_out and self.dir != 0 and not self._scaled_out and self._entry_sl_dist > 0:
            if self.risk.check_scale_out(
                self.entry_price, price, self._entry_sl_dist, self.dir, self._scaled_out
            ):
                # 用 floor 後的實際送出量記帳，避免 self.qty 與交易所殘量 sub-step 漂移
                try:
                    half_qty = float(self.execu.round_qty(self.qty / 2))
                except Exception:                   # noqa: BLE001 — execu 無 round_qty 時退回原值
                    half_qty = self.qty / 2
                ok, _msg = self.execu.valid_order(half_qty, price)
                if ok and half_qty > 0:
                    self.execu.close(half_qty, self.dir)
                    pnl_half = ((price - self.entry_price) * half_qty * self.dir
                                - self._round_trip_fee(half_qty, self.entry_price, price))  # 扣雙邊 taker 費（OPT-01）
                    self.journal.log("scale_out", price, half_qty, pnl_half, ts=bar_time)
                    self.qty -= half_qty
                    self.sl = self.entry_price      # 剩餘半倉停損移到成本（保本）
                    self._scaled_out = True
                    self._scaled_pnl += pnl_half    # F4：入袋獲利記下，出場時與剩餘倉合併判淨損益
                    # 同步共用 DB 暴露為減半後的真實倉量，避免他台 check_exposure 高估而誤擋進場
                    self._guard.upsert_position(self.cfg.strategy, self.cfg.symbol,
                                                self.dir, self.qty, self.entry_price)
                    self._save()
                    acts.append({"act": "scale_out", "price": round(price, 2),
                                 "qty": round(half_qty, 6)})
                    side_txt = "多" if self.dir == 1 else "空"
                    print(f"[{bar_time}] Scale Out {side_txt} — 平半倉 {half_qty:.6f} "
                          f"@ {price:.2f}，SL 移至成本 {self.entry_price:.2f}")

        # Chandelier 追蹤停損：用「這根已收盤」(row=iloc[-2]) 的極值與 ATR 更新 self.sl，
        # 供「下一根」判定觸發（只升不降/只降不升、不用未收盤即時值，故無 look-ahead）。
        atr_now = float(row["atr"]) if "atr" in row.index and not pd.isna(row["atr"]) else None
        if self.dir == 1 and atr_now is not None:
            self.peak = max(self.peak, float(row["high"]))
            self.sl = self.risk.update_trailing_stop(self.sl, self.peak, atr_now, 1)
        elif self.dir == -1 and atr_now is not None:
            self.trough = min(self.trough, float(row["low"]))
            self.sl = self.risk.update_trailing_stop(self.sl, self.trough, atr_now, -1)

        # self.sl 若被 scale-out/吊燈移動 → 同步交易所掛單式停損（cancel/replace；旗標關時 no-op）
        self._sync_protective_stop(bar_time)

        self._write_sop(price, bar_time, row, ind, pos_before, target, risk_rec, acts, anomaly)

    def _write_sop(self, price, bar_time, row, ind, pos_before, target, risk_rec, acts, anomaly) -> None:
        """每根 K 線結束後，把 SOP 決策記錄寫入 bot_state_futures.json，前端即時讀取。"""
        try:
            equity = round(self.execu.balance(self.cfg.quote_asset), 2)
        except Exception:
            equity = None

        # OPT-05：把本台當前淨值寫進共用 DB，供組合層回撤熔斷彙總（fail-open，啟用與否都寫不傷）
        if equity is not None:
            self._guard.upsert_equity(self.cfg.strategy, self.cfg.symbol, equity)

        # 讀回上次持久化（取 last_balance 比較；prev 也保住未在此函式更新的欄位語意）
        prev = BotState.load(self.state_path)

        # 測試網重置偵測：餘額大幅下滑 → 清空持倉狀態（含共用 DB 殘列，避免幽靈暴露）
        if equity is not None and detect_testnet_reset(current=equity, last=prev.last_balance):
            print(f"[{bar_time}] ⚠️  測試網重置偵測：餘額 {equity:.2f} << {prev.last_balance:.2f}，清空持倉狀態")
            self.dir = 0
            self.entry_price = self.sl = self.tp = self.qty = 0.0
            self.peak = self.trough = 0.0
            self._scaled_out = False
            self._entry_sl_dist = 0.0
            self._scaled_pnl = 0.0                                           # F4：重置一併歸零
            self._guard.clear_position(self.cfg.strategy, self.cfg.symbol)   # 清共用 DB 殘列
            self._cancel_protective()                                        # 撤殘留掛單（旗標關時 no-op）
            self.risk.reset_equity_peak()   # R1：不清會讓峰值回撤熔斷永久誤觸、bot 默默停擺
            self._guard.clear_equity()      # F6：組合層淨值/峰值殘留不清 → kill-switch 啟用時永久擋新倉
        last_balance = equity if equity is not None else prev.last_balance

        last_decision = {
            "ts": str(bar_time),
            "price": round(price, 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "volume": (round(float(row["volume"]), 2) if "volume" in row.index else None),
            "anomaly": anomaly,
            "ind": ind,
            "pos_before": pos_before,
            "target": target,
            "risk": risk_rec,
            "actions": acts,
            "pos_after": self.dir,
            "equity": equity,
        }
        # 關鍵：以完整 BotState 為底寫檔（保住 cb/dcg/scaled_out/entry_sl_dist/stop_oid/tp_oid/last_balance），
        # 再疊上前端顯示欄位。否則每根 K 棒覆寫會抹掉持久化欄位 → 重啟熔斷/護欄/scale-out 全歸零。
        cb_dict = self.cb.to_dict()
        bs = BotState(
            in_position=self.dir != 0, direction=self.dir, entry_price=self.entry_price,
            sl=self.sl, tp=self.tp, qty=abs(self.qty),
            symbol=self.cfg.symbol, strategy=self.cfg.strategy,
            cb_consecutive_losses=cb_dict["consecutive_losses"],
            cb_paused_until=cb_dict["paused_until"] or "",
            dcg_state=json.dumps(self._dcg.to_dict()),
            last_balance=last_balance,
            scaled_out=self._scaled_out, scaled_pnl=self._scaled_pnl,
            entry_sl_dist=self._entry_sl_dist,
            stop_oid=str(self._stop_oid or ""), tp_oid=str(self._tp_oid or ""),
        )
        state = {
            **asdict(bs),
            "cash": equity,            # USDT 保證金餘額（前端顯示用）
            "base": abs(self.qty),     # 持幣量（前端未實現損益估算用）
            "interval": self.cfg.interval,
            "last_price": price,
            "poll": self.cfg.poll_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_decision": last_decision,
            "mode": "futures",
        }
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, default=str)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    def restored_last_bar(self):
        """重啟續跑：這根 K 棒「已決策過」的最佳證據，poll 迴圈以此初始化 last_bar。

        優先序：
          1. state 檔 last_decision.ts（同容器重啟；最精確——含「決策過但沒交易」的棒）。
             symbol/interval 與現行設定不符（config 剛換過市場/週期）→ 不採用。
          2. journal 最新一筆成交推斷（2026-07-06 稽核 F2：Railway redeploy 是全新
             容器、state 檔消失，若直接視為全新起跑會對「已決策過的當前棒」再決策
             一次 → 空手且訊號仍在就重複進場。b7 實測同棒進場 3 次 ×2 輪）。
        都沒有 → None（全新起跑，決策當前棒）。
        """
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            if (st.get("symbol") == self.cfg.symbol
                    and st.get("interval") == self.cfg.interval):
                ts = (st.get("last_decision") or {}).get("ts")
                if ts:
                    return pd.Timestamp(ts)
        except Exception:                       # noqa: BLE001 — 缺檔/壞檔 → 走 journal 推斷
            pass
        return self._last_bar_from_journal()

    def _last_bar_from_journal(self):
        """由 journal（持久 DB，redeploy 不消失）推斷最後已行動的 K 棒。

        entry / on_bar_close 記帳列的 ts＝決策棒 open_time（正好落在棒界）→ 直接採用；
        盤中軟停損/對帳出場列的 ts＝牆鐘時間（棒中）→ 換算成「當下已收完的那根」
        ＝floor − 1 根（該筆行動源自那根棒的決策脈絡）。只涵蓋「有交易」的棒——
        「決策過但沒動作」的棒重複決策是冪等的（同資料同結論），無害。
        """
        try:
            from core.trade_journal import read_trades_db
            rows = read_trades_db(limit=1, mode=getattr(self.journal, "mode", None),
                                  strategy=self.cfg.strategy, symbol=self.cfg.symbol,
                                  db_path=getattr(self.journal, "db_path", "trades.db"))
            if not rows:
                return None
            ts = pd.Timestamp(str(rows[0]["ts"]))
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            freq = _interval_freq(self.cfg.interval)
            bar = ts.floor(freq)
            return bar if ts == bar else bar - pd.Timedelta(freq)
        except Exception:                       # noqa: BLE001 — DB 不可用 → 全新起跑
            return None

    def _heartbeat(self, price: float | None = None) -> None:
        """同一根 K 線的輪詢週期：只更新 updated_at + 現價，維持前端綠燈。"""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                st = json.load(f)
            if not isinstance(st, dict):
                return
            st["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if price is not None:
                st["last_price"] = price
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f, default=str)
            os.replace(tmp, self.state_path)
        except Exception:
            pass


def _read_trades_json(path: str) -> bytes:
    """GET /trades?limit=N&mode=M → 從 PostgreSQL 或 SQLite 讀近期交易，回傳 JSON bytes。

    用 BOT_STRATEGY + BOT_SYMBOL env var 過濾，確保每台 bot 只回傳自己的紀錄。
    （兩台 bot 跑同一策略但不同標的時，僅靠 strategy 會撈到對方的紀錄。）
    """
    import urllib.parse
    from core.trade_journal import read_trades_db
    qs       = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    limit    = int(qs.get("limit", ["50"])[0])
    mode     = qs.get("mode", [None])[0]
    strategy = os.getenv("BOT_STRATEGY")   # 各 service 自己的策略名
    symbol   = os.getenv("BOT_SYMBOL")     # 各 service 自己的標的
    try:
        rows = read_trades_db(limit=limit, mode=mode, strategy=strategy,
                              symbol=symbol)
        return json.dumps(rows, default=str).encode()
    except Exception:
        return b"[]"


def _start_state_server() -> None:
    """Railway 注入 $PORT 時，在該 port 開 HTTP 狀態端點供本機前端讀取。
    本機開發不設 PORT → 跳過，不影響原有流程。"""
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        port = int(port_str)
    except ValueError:
        return

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                body = b'{"ok":true}'
            elif self.path in ("/", "/state"):
                try:
                    with open(STATE_PATH, "rb") as f:
                        body = f.read()
                except FileNotFoundError:
                    body = b"{}"
            elif self.path.startswith("/trades"):
                body = _read_trades_json(self.path)
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _reply(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # 手動平倉：寫旗標檔，由主輪詢迴圈在主執行緒實際平倉（HTTP 緒不直接下單）。
            if self.path == "/close":
                token = os.getenv("CLOSE_TOKEN", "")
                if not _close_authorized(self.headers.get("X-Close-Token"), token):
                    # 未設 CLOSE_TOKEN → 端點停用；token 不符 → 拒絕（bot 端點公開，必須擋）
                    self._reply(403, {"ok": False, "msg": "未授權（CLOSE_TOKEN 未設或不符）"})
                    return
                try:
                    with open(CLOSE_REQUEST_PATH, "w") as f:
                        f.write(datetime.now(timezone.utc).isoformat(timespec="seconds"))
                    _CLOSE_EVENT.set()   # 即時喚醒主迴圈，不等下一個 poll 週期
                    self._reply(200, {"ok": True, "queued": True, "msg": "已排入平倉，下一輪執行"})
                except OSError as e:
                    self._reply(500, {"ok": False, "msg": f"寫入平倉旗標失敗：{e}"})
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *_):
            pass  # suppress per-request stdout noise

    server = socketserver.TCPServer(("0.0.0.0", port), _Handler)
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[狀態 API] 監聽 :{port}  GET /state → {STATE_PATH}")


def _make_ws_handler(trader, last_closed: list, lock) -> callable:
    """WS callback 工廠（可獨立測試）：每 tick 刷新心跳；K 棒收盤才觸發決策。

    Args:
        trader: FuturesLiveTrader 實例
        last_closed: 長度 1 的 list，儲存上一根已處理的 K 棒時間戳（去重用）
        lock: threading.Lock，保護 trader 方法不跨緒競態
    """
    def handle(msg):
        k = msg.get("k") if isinstance(msg, dict) else None
        if not k:
            return
        live_price = float(k.get("c", 0))
        with lock:
            trader._heartbeat(live_price)
        if not k.get("x"):        # K 棒未收盤，只更新心跳
            return
        bt = k.get("t")
        if bt == last_closed[0]:  # 去重：同一根 K 棒的重播
            return
        last_closed[0] = bt
        try:
            with lock:
                trader.on_bar_close(pd.to_datetime(bt, unit="ms"))
        except Exception:
            print("[錯誤/WS]", traceback.format_exc())
    return handle


def _ws_main(trader, cfg) -> None:
    """WebSocket 主迴圈：Binance 合約 K 線事件驅動，心跳毫秒級更新。

    交易邏輯（on_bar_close）仍在 K 棒收盤後才觸發，策略參數不需改變。
    手動平倉旗標由主執行緒每 5 秒檢查，確保在主緒執行。
    """
    from binance import ThreadedWebsocketManager

    _trade_lock = threading.Lock()
    last_closed = [None]
    handle = _make_ws_handler(trader, last_closed, _trade_lock)

    twm = ThreadedWebsocketManager(
        api_key=cfg.futures_api_key, api_secret=cfg.futures_api_secret, testnet=True
    )
    twm.start()
    twm.start_kline_futures_socket(callback=handle, symbol=cfg.symbol, interval=cfg.interval)
    print(f"[WS] 訂閱合約 K 線 {cfg.symbol}@kline_{cfg.interval}，K 棒收盤即觸發決策…")

    try:
        while True:
            _CLOSE_EVENT.wait(timeout=5)
            _CLOSE_EVENT.clear()
            if os.path.exists(CLOSE_REQUEST_PATH):
                try:
                    os.remove(CLOSE_REQUEST_PATH)
                except OSError:
                    pass
                with _trade_lock:
                    print("[手動平倉] 收到結算請求", trader.manual_close())
    except KeyboardInterrupt:
        print("\n[結束] 使用者中斷")
        twm.stop()


def main():
    # 合併部署：設了 BOTS_CONFIG（JSON 陣列）→ 委派多 bot 監督器，同一 start command
    # 即可跑合併 service（純 env var 切換、無需改 Railway start command）。
    # 未設 → 照舊走單台路徑，Bot1/Bot2 行為完全不變。
    if os.getenv("BOTS_CONFIG", "").strip():
        import run_multi_futures
        return run_multi_futures.main()

    cfg = Config()
    ap = argparse.ArgumentParser(description="合約測試網模擬盤（多/空，可做空）。")
    # 預設值從環境變數讀取（雲端多服務用 BOT_* 變數區分；不依賴 shell 對 startCommand 的插值，
    # 因 Railway 不一定會展開 ${VAR}）。CLI 參數仍可覆蓋環境變數。
    ap.add_argument("--strategy", default=os.getenv("BOT_STRATEGY", "supertrend"),
                    help="建議用 supertrend / donchian / fib_retracement / zscore_ls（支援做空）")
    ap.add_argument("--symbol", default=os.getenv("BOT_SYMBOL", "BTCUSDT"))
    ap.add_argument("--interval", default=os.getenv("BOT_INTERVAL", "4h"))
    ap.add_argument("--leverage", type=int, default=int(os.getenv("BOT_LEV", "3")))
    ap.add_argument("--poll", type=int, default=int(os.getenv("BOT_POLL", "30")))
    ap.add_argument("--budget", type=float, default=float(os.getenv("BOT_BUDGET", "500")),
                    help="每筆最大倉位（USDT）。由帳戶餘額動態算出 max_position_pct。")
    ap.add_argument("--params", default=os.getenv("BOT_PARAMS", ""),
                    help='策略參數 JSON，例如 \'{"use_htf_filter": true, "htf_ema_period": 200}\'')
    ap.add_argument("--cb-max-losses", type=int,
                    default=int(os.getenv("CB_MAX_LOSSES", "3")),
                    help="Circuit Breaker：連續虧損幾筆後暫停（預設 3）")
    ap.add_argument("--cb-pause-hours", type=float,
                    default=float(os.getenv("CB_PAUSE_HOURS", "24")),
                    help="Circuit Breaker：暫停幾小時（預設 24）")
    ap.add_argument("--ws", action="store_true",
                    default=bool(int(os.getenv("BOT_WS", "0"))),
                    help="改用 WebSocket K 線串流（價格毫秒級即時；不影響交易信號時間框架）")
    args = ap.parse_args()
    cfg.strategy, cfg.symbol, cfg.interval = args.strategy, args.symbol, args.interval
    cfg.futures_leverage, cfg.poll_seconds = args.leverage, args.poll
    cfg.strategy_params = {**cfg.strategy_params, **parse_bot_params(args.params)}
    _risk = os.getenv("RISK_PER_TRADE")
    if _risk:
        cfg.risk_per_trade = float(_risk)

    # 健康/狀態 HTTP 伺服器「最先」啟動：不等任何幣安 API（exchange_info/leverage/balance），
    # 確保雲端 healthcheck 在啟動初期就能通過；金鑰缺失或交易初始化失敗都不能讓 process 結束。
    _start_state_server()

    if not cfg.futures_api_key or not cfg.futures_api_secret:
        print("找不到合約測試網金鑰。請到 https://testnet.binancefuture.com 產生，"
              "並在 .env 填入 BINANCE_FUTURES_TESTNET_API_KEY / _SECRET。")
        while True:          # 保持存活，healthcheck 仍通過，便於在 Railway console 診斷
            time.sleep(30)

    try:
        client = make_client(cfg.futures_api_key, cfg.futures_api_secret, testnet=True)
        # 訊號資料 client：預設主網公開 K 線（與回測/驗證同源），下單仍走測試網 client。
        data_client = make_data_client(os.getenv("SIGNAL_DATA_SOURCE", "mainnet"))
        execu = FuturesExecutionEngineer(client, cfg.symbol, leverage=cfg.futures_leverage)

        # --budget：從真實餘額動態算出 max_position_pct，把每筆倉位釘在指定 USDT 上限
        balance = execu.balance(cfg.quote_asset)
        if args.budget is not None and balance > 0:
            cfg.max_position_pct = min(args.budget / balance, 1.0)

        strat = build_strategy(cfg.strategy, **cfg.strategy_params)
        risk = RiskOfficer(cfg)
        journal = TradeJournal(db_path="trades.db", csv_path="trades_futures.csv",
                               mode="live_futures_testnet", symbol=cfg.symbol, strategy=cfg.strategy)
        ml_path = os.getenv("ML_FILTER_PATH", f"models/{cfg.strategy}.pkl")
        trader = FuturesLiveTrader(cfg, client, strat, risk, execu, journal,
                                   cb_max_losses=args.cb_max_losses,
                                   cb_pause_hours=args.cb_pause_hours,
                                   ml_model_path=ml_path,
                                   ml_threshold=float(os.getenv("ML_THRESHOLD", "0.55")),
                                   data_client=data_client)

        budget_msg = f" | 預算上限 {args.budget:.0f}U/筆（max_pos={cfg.max_position_pct:.1%}）" if args.budget else ""
        print(f"[啟動] 合約測試網模擬盤（多/空）| {cfg.symbol} {cfg.interval} | "
              f"策略 {cfg.strategy} | 槓桿 {cfg.futures_leverage}x{budget_msg}")
        print(f"合約 USDT 餘額：{balance:.2f}")
        trader.restore()
    except Exception:                          # 交易初始化失敗：印出原因並保持存活（healthcheck 通過、便於診斷）
        print("[致命] 交易初始化失敗（保持存活供診斷）：\n" + traceback.format_exc())
        while True:
            time.sleep(30)

    if args.ws:
        _ws_main(trader, cfg)
        return

    last_bar = trader.restored_last_bar()   # 重啟續跑：已決策過的 K 棒不重複決策
    while True:
        try:
            # 手動平倉旗標（儀表板按鈕 → HTTP /close 寫入）：主執行緒平倉，無跨緒競態。
            if os.path.exists(CLOSE_REQUEST_PATH):
                try:
                    os.remove(CLOSE_REQUEST_PATH)
                except OSError:
                    pass
                print("[手動平倉] 收到結算請求", trader.manual_close())

            df = fetch_klines(data_client, cfg.symbol, cfg.interval, 3, futures=True)
            bar_time = df.index[-2]
            live_price = float(df["close"].iloc[-1])
            reason = trader.check_soft_stops(live_price)   # 每輪軟停損（掛單失效後備）
            if reason:
                print(f"[軟停損] 觸發（{reason}）@ {live_price}")
            if trader.check_profit_floor(live_price):
                print("[盈利保底] 觸發 manual_close", trader.manual_close())
            # 盈利保底計時中 → 縮短 poll 間隔到 5s 以快速確認持續；否則正常間隔
            wait_time = 5 if trader._profit_above_since is not None else cfg.poll_seconds
            if bar_time == last_bar:
                trader._heartbeat(live_price)   # 刷新 updated_at，前端綠燈不熄
                _CLOSE_EVENT.wait(timeout=wait_time)
                _CLOSE_EVENT.clear()
                continue
            last_bar = bar_time
            trader.on_bar_close(bar_time)
            _CLOSE_EVENT.wait(timeout=wait_time)
            _CLOSE_EVENT.clear()
        except KeyboardInterrupt:
            print("\n[結束] 使用者中斷")
            break
        except Exception:
            print("[錯誤]", traceback.format_exc())
            _CLOSE_EVENT.wait(timeout=cfg.poll_seconds)
            _CLOSE_EVENT.clear()


if __name__ == "__main__":
    main()
