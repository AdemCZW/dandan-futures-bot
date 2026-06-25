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
from datetime import datetime, timezone

import pandas as pd

from config import Config
from core.market_analyst import make_client, fetch_klines, detect_anomaly
from core.quant_researcher import build_strategy
from core.risk_officer import RiskOfficer
from core.futures_execution_engineer import FuturesExecutionEngineer
from core.trade_journal import TradeJournal
from core.bot_state import BotState
from core.circuit_breaker import CircuitBreaker

STATE_PATH = "bot_state_futures.json"


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
                 ml_threshold: float = 0.55):
        self.cfg, self.client, self.strat = cfg, client, strat
        self.risk, self.execu, self.journal = risk, execu, journal
        self.dir = 0
        self.entry_price = self.sl = self.tp = 0.0
        self.qty = 0.0                      # 本地追蹤的持倉量（避免開倉後立刻讀帶號倉位的最終一致性問題）
        self._last_risk = None              # _open() 執行後暫存風控決策供 SOP 讀取
        self.peak = self.trough = 0.0       # 進場後極值（Chandelier 追蹤停損用）
        self._scaled_out = False            # Scale-out：本輪持倉已部分獲利了結
        self._entry_sl_dist = 0.0          # 進場時的原始停損距離（scale-out 閾值用）
        self.cb = CircuitBreaker(max_losses=cb_max_losses, pause_hours=cb_pause_hours)
        # PortfolioGuard：跨 bot 同向暴露控制；max_notional 由 env 設定（預設 15000 USDT）
        from core.portfolio_guard import PortfolioGuard
        self._guard = PortfolioGuard()
        self._guard_max = float(os.getenv("PORTFOLIO_MAX_NOTIONAL", "15000"))
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
        BotState(in_position=self.dir != 0, direction=self.dir, entry_price=self.entry_price,
                 sl=self.sl, tp=self.tp, qty=abs(self.qty),
                 symbol=self.cfg.symbol, strategy=self.cfg.strategy,
                 cb_consecutive_losses=cb_dict["consecutive_losses"],
                 cb_paused_until=cb_dict["paused_until"] or "",
                 scaled_out=self._scaled_out,
                 entry_sl_dist=self._entry_sl_dist).save(STATE_PATH)

    def _latest_atr(self):
        """抓最近已收盤那根的 ATR（供 restore 重建停損用）；失敗則回 None 退回固定百分比。"""
        try:
            df = self.strat.prepare(
                fetch_klines(self.client, self.cfg.symbol, self.cfg.interval, 200, futures=True)).dropna()
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
        st = BotState.load(STATE_PATH)
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
            self._entry_sl_dist = st.entry_sl_dist
        else:
            self.dir, self.entry_price, self.sl, self.tp, self.qty = 0, 0.0, 0.0, 0.0, 0.0
            self.peak = self.trough = 0.0
            msg = "空手啟動（合約無未平倉部位）"
        # 從狀態檔還原 Circuit Breaker
        st = BotState.load(STATE_PATH)
        self.cb = CircuitBreaker.from_dict(
            {"consecutive_losses": st.cb_consecutive_losses,
             "paused_until": st.cb_paused_until or None},
            max_losses=self.cb.max_losses, pause_hours=self.cb.pause_hours)
        self._save()
        print(f"[狀態] {msg}")

    def _go_flat(self, price, bar_time, reason) -> None:
        amt = self.execu.position_amt()
        if abs(amt) > 0:
            self.execu.close(abs(amt), self.dir)
            pnl = (price - self.entry_price) * amt    # amt 帶號 → 多空 pnl 方向自動正確
            self.journal.log(reason, price, abs(amt), pnl, ts=bar_time)
            self.cb.record_trade(pnl)                 # Circuit Breaker 記錄本筆損益
            print(f"[{bar_time}] {reason} @ {price:.2f}")
        self.dir, self.entry_price, self.sl, self.tp, self.qty = 0, 0.0, 0.0, 0.0, 0.0
        self.peak = self.trough = 0.0
        self._scaled_out = False
        self._entry_sl_dist = 0.0
        self._guard.clear_position(self.cfg.strategy)
        self._save()

    def _kelly_pct(self) -> float | None:
        """從 DB 讀近 50 筆平倉紀錄，計算 half-Kelly 倉位比例。樣本不足時回傳 None。"""
        try:
            from core.trade_journal import read_trades_db
            from core.risk_officer import kelly_fraction
            strategy = os.getenv("BOT_STRATEGY")
            rows = read_trades_db(limit=50, mode="exit", strategy=strategy)
            pnl = [r["pnl"] for r in rows if r.get("pnl") is not None]
            return kelly_fraction(pnl, min_trades=20)
        except Exception:
            return None

    def _open(self, price, bar_time, direction, atr=None) -> None:
        cfg = self.cfg
        bal = self.execu.balance(cfg.quote_asset)
        kelly_pct = self._kelly_pct()
        decision = self.risk.check_entry(bal, price, bar_time, direction=direction, atr=atr,
                                         kelly_pct=kelly_pct)
        if decision.allow:
            notional = decision.quantity * price
            ok, reason = self._guard.check_exposure(
                self.cfg.strategy, direction, notional, self._guard_max)
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
            self.execu.open_long(decision.quantity)
            side = "entry"
        else:
            self.execu.open_short(decision.quantity)
            side = "entry_short"
        self.dir = direction
        self.qty = decision.quantity                # 用本地下單量，不在開倉後立刻讀帶號倉位
        self.entry_price = price
        self.peak = self.trough = price             # Chandelier 進場後極值起點
        self.sl, self.tp = self.risk.exit_levels(price, direction, atr=atr)
        self._entry_sl_dist = abs(price - self.sl)  # 原始停損距離，scale-out 閾值計算用
        self._scaled_out = False                    # 新倉重設
        self.journal.log(side, price, decision.quantity, 0.0, ts=bar_time)
        self._guard.upsert_position(self.cfg.strategy, self.cfg.symbol,
                                    direction, decision.quantity, price)
        self._save()
        verb = "進場做多" if direction == 1 else "進場做空"
        print(f"[{bar_time}] {verb} ~{decision.quantity} @ {price:.2f} "
              f"(SL {self.sl:.2f} / TP {self.tp:.2f})")

    def on_bar_close(self, bar_time) -> None:
        if self.cb.is_paused():
            print(f"[{bar_time}] [熔斷] 暫停中，跳過本輪決策")
            return

        cfg = self.cfg
        df = self.strat.prepare(
            fetch_klines(self.client, cfg.symbol, cfg.interval, 200, futures=True)).dropna()
        if len(df) < 2:                     # 指標暖機不足（如單調行情 swing 未確認）→ 本輪不決策
            print(f"[{bar_time}] 指標暖機不足（dropna 後僅 {len(df)} 根），本輪跳過")
            return
        row = df.iloc[-2]
        price = float(df["close"].iloc[-1])
        pos_before = self.dir
        anomaly = bool(detect_anomaly(df.iloc[:-1]))
        acts, risk_rec, target = [], None, None

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
            self._go_flat(price, bar_time, "exit_sltp")
            acts.append({"act": "exit_sltp", "price": round(price, 2)})
        elif self.dir == -1 and (price >= self.sl or price <= self.tp):
            self._go_flat(price, bar_time, "exit_sltp")
            acts.append({"act": "exit_sltp", "price": round(price, 2)})

        if anomaly:
            acts.append({"act": "skip_anomaly"})
            print(f"[{bar_time}] 偵測到暴量異常，本輪跳過下單")
        else:
            target = self.strat.signal(row, self.dir)
            if target != self.dir:
                if self.dir != 0:
                    self._go_flat(price, bar_time, "exit_signal")
                    acts.append({"act": "exit_signal", "price": round(price, 2)})
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
        if self.dir != 0 and not self._scaled_out and self._entry_sl_dist > 0:
            if self.risk.check_scale_out(
                self.entry_price, price, self._entry_sl_dist, self.dir, self._scaled_out
            ):
                half_qty = self.qty / 2
                ok, _msg = self.execu.valid_order(half_qty, price)
                if ok and half_qty > 0:
                    self.execu.close(half_qty, self.dir)
                    pnl_half = (price - self.entry_price) * half_qty * self.dir
                    self.journal.log("scale_out", price, half_qty, pnl_half, ts=bar_time)
                    self.qty -= half_qty
                    self.sl = self.entry_price      # 剩餘半倉停損移到成本（保本）
                    self._scaled_out = True
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

        self._write_sop(price, bar_time, row, ind, pos_before, target, risk_rec, acts, anomaly)

    def _write_sop(self, price, bar_time, row, ind, pos_before, target, risk_rec, acts, anomaly) -> None:
        """每根 K 線結束後，把 SOP 決策記錄寫入 bot_state_futures.json，前端即時讀取。"""
        try:
            equity = round(self.execu.balance(self.cfg.quote_asset), 2)
        except Exception:
            equity = None

        # 測試網重置偵測：餘額大幅下滑 → 清空持倉狀態
        if equity is not None:
            from core.bot_state import BotState, detect_testnet_reset
            st = BotState.load(STATE_PATH)
            if detect_testnet_reset(current=equity, last=st.last_balance):
                print(f"[{bar_time}] ⚠️  測試網重置偵測：餘額 {equity:.2f} << {st.last_balance:.2f}，清空持倉狀態")
                self.dir = 0
                self.entry_price = self.sl = self.tp = self.qty = 0.0
            # 更新 last_balance 並持久化
            st.last_balance = equity
            st.save(STATE_PATH)

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
        state = {
            "in_position": self.dir != 0,
            "direction": self.dir,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "qty": abs(self.qty),
            "cash": equity,         # USDT 保證金餘額（前端顯示用）
            "base": abs(self.qty),  # 持幣量 BTC（前端未實現損益估算用）
            "symbol": self.cfg.symbol,
            "strategy": self.cfg.strategy,
            "interval": self.cfg.interval,
            "last_price": price,
            "poll": self.cfg.poll_seconds,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "last_decision": last_decision,
            "mode": "futures",
        }
        tmp = STATE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, default=str)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass

    def _heartbeat(self, price: float | None = None) -> None:
        """同一根 K 線的輪詢週期：只更新 updated_at + 現價，維持前端綠燈。"""
        if not os.path.exists(STATE_PATH):
            return
        try:
            with open(STATE_PATH) as f:
                st = json.load(f)
            if not isinstance(st, dict):
                return
            st["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if price is not None:
                st["last_price"] = price
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f, default=str)
            os.replace(tmp, STATE_PATH)
        except Exception:
            pass


