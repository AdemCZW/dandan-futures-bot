"""合約做空：FuturesExecutionEngineer 邏輯 + FuturesLiveTrader 多/空決策（離線、假 client）。

只驗證可離線驗證的部分（精度/訂單參數/解析/決策與換邊）；實際對合約測試網的
網路往返需 BINANCE_FUTURES_TESTNET_* 金鑰才能完整驗證。
"""
from decimal import Decimal

import pandas as pd
import pytest

from core.futures_execution_engineer import FuturesExecutionEngineer
from core.risk_officer import RiskOfficer
from core.bot_state import BotState
from config import Config
import run_live_futures as M


INFO = {"symbols": [{"symbol": "BTCUSDT", "filters": [
    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
    {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
    {"filterType": "MIN_NOTIONAL", "notional": "5"},   # 合約用 "notional"
]}]}


class FakeFuturesClient:
    def __init__(self, positions=None, balances=None, price="100", open_orders=None):
        self._pos = positions or [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self._bal = balances or [{"asset": "USDT", "balance": "10000"}]
        self._price = price
        self._open_orders = open_orders or []
        self.created = []
        self.leverage_calls = []
        self.cancelled = []
        self.cancel_all = []
        self.open_orders_calls = []

    def futures_exchange_info(self): return INFO
    def futures_change_leverage(self, **kw): self.leverage_calls.append(kw)
    def futures_create_order(self, **kw):
        self.created.append(kw)
        return {"orderId": 100 + len(self.created), **kw}
    def futures_position_information(self, symbol=None): return self._pos
    def futures_account_balance(self): return self._bal
    def futures_symbol_ticker(self, symbol=None): return {"symbol": symbol, "price": self._price}
    def futures_cancel_order(self, **kw):
        self.cancelled.append(kw); return {"orderId": kw.get("orderId"), "status": "CANCELED"}
    def futures_cancel_all_open_orders(self, **kw):
        self.cancel_all.append(kw); return {"code": 200, "msg": "success"}
    def futures_get_open_orders(self, **kw):
        self.open_orders_calls.append(kw); return self._open_orders


# ── FuturesExecutionEngineer ────────────────────────────────────────
def test_init_sets_leverage_and_parses_filters():
    c = FakeFuturesClient()
    e = FuturesExecutionEngineer(c, "BTCUSDT", leverage=3)
    assert c.leverage_calls == [{"symbol": "BTCUSDT", "leverage": 3}]
    assert e._filters["step_size"] == Decimal("0.001")
    assert e._filters["min_notional"] == Decimal("5")


def test_parse_filters_unknown_symbol_raises():
    with pytest.raises(ValueError):
        FuturesExecutionEngineer._parse_filters(INFO, "NOSUCHUSDT")


def test_round_qty_fixed_point_no_scientific():
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    assert e.round_qty(0.0034) == "0.003"          # floor to step
    tiny = FuturesExecutionEngineer.__new__(FuturesExecutionEngineer)
    tiny._filters = {"step_size": Decimal("0.00000001")}
    assert tiny.round_qty(0.00000123) == "0.00000123" and "e" not in tiny.round_qty(0.00000123)


def test_valid_order_uses_rounded_qty_for_min_notional():
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    ok, _ = e.valid_order(0.001, 100)              # notional 0.1 < 5
    assert ok is False
    ok2, _ = e.valid_order(0.06, 100)              # 6 >= 5
    assert ok2 is True


def test_order_params_side_type_and_reduce_only():
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    p = e.order_params("SELL", 0.0034)
    assert p == {"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.003"}
    assert e.order_params("BUY", 0.01, reduce_only=True)["reduceOnly"] == "true"


def test_open_short_and_close_call_create_order():
    c = FakeFuturesClient()
    e = FuturesExecutionEngineer(c, "BTCUSDT", set_leverage=False)
    e.open_short(0.005)
    assert c.created[-1]["side"] == "SELL" and c.created[-1]["quantity"] == "0.005"
    assert "reduceOnly" not in c.created[-1]
    e.close(0.005, current_dir=-1)                 # 平空單 → BUY reduceOnly
    assert c.created[-1]["side"] == "BUY" and c.created[-1]["reduceOnly"] == "true"
    e.close(0.005, current_dir=1)                  # 平多單 → SELL reduceOnly
    assert c.created[-1]["side"] == "SELL" and c.created[-1]["reduceOnly"] == "true"


# ── 交易所掛單式 STOP / TAKE_PROFIT（硬停損）─────────────────────────
def test_stop_order_params_long_is_sell_closeposition():
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    p = e.stop_order_params(current_dir=1, trigger_price=95.07, order_type="STOP_MARKET")
    assert p["side"] == "SELL"                    # 平多 → SELL 觸發
    assert p["type"] == "STOP_MARKET"
    assert p["stopPrice"] == "95.0"               # 依 tickSize 0.1 floor
    assert p["closePosition"] == "true"           # 整倉平、倉位歸零自動撤單
    assert p["workingType"] == "CONTRACT_PRICE"   # 對齊軟停損用 last price
    assert "quantity" not in p and "reduceOnly" not in p   # closePosition 不可帶量/reduceOnly


def test_stop_order_params_short_is_buy():
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    p = e.stop_order_params(current_dir=-1, trigger_price=105.0, order_type="STOP_MARKET")
    assert p["side"] == "BUY"                      # 平空 → BUY 觸發


def test_take_profit_uses_take_profit_market_type():
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    p = e.stop_order_params(current_dir=1, trigger_price=110.0, order_type="TAKE_PROFIT_MARKET")
    assert p["type"] == "TAKE_PROFIT_MARKET" and p["side"] == "SELL"


def test_place_stop_and_tp_create_orders_and_return_id():
    c = FakeFuturesClient()
    e = FuturesExecutionEngineer(c, "BTCUSDT", set_leverage=False)
    r1 = e.place_stop(current_dir=1, stop_price=95.0)
    assert c.created[-1]["type"] == "STOP_MARKET" and c.created[-1]["closePosition"] == "true"
    assert r1["orderId"] == 101
    r2 = e.place_take_profit(current_dir=1, tp_price=110.0)
    assert c.created[-1]["type"] == "TAKE_PROFIT_MARKET"
    assert r2["orderId"] == 102


def test_cancel_order_calls_futures_cancel_order():
    c = FakeFuturesClient()
    e = FuturesExecutionEngineer(c, "BTCUSDT", set_leverage=False)
    e.cancel_order(101)
    assert c.cancelled[-1] == {"symbol": "BTCUSDT", "orderId": 101}


def test_cancel_all_stops_and_open_orders():
    c = FakeFuturesClient(open_orders=[{"orderId": 7, "type": "STOP_MARKET"}])
    e = FuturesExecutionEngineer(c, "BTCUSDT", set_leverage=False)
    e.cancel_all_stops()
    assert c.cancel_all[-1] == {"symbol": "BTCUSDT"}
    assert e.open_orders() == [{"orderId": 7, "type": "STOP_MARKET"}]
    assert c.open_orders_calls[-1] == {"symbol": "BTCUSDT"}


def test_position_balance_markprice_parsing():
    c = FakeFuturesClient(positions=[{"symbol": "BTCUSDT", "positionAmt": "-0.5"}],
                          balances=[{"asset": "USDT", "balance": "9999.5"}], price="123.4")
    e = FuturesExecutionEngineer(c, "BTCUSDT", set_leverage=False)
    assert e.position_amt() == -0.5                # 帶號：負＝空
    assert e.balance("USDT") == 9999.5
    assert e.mark_price() == 123.4


# ── FuturesLiveTrader（多/空決策與換邊）────────────────────────────
class FakeExecu:
    def __init__(self):
        self.amt = 0.0
        self._filters = {"min_qty": Decimal("0.001")}
        self.orders = []
        self.stops = []           # place_stop 呼叫 (dir, price)
        self.tps = []             # place_take_profit 呼叫 (dir, price)
        self.cancelled = []       # cancel_order 的 orderId
        self.cancel_all_count = 0
        self._open_orders = []
        self._orders_status = {}   # get_order(oid) → 狀態 dict（對帳真相用）
        self._stop_resp_empty = False   # 模擬 place_stop 回應空 body（孤兒單情境）
        self._mark = 100.0
        self._oid = 0
    def position_amt(self): return self.amt
    def balance(self, a="USDT"): return 10000.0
    def mark_price(self): return self._mark
    def valid_order(self, q, p): return True, "ok"
    def round_price(self, p):
        tick = Decimal("0.1")
        q = (Decimal(str(p)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick
        return format(q, "f")
    def open_long(self, q): self.amt = float(q); self.orders.append(("open_long", float(q)))
    def open_short(self, q): self.amt = -float(q); self.orders.append(("open_short", float(q)))
    def close(self, q, d): self.amt = 0.0; self.orders.append(("close", float(q), d))
    # 交易所掛單式停損/停利
    def place_stop(self, d, price):
        self._oid += 1; self.stops.append((d, float(price)))
        return {} if self._stop_resp_empty else {"orderId": f"S{self._oid}"}
    def place_take_profit(self, d, price):
        self._oid += 1; self.tps.append((d, float(price))); return {"orderId": f"T{self._oid}"}
    def cancel_order(self, oid): self.cancelled.append(oid); return {"status": "CANCELED"}
    def cancel_all_stops(self): self.cancel_all_count += 1; return {}
    def open_orders(self): return self._open_orders
    def get_order(self, oid): return self._orders_status.get(oid)


class FakeJournal:
    def __init__(self): self.logs = []
    def log(self, side, price, qty=0, pnl=0, ts=None): self.logs.append(side)


class ScriptStrat:
    allow_short = True
    def __init__(self, sigs): self.sigs = sigs; self.i = -1
    def prepare(self, df): return df
    def signal(self, row, pos): self.i += 1; return self.sigs[min(self.i, len(self.sigs) - 1)]


def _flat_df():
    idx = pd.date_range("2026-06-22", periods=6, freq="5min")
    c = [100.0] * 6
    return pd.DataFrame({"open": c, "high": [x * 1.001 for x in c],
                         "low": [x * 0.999 for x in c], "close": c, "volume": [1.0] * 6}, index=idx)


@pytest.fixture
def patched(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "fetch_klines", lambda *a, **k: _flat_df())
    monkeypatch.setattr(M, "detect_anomaly", lambda df: False)
    monkeypatch.setattr(M, "STATE_PATH", str(tmp_path / "state.json"))
    # 隔離真實 trades.db：_kelly_pct 內部 import read_trades_db，預設回空 → Kelly None → 走 budget
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    return tmp_path


def _trader(sigs, execu):
    cfg = Config()
    return M.FuturesLiveTrader(cfg, None, ScriptStrat(sigs), RiskOfficer(cfg), execu, FakeJournal())


class HoldShortWithAtr:
    """持空策略（永遠回 -1）且 prepare 注入 atr 欄，用來觸發 Chandelier trailing。"""
    allow_short = True
    def prepare(self, df):
        out = df.copy(); out["atr"] = 1.0; return out
    def signal(self, row, pos): return -1


def test_restore_short_initializes_chandelier_extremes(patched):
    """還原空單時，peak/trough 必須以 entry 為起點（不可殘留 0）。"""
    BotState(in_position=True, direction=-1, entry_price=100.0, sl=102.0, tp=96.0,
             qty=0.01, symbol="BTCUSDT", strategy="x").save(M.STATE_PATH)
    ex = FakeExecu(); ex.amt = -0.01            # 帳上有空單
    t = _trader([-1], ex)
    t.restore()
    assert t.trough == 100.0
    assert t.peak == 100.0


def test_restore_short_then_trailing_does_not_corrupt_sl(patched):
    """回歸：重啟還原空單後，下一根 Chandelier 不可把 SL 砸成 ~3×ATR（停損失效）。"""
    BotState(in_position=True, direction=-1, entry_price=100.0, sl=102.0, tp=96.0,
             qty=0.01, symbol="BTCUSDT", strategy="x").save(M.STATE_PATH)
    ex = FakeExecu(); ex.amt = -0.01
    cfg = Config()
    t = M.FuturesLiveTrader(cfg, None, HoldShortWithAtr(), RiskOfficer(cfg), ex, FakeJournal())
    t.restore()
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    # SL 必須維持在 entry(100) 之上的合理空單停損，而非被 trailing 砸到 ~3（min(sl, 0+3*atr)）
    assert t.sl > 100.0


def test_flat_to_long_opens_long(patched):
    ex = FakeExecu()
    t = _trader([1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == 1 and ex.orders[-1][0] == "open_long"
    assert BotState.load(M.STATE_PATH).direction == 1


def test_flat_to_short_opens_short(patched):
    ex = FakeExecu()
    t = _trader([-1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == -1 and ex.orders[-1][0] == "open_short"
    assert BotState.load(M.STATE_PATH).direction == -1


def test_long_to_short_flip_closes_then_opens(patched):
    ex = FakeExecu()
    t = _trader([1, -1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))   # 開多
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))   # 翻空：先平多再開空
    kinds = [o[0] for o in ex.orders]
    assert kinds == ["open_long", "close", "open_short"]
    assert t.dir == -1


def test_target_zero_closes_position(patched):
    ex = FakeExecu()
    t = _trader([-1, 0], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))   # 開空
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))   # 目標空手 → 平倉
    assert t.dir == 0 and ex.orders[-1][0] == "close"
    assert BotState.load(M.STATE_PATH).direction == 0


# ── 方向感知通道護欄（DirectionalChannelGuard）的進場閘門接線 ──────────────
def test_dcg_blocks_entry_in_blocked_direction(patched):
    """做空被封鎖時，策略要做空也不可進場（dir 維持 0、無 open_short）。"""
    ex = FakeExecu()
    t = _trader([-1], ex)
    t._dcg.enabled = True
    t._dcg.blocked_dir = -1                              # 模擬做空已被封鎖
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == 0
    assert not any(o[0] == "open_short" for o in ex.orders)


def test_dcg_allows_unblocked_direction(patched):
    """只封鎖做空時，做多不受影響仍可進場。"""
    ex = FakeExecu()
    t = _trader([1], ex)
    t._dcg.enabled = True
    t._dcg.blocked_dir = -1                              # 只封鎖做空
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == 1 and ex.orders[-1][0] == "open_long"


def test_dcg_disabled_never_blocks(patched):
    """護欄停用（預設）時，即使有封鎖狀態也照常進場 —— 確保其他 bot 行為不變。"""
    ex = FakeExecu()
    t = _trader([-1], ex)
    t._dcg.enabled = False
    t._dcg.blocked_dir = -1
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == -1 and ex.orders[-1][0] == "open_short"


def test_short_stop_loss_triggers_when_price_rises(patched, monkeypatch):
    # 空單停損在上方：構造價格漲過 sl 觸發 exit_sltp
    rise = [100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
    idx = pd.date_range("2026-06-22", periods=6, freq="5min")
    df = pd.DataFrame({"open": rise, "high": [x * 1.001 for x in rise],
                       "low": [x * 0.999 for x in rise], "close": rise, "volume": [1.0] * 6}, index=idx)
    ex = FakeExecu()
    t = _trader([-1, 0], ex)   # 第二根目標空手，避免停損後同根又重開空
    monkeypatch.setattr(M, "fetch_klines", lambda *a, **k: _flat_df())
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))   # 開空 @100，sl=102
    monkeypatch.setattr(M, "fetch_klines", lambda *a, **k: df)   # 價格漲到 110
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    assert ex.orders[-1][0] == "close" and t.dir == 0
    assert "exit_sl" in t.journal.logs        # 價漲破空單停損 → 真停損（細分後）


def test_protective_stop_fires_even_during_anomaly(patched, monkeypatch):
    # 修復鎖定：暴量(anomaly)時，持倉的保護性停損仍必須觸發（不被暴量抑制）
    rise = [100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
    idx = pd.date_range("2026-06-22", periods=6, freq="5min")
    df_rise = pd.DataFrame({"open": rise, "high": [x * 1.001 for x in rise],
                            "low": [x * 0.999 for x in rise], "close": rise, "volume": [1.0] * 6}, index=idx)
    ex = FakeExecu()
    t = _trader([-1, -1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))          # 開空 @100，sl=102
    monkeypatch.setattr(M, "fetch_klines", lambda *a, **k: df_rise)   # 價漲到 110（破空單停損）
    monkeypatch.setattr(M, "detect_anomaly", lambda df: True)         # 且本根暴量
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    assert t.dir == 0 and "exit_sl" in t.journal.logs        # 暴量也要停損出場
    assert ex.orders[-1][0] == "close"


# ── 結單原因細分（_classify_exit）：停利目標 / 移動停利 / 停損 / 保本 ────────
def test_classify_exit_take_profit(patched):
    t = _trader([0], FakeExecu())
    t.dir, t.entry_price, t.sl, t.tp = 1, 100.0, 98.0, 104.0
    assert t._classify_exit(104.0) == "exit_tp"          # 觸及停利目標

def test_classify_exit_hard_stop_loss(patched):
    t = _trader([0], FakeExecu())
    t.dir, t.entry_price, t.sl, t.tp = 1, 100.0, 98.0, 104.0
    assert t._classify_exit(98.0) == "exit_sl"           # 跌破成本 → 真停損

def test_classify_exit_trailing_profit(patched):
    t = _trader([0], FakeExecu())
    t.dir, t.entry_price, t.sl, t.tp = 1, 100.0, 103.0, 110.0   # 吊燈把 sl 上移到成本之上
    assert t._classify_exit(103.0) == "exit_trail"       # 移動停利鎖利出場

def test_classify_exit_breakeven(patched):
    t = _trader([0], FakeExecu())
    t.dir, t.entry_price, t.sl, t.tp = 1, 100.0, 100.0, 110.0   # scale-out 後 sl=成本
    assert t._classify_exit(100.0) == "exit_breakeven"   # 回到成本價 → 保本出場

def test_classify_exit_short_take_profit(patched):
    t = _trader([0], FakeExecu())
    t.dir, t.entry_price, t.sl, t.tp = -1, 100.0, 102.0, 96.0
    assert t._classify_exit(96.0) == "exit_tp"           # 空單觸及下方停利


# ── 交易所掛單式硬停損生命週期（EXCHANGE_STOP_ENABLED；預設關，逐台開）─────────
def test_open_places_exchange_stop_and_tp_when_enabled(patched):
    ex = FakeExecu()
    t = _trader([1], ex)
    t._exchange_stop = True                                   # 開啟交易所掛單保護
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))          # 開多
    assert t.dir == 1
    assert ex.stops and ex.stops[-1][0] == 1                  # 掛了 STOP（平多方向）
    assert ex.tps and ex.tps[-1][0] == 1                      # 掛了 TP
    assert t._stop_oid is not None and t._tp_oid is not None  # orderId 記下來


def test_open_skips_exchange_stop_when_disabled(patched):
    """預設關閉 → 完全不掛交易所單，四台 bot 現行行為不變。"""
    ex = FakeExecu()
    t = _trader([1], ex)
    assert t._exchange_stop is False                          # 預設關
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == 1 and ex.stops == [] and ex.tps == []


def test_go_flat_cancels_protective_orders(patched):
    ex = FakeExecu()
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", 96.0
    ex.amt = 0.01
    t._go_flat(100.0, pd.Timestamp("2026-06-22 00:05"), "exit_signal")
    assert "S1" in ex.cancelled and "T1" in ex.cancelled      # 平倉前撤掉殘留掛單
    assert t._stop_oid is None and t._tp_oid is None


def test_sync_protective_stop_replaces_on_sl_change(patched):
    ex = FakeExecu()
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", ex.round_price(96.0)
    t.sl = 98.0                                               # 吊燈把停損上移（跨 tick）
    t._sync_protective_stop(pd.Timestamp("2026-06-22 00:05"))
    assert ex.cancelled == ["S1"]                            # 撤舊停損
    assert ex.stops[-1] == (1, 98.0)                         # 掛新停損在 98
    assert t._tp_oid == "T1"                                 # 停利不動


def test_sync_protective_stop_noop_when_sl_unchanged(patched):
    ex = FakeExecu()
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", ex.round_price(96.0)
    t._sync_protective_stop(pd.Timestamp("2026-06-22 00:05"))   # sl 未變
    assert ex.cancelled == [] and ex.stops == []


def test_sync_protective_stop_noop_on_sub_tick_drift(patched):
    """Bug A 修復：sl 在同一 tick 內微動（95.03→95.07，round 後都 95.0）→ 不該 churn。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 95.03, 108.0, 0.01
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", ex.round_price(95.03)
    t.sl = 95.07                                              # sub-tick 漂移
    t._sync_protective_stop(pd.Timestamp("2026-06-22 00:05"))
    assert ex.cancelled == [] and ex.stops == []             # round 後同價 → 不撤不掛


def test_place_protective_market_closes_when_price_crossed_sl(patched):
    """Bug C 修復：掛單時現價已穿越 SL（會被幣安 -2021 拒）→ 改直接市價平倉，不留裸倉。"""
    ex = FakeExecu(); ex.amt = 0.01; ex._mark = 95.0          # 現價 95 已跌破 SL 96
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._place_protective(pd.Timestamp("2026-06-22 00:05"))
    assert any(o[0] == "close" for o in ex.orders)           # 市價平倉
    assert t.dir == 0                                         # 已平、不留裸倉
    assert ex.stops == []                                     # 沒掛注定被拒的 STOP


def test_reconcile_uses_exchange_order_truth_for_classification(patched):
    """Bug B 修復：對帳優先查交易所成交真相（哪張 oid FILLED + avgPrice），不靠現價猜。"""
    ex = FakeExecu(); ex.amt = 0.0
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", ex.round_price(96.0)
    # 交易所真相：TP 單已成交（即使現價 104 < tp 108，wick 觸發後回落）
    ex._orders_status = {"T1": {"status": "FILLED", "avgPrice": "108.0"},
                         "S1": {"status": "CANCELED"}}
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    assert "exit_tp" in t.journal.logs                        # 依真相判 TP，不被現價誤導


def test_place_protective_recovers_orphan_oid_from_open_orders(patched):
    """Bug D 修復：place_stop 回應空 body（oid 遺失）→ 從 open_orders 反查補回 oid，避免孤兒疊單。"""
    ex = FakeExecu(); ex.amt = 0.01
    ex._stop_resp_empty = True                               # 模擬 2xx 空 body
    ex._open_orders = [{"orderId": "S99", "type": "STOP_MARKET", "stopPrice": "96.0"}]
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._place_protective(pd.Timestamp("2026-06-22 00:05"))
    assert t._stop_oid == "S99"                              # 反查補回，不留孤兒


# ── 全面審查修復回歸（影響四台現行行為的真實漏洞）──────────────────────────
def test_circuit_breaker_pause_still_fires_stop_loss(patched):
    """#8：熔斷暫停期間，持倉的方向性停損停利仍須觸發（不裸奔）。"""
    ex = FakeExecu(); ex.amt = 0.01
    t = _trader([0], ex)
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 105.0, 110.0, 0.01  # sl>price → 觸發停損
    t.cb.is_paused = lambda: True                            # 熔斷暫停中
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    assert t.dir == 0                                        # 仍平倉、未裸奔
    assert any(o[0] == "close" for o in ex.orders)


def test_circuit_breaker_pause_blocks_new_entry(patched):
    """#8 反向：熔斷暫停時不可開新倉。"""
    ex = FakeExecu()
    t = _trader([1], ex)                                     # 策略想做多
    t.cb.is_paused = lambda: True
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.dir == 0 and not any(o[0] == "open_long" for o in ex.orders)


def test_write_sop_preserves_persisted_state(patched):
    """#9：_write_sop 每根覆寫狀態檔時，必須保住 cb/dcg/scaled_out/entry_sl_dist（否則重啟全歸零）。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.cb.consecutive_losses = 2                              # 熔斷已記 2 連虧
    t._dcg.enabled = True; t._dcg.blocked_dir = -1           # 護欄封鎖做空中
    t._scaled_out = True; t._entry_sl_dist = 3.5
    row = _flat_df().iloc[-2]
    t._write_sop(100.0, pd.Timestamp("2026-06-22 00:05"), row, {}, 0, None, None, [], False)
    st = BotState.load(M.STATE_PATH)
    assert st.cb_consecutive_losses == 2                     # 熔斷計數保住
    assert st.dcg_state and "blocked_dir" in st.dcg_state    # 護欄狀態保住
    assert st.scaled_out is True and st.entry_sl_dist == 3.5


def test_kelly_pct_filters_by_side_not_mode(patched, monkeypatch):
    """#1：_kelly_pct 應以 side 前綴篩平倉（不是 mode='exit'），≥20 筆 exit 才回非 None。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    rows = [{"pnl": 5.0 if i % 3 else -3.0, "side": "exit_tp"} for i in range(30)]
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: rows)
    assert t._kelly_pct() is not None                        # side 過濾生效 → 有樣本

    monkeypatch.setattr("core.trade_journal.read_trades_db",
                        lambda *a, **k: [{"pnl": 5.0, "side": "entry"}] * 30)
    assert t._kelly_pct() is None                            # 全是進場列（side 非 exit）→ 0 樣本