def _read_trades_json(path: str) -> bytes:
    """GET /trades?limit=N&mode=M → 從 PostgreSQL 或 SQLite 讀近期交易，回傳 JSON bytes。

    用 BOT_STRATEGY env var 過濾，確保每台 bot 只回傳自己的紀錄。
    """
    import urllib.parse
    from core.trade_journal import read_trades_db
    qs       = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    limit    = int(qs.get("limit", ["50"])[0])
    mode     = qs.get("mode", [None])[0]
    strategy = os.getenv("BOT_STRATEGY")   # 各 service 自己的策略名
    try:
        rows = read_trades_db(limit=limit, mode=mode, strategy=strategy)
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

        def log_message(self, *_):
            pass  # suppress per-request stdout noise

    server = socketserver.TCPServer(("0.0.0.0", port), _Handler)
    server.allow_reuse_address = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[狀態 API] 監聽 :{port}  GET /state → {STATE_PATH}")


def main():
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
    args = ap.parse_args()
    cfg.strategy, cfg.symbol, cfg.interval = args.strategy, args.symbol, args.interval
    cfg.futures_leverage, cfg.poll_seconds = args.leverage, args.poll
    cfg.strategy_params = {**cfg.strategy_params, **parse_bot_params(args.params)}

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
                                   ml_threshold=float(os.getenv("ML_THRESHOLD", "0.55")))

        budget_msg = f" | 預算上限 {args.budget:.0f}U/筆（max_pos={cfg.max_position_pct:.1%}）" if args.budget else ""
        print(f"[啟動] 合約測試網模擬盤（多/空）| {cfg.symbol} {cfg.interval} | "
              f"策略 {cfg.strategy} | 槓桿 {cfg.futures_leverage}x{budget_msg}")
        print(f"合約 USDT 餘額：{balance:.2f}")
        trader.restore()
    except Exception:                          # 交易初始化失敗：印出原因並保持存活（healthcheck 通過、便於診斷）
        print("[致命] 交易初始化失敗（保持存活供診斷）：\n" + traceback.format_exc())
        while True:
            time.sleep(30)

    last_bar = None
    while True:
        try:
            df = fetch_klines(client, cfg.symbol, cfg.interval, 3, futures=True)
            bar_time = df.index[-2]
            live_price = float(df["close"].iloc[-1])
            if bar_time == last_bar:
                trader._heartbeat(live_price)   # 刷新 updated_at，前端綠燈不熄
                time.sleep(cfg.poll_seconds)
                continue
            last_bar = bar_time
            trader.on_bar_close(bar_time)
            time.sleep(cfg.poll_seconds)
        except KeyboardInterrupt:
            print("\n[結束] 使用者中斷")
            break
        except Exception:
            print("[錯誤]", traceback.format_exc())
            time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