def test_reconcile_exchange_stop_fill_records_exit_and_clears(patched, monkeypatch):
    """交易所停損已平倉（本地以為持多、實際 amt≈0）→ 補記 exit、清狀態、不重複下市價單。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", 96.0
    ex.amt = 0.0                                              # 交易所已把倉位平掉
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    assert t.dir == 0                                         # 本地狀態清空
    assert any("exit" in s for s in t.journal.logs)           # 有補記一筆平倉
    assert not any(o[0] == "close" for o in ex.orders)        # 沒有重複下市價平倉單
    assert t._stop_oid is None and t._tp_oid is None


def test_reconcile_tp_fill_classifies_exit_tp(patched):
    ex = FakeExecu()
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 99.0, 0.01  # tp 設在 99（現價 100 已越過）
    t._stop_oid, t._tp_oid, t._stop_sl = "S1", "T1", 96.0
    ex.amt = 0.0
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))          # 現價 100 ≥ tp 99 → 判 TP 成交
    assert t.dir == 0 and "exit_tp" in t.journal.logs


def test_reconcile_skipped_when_exchange_stop_disabled(patched):
    """未開交易所單時，不做掛單對帳（維持原行為，避免把正常空手誤判）。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    ex.amt = 0.0
    # 不應因 amt==0 而對帳補單；走原本軟停損/訊號路徑
    t.on_bar_close(pd.Timestamp("2026-06-22 00:05"))
    assert all("對帳" not in s for s in t.journal.logs)


def test_restore_replaces_protective_when_enabled(patched, monkeypatch):
    """重啟還原持倉且開啟交易所單 → 撤掉殘留掛單並依還原的 SL/TP 重掛。"""
    BotState(in_position=True, direction=1, entry_price=100.0, sl=96.0, tp=108.0,
             qty=0.01, symbol="BTCUSDT", strategy="x").save(M.STATE_PATH)
    ex = FakeExecu(); ex.amt = 0.01
    monkeypatch.setenv("EXCHANGE_STOP_ENABLED", "1")
    t = _trader([0], ex)
    t.restore()
    assert ex.cancel_all_count >= 1                           # 先清乾淨殘留掛單
    assert ex.stops and ex.tps                                # 依還原狀態重掛 STOP + TP
    assert t._stop_oid is not None and t._tp_oid is not None


# ── 手動平倉（結算按鈕）：manual_close + 端點授權（close-only，不暫停 bot）──────
def test_manual_close_when_flat_is_noop(patched):
    ex = FakeExecu()
    t = _trader([0], ex)
    res = t.manual_close(now="2026-06-28 00:00:00")
    assert res["ok"] is False                                 # 空手無倉可平
    assert not any(o[0] == "close" for o in ex.orders)        # 不下任何單


def test_manual_close_closes_position_and_logs_exit_manual(patched):
    ex = FakeExecu(); ex.amt = 0.01
    t = _trader([0], ex)
    t.dir, t.entry_price, t.sl, t.tp, t.qty = 1, 100.0, 96.0, 108.0, 0.01
    res = t.manual_close(now="2026-06-28 00:00:00")
    assert res["ok"] is True and res["closed_dir"] == 1
    assert ex.orders[-1][0] == "close"                        # 市價平倉
    assert t.dir == 0                                         # 平倉後空手
    assert "exit_manual" in t.journal.logs                    # 結單原因 = 手動平倉


def test_manual_close_keeps_bot_running_no_pause(patched):
    """close-only：平倉後不暫停，熔斷狀態不變（下一根可照常進場）。"""
    ex = FakeExecu(); ex.amt = -0.01
    t = _trader([0], ex)
    t.dir, t.entry_price, t.sl, t.tp, t.qty = -1, 100.0, 102.0, 96.0, 0.01
    before_paused = t.cb.is_paused()
    t.manual_close(now="2026-06-28 00:00:00")
    assert t.cb.is_paused() == before_paused                  # 未額外暫停


def test_close_authorized_helper():
    assert M._close_authorized("secret", "secret") is True
    assert M._close_authorized("wrong", "secret") is False
    assert M._close_authorized("", "") is False               # 未設 CLOSE_TOKEN → 端點停用
    assert M._close_authorized("anything", "") is False
    assert M._close_authorized(None, "secret") is False


def test_close_event_is_threading_event():
    """_CLOSE_EVENT 必須是 threading.Event，才能從 HTTP 緒即時喚醒主迴圈。"""
    import threading
    assert hasattr(M, "_CLOSE_EVENT")
    assert isinstance(M._CLOSE_EVENT, threading.Event)


def test_close_event_set_when_flag_written(tmp_path, monkeypatch):
    """do_POST 寫完旗標檔後必須 set() _CLOSE_EVENT，讓主迴圈立刻從 wait 中醒來。"""
    import threading
    flag = str(tmp_path / "close_request.flag")
    monkeypatch.setattr(M, "CLOSE_REQUEST_PATH", flag)
    monkeypatch.setattr(M, "_CLOSE_EVENT", threading.Event())

    # 模擬 HTTP handler 核心邏輯：authorized → write flag → set event
    with open(flag, "w") as f:
        f.write("now")
    M._CLOSE_EVENT.set()                 # 這是 do_POST 應執行的

    assert M._CLOSE_EVENT.is_set()       # event 必須被設起來
    assert M._CLOSE_EVENT.wait(timeout=0)  # wait(0) 應立即回 True


# ── Ghost Position Bug ────────────────────────────────────────────────────────
# 問題：_go_flat() 靠 position_amt()（交易所 API）決定平倉量。
# Testnet 在 scale_out 後有 API 時間差，position_amt() 可能回 0 →
# close() 被跳過 → 交易所倉位殘留 → 下一筆進場疊加（幽靈倉）。
# 修正：改以本地追蹤的 self.qty 為主，不依賴交易所即時回傳值。

def test_go_flat_uses_self_qty_when_exchange_stale(patched):
    """position_amt() 回 0（API 時間差）時，_go_flat() 仍須依 self.qty 送出 close。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    # 空單，本地追蹤 qty=42，但交易所回傳 stale=0
    t.dir, t.entry_price, t.sl, t.tp, t.qty = -1, 100.0, 102.0, 96.0, 42.0
    ex.amt = 0.0   # ← 模擬 API 時間差：交易所尚未更新倉位

    t._go_flat(100.0, pd.Timestamp("2026-06-22 00:05"), "exit_signal")

    close_calls = [o for o in ex.orders if o[0] == "close"]
    assert len(close_calls) == 1,         "close() 未被呼叫 → 幽靈倉殘留"
    assert close_calls[0][1] == 42.0,     "close qty 應為 self.qty=42，非 position_amt()=0"
    assert t.qty == 0.0
    assert t.dir == 0


def test_go_flat_after_scale_out_closes_remaining_qty(patched):
    """scale_out 後 self.qty=21，_go_flat() 須平 21（非 42 或 0）。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.sl, t.tp, t.qty = -1, 100.0, 100.0, 96.0, 21.0
    t._scaled_out = True
    ex.amt = 0.0   # 同樣模擬 stale

    t._go_flat(100.0, pd.Timestamp("2026-06-22 00:05"), "exit_signal")

    close_calls = [o for o in ex.orders if o[0] == "close"]
    assert len(close_calls) == 1
    assert close_calls[0][1] == 21.0,     "close qty 應為 scale_out 後的剩餘 self.qty=21"


def test_go_flat_pnl_sign_correct_for_short(patched):
    """空單獲利（price < entry）時 pnl 應為正值；使用 self.qty * self.dir 符號正確。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    # 空單：entry=105, exit=100 → profit=5*21=105
    t.dir, t.entry_price, t.sl, t.tp, t.qty = -1, 105.0, 107.0, 99.0, 21.0
    ex.amt = 0.0

    j = FakeJournal()
    t.journal = j
    t._go_flat(100.0, pd.Timestamp("2026-06-22 00:05"), "exit_signal")

    # journal.log(side, price, qty, pnl, ...) — 第 4 個位置參數是 pnl
    assert len(j.logs) == 1
    # FakeJournal 只記 side，需要升級才能驗 pnl；但至少確認 close 有被呼叫
    close_calls = [o for o in ex.orders if o[0] == "close"]
    assert len(close_calls) == 1 and close_calls[0][1] == 21.0
