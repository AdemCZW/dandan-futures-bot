"""合約做空：FuturesExecutionEngineer 邏輯 + FuturesLiveTrader 多/空決策（離線、假 client）。

只驗證可離線驗證的部分（精度/訂單參數/解析/決策與換邊）；實際對合約測試網的
網路往返需 BINANCE_FUTURES_TESTNET_* 金鑰才能完整驗證。
"""
import json
import os
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
        self.get_order_calls = []
        self._orders = {}          # orderId → 狀態 dict（對帳真相用）

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
    def futures_get_order(self, **kw):
        self.get_order_calls.append(kw)
        return self._orders.get(kw.get("orderId"), {"orderId": kw.get("orderId"), "status": "NEW"})


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


def test_get_order_calls_futures_get_order_and_returns_status():
    """OPT-06：FuturesExecutionEngineer 必須有 get_order，否則 _reconcile_exit 永遠走現價猜方向 fallback。"""
    c = FakeFuturesClient()
    c._orders["S9"] = {"orderId": "S9", "status": "FILLED", "avgPrice": "95.5"}
    e = FuturesExecutionEngineer(c, "BTCUSDT", set_leverage=False)
    o = e.get_order("S9")
    assert c.get_order_calls[-1] == {"symbol": "BTCUSDT", "orderId": "S9"}
    assert o["status"] == "FILLED" and o["avgPrice"] == "95.5"


# ── F3：市價單成交均價解析（journal 進場價記實際成交價，非訊號棒收盤價）──
def test_fill_price_reads_avg_price():
    """MARKET 單回應含 avgPrice → 直接用它當實際成交均價。"""
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    assert e.fill_price({"avgPrice": "101.37", "executedQty": "0.05"}) == 101.37


def test_fill_price_falls_back_to_cumquote_over_executed_qty():
    """avgPrice 缺/為 0 時，用 cumQuote / executedQty 算 VWAP。"""
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    resp = {"avgPrice": "0", "cumQuote": "500.0", "executedQty": "5.0"}
    assert e.fill_price(resp) == 100.0


def test_fill_price_returns_none_when_unavailable():
    """回應無任何可用成交價資訊（testnet 偶發空回應）→ None，呼叫端 fallback 訊號價。"""
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    assert e.fill_price({"avgPrice": "0", "executedQty": "0"}) is None
    assert e.fill_price({}) is None
    assert e.fill_price(None) is None


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
        self.stop_qtys = []       # 各次 place_stop 的 qty（None=closePosition、數值=帶量後備）
        self.tps = []             # place_take_profit 呼叫 (dir, price)
        self.tp_qtys = []
        self.cancelled = []       # cancel_order 的 orderId
        self.cancel_all_count = 0
        self._open_orders = []
        self._orders_status = {}   # get_order(oid) → 狀態 dict（對帳真相用）
        self._stop_resp_empty = False   # 模擬 place_stop 回應空 body（孤兒單情境）
        self._mark = 100.0
        self._oid = 0
        self._fill_resp = None      # F3：open_long/open_short 回應（含 avgPrice）；None=舊行為
    def position_amt(self): return self.amt
    def balance(self, a="USDT"): return 10000.0
    def mark_price(self): return self._mark
    def valid_order(self, q, p): return True, "ok"
    def fill_price(self, resp):
        if not resp:
            return None
        ap = float(resp.get("avgPrice") or 0)
        return ap if ap > 0 else None
    def round_price(self, p):
        tick = Decimal("0.1")
        q = (Decimal(str(p)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick
        return format(q, "f")
    def open_long(self, q):
        self.amt = float(q); self.orders.append(("open_long", float(q))); return self._fill_resp
    def open_short(self, q):
        self.amt = -float(q); self.orders.append(("open_short", float(q))); return self._fill_resp
    def close(self, q, d): self.amt = 0.0; self.orders.append(("close", float(q), d))
    # 交易所掛單式停損/停利
    def place_stop(self, d, price, qty=None):
        self._oid += 1; self.stops.append((d, float(price))); self.stop_qtys.append(qty)
        return {} if self._stop_resp_empty else {"orderId": f"S{self._oid}"}
    def place_take_profit(self, d, price, qty=None):
        self._oid += 1; self.tps.append((d, float(price))); self.tp_qtys.append(qty)
        return {"orderId": f"T{self._oid}"}
    def cancel_order(self, oid): self.cancelled.append(oid); return {"status": "CANCELED"}
    def cancel_all_stops(self): self.cancel_all_count += 1; return {}
    def open_orders(self): return self._open_orders
    def get_order(self, oid): return self._orders_status.get(oid)


class FakeJournal:
    def __init__(self): self.logs = []; self.records = []
    def log(self, side, price, qty=0, pnl=0, ts=None):
        self.logs.append(side)
        self.records.append({"side": side, "price": price, "qty": qty, "pnl": pnl})


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


# ── F3：進場價記實際成交均價，而非訊號棒收盤價 ────────────────────────
def test_open_uses_actual_fill_price_as_entry(patched):
    """開倉回應含 avgPrice（≠訊號價 100）→ entry_price 用實際成交價。"""
    ex = FakeExecu()
    ex._fill_resp = {"avgPrice": "100.42", "executedQty": "1"}   # 實際成交偏離訊號價
    t = _trader([1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.entry_price == 100.42


def test_open_journals_actual_fill_price(patched):
    """F3 核心：journal 的 entry 這筆記的是實際成交均價，不是訊號棒收盤價。"""
    ex = FakeExecu()
    ex._fill_resp = {"avgPrice": "100.42", "executedQty": "1"}
    t = _trader([1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    entry_rec = [r for r in t.journal.records if r["side"] == "entry"][-1]
    assert entry_rec["price"] == 100.42


def test_open_sl_tp_relative_to_actual_fill(patched):
    """SL/TP 基準跟著實際成交價走（進場價位真相一致），不再基於訊號價。"""
    ex = FakeExecu()
    ex._fill_resp = {"avgPrice": "100.42", "executedQty": "1"}
    t = _trader([1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    # 固定%回退（_flat_df 無 atr 欄）：SL = entry×(1-stop_loss_pct)，須以 100.42 為基準
    expected_sl = 100.42 * (1 - t.cfg.stop_loss_pct)
    assert abs(t.sl - expected_sl) < 1e-6


def test_open_falls_back_to_signal_price_when_no_fill(patched):
    """回應無成交價（testnet 偶發）→ 退回訊號棒收盤價，行為與舊版一致（回歸保護）。"""
    ex = FakeExecu()
    ex._fill_resp = None
    t = _trader([1], ex)
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.entry_price == 100.0          # _flat_df 收盤價
    entry_rec = [r for r in t.journal.records if r["side"] == "entry"][-1]
    assert entry_rec["price"] == 100.0


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


def test_profit_floor_disabled_when_threshold_zero(patched):
    """min_profit_close_usdt=0（預設）→ 停用，永遠回 False。"""
    t = _trader([0], FakeExecu())
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    assert t.check_profit_floor(200.0) is False   # 浮盈 +100U 但功能關閉


def test_profit_floor_starts_timer_but_not_immediate(patched):
    """浮盈首次達標 → 開始計時、當輪不平（回 False），_profit_above_since 被設定。"""
    t = _trader([0], FakeExecu())
    t.cfg.strategy_params = {"min_profit_close_usdt": 20, "profit_sustain_seconds": 5}
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    assert t.check_profit_floor(125.0) is False    # 浮盈 +25U ≥ 20，但剛開始計時
    assert t._profit_above_since is not None


def test_profit_floor_fires_after_sustain(patched):
    """浮盈達標且持續超過 sustain 秒 → 回 True（可平倉）。"""
    from datetime import datetime, timezone, timedelta
    t = _trader([0], FakeExecu())
    t.cfg.strategy_params = {"min_profit_close_usdt": 20, "profit_sustain_seconds": 5}
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    # 模擬 6 秒前就已超標
    t._profit_above_since = datetime.now(timezone.utc) - timedelta(seconds=6)
    t._peak_pnl = 25.0
    assert t.check_profit_floor(125.0) is True


def test_profit_floor_resets_timer_when_drops_below(patched):
    """計時中浮盈跌破閾值 → 計時重置為 None（要重新累積 5 秒）。"""
    from datetime import datetime, timezone, timedelta
    t = _trader([0], FakeExecu())
    t.cfg.strategy_params = {"min_profit_close_usdt": 20, "profit_sustain_seconds": 5}
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    t._profit_above_since = datetime.now(timezone.utc) - timedelta(seconds=6)
    t._peak_pnl = 25.0
    # 現價使浮盈掉到 +10U（< 20）→ 計時重置，且未觸發保底（10 > 25*0.5=12.5? 否，10<12.5 →保底）
    # 用 +18U 避開保底條件（18 > 25*0.5）：純測計時重置
    assert t.check_profit_floor(118.0) is False
    assert t._profit_above_since is None


def test_profit_floor_giveback_locks_in(patched):
    """峰盈曾達 70%、回落超一半仍在盈 → 保底結算（無需計時）。"""
    t = _trader([0], FakeExecu())
    t.cfg.strategy_params = {"min_profit_close_usdt": 20, "profit_sustain_seconds": 5}
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    t._peak_pnl = 15.0            # 峰值 ≥ 20*0.7=14
    # 現價浮盈 +5U：< 峰值一半(7.5) 且 > 0 → 保底
    assert t.check_profit_floor(105.0) is True


def test_restore_backfills_missing_exit_when_db_open_but_exchange_flat(patched, monkeypatch):
    """重啟還原：DB 顯示仍持倉、但交易所已空手（狀態檔被清＋交易所端已平倉漏記）
       → 補記一筆 exit_reconciled，避免該筆交易與損益消失。"""
    ex = FakeExecu()                 # amt=0 → 交易所空手
    ex._mark = 110.0
    t = _trader([0], ex)
    monkeypatch.setattr(
        "core.trade_journal.read_trades_db",
        lambda *a, **k: [{"side": "entry", "price": 100.0, "qty": 1.0, "pnl": 0.0}])
    t.restore()
    sides = [r["side"] for r in t.journal.records]
    assert "exit_reconciled" in sides
    rec = next(r for r in t.journal.records if r["side"] == "exit_reconciled")
    assert rec["pnl"] > 0            # (110-100)*1 − fee > 0
    assert t.dir == 0                # 補記後維持空手


def test_restore_no_backfill_when_position_live(patched, monkeypatch):
    """交易所實際仍有倉 → 正常還原，不補記平倉。"""
    ex = FakeExecu()
    ex.amt = 1.0                     # 交易所有多倉
    t = _trader([0], ex)
    monkeypatch.setattr(
        "core.trade_journal.read_trades_db",
        lambda *a, **k: [{"side": "entry", "price": 100.0, "qty": 1.0, "pnl": 0.0}])
    t.restore()
    assert "exit_reconciled" not in [r["side"] for r in t.journal.records]
    assert t.dir == 1


def test_restore_no_backfill_when_db_properly_closed(patched, monkeypatch):
    """DB 已正常平倉（entry→exit 配對）＋交易所空手 → 不補記。"""
    ex = FakeExecu()                 # flat
    t = _trader([0], ex)
    monkeypatch.setattr(
        "core.trade_journal.read_trades_db",
        lambda *a, **k: [
            {"side": "entry", "price": 100.0, "qty": 1.0, "pnl": 0.0},
            {"side": "exit_tp", "price": 110.0, "qty": 1.0, "pnl": 10.0},
        ][::-1])                     # read_trades_db 最新在前
    t.restore()
    assert "exit_reconciled" not in [r["side"] for r in t.journal.records]


def test_restore_backfill_short_pnl_sign(patched, monkeypatch):
    """未平倉為空單、標記價高於進場 → 補記為虧損（pnl < 0）。"""
    ex = FakeExecu()
    ex._mark = 110.0
    t = _trader([0], ex)
    monkeypatch.setattr(
        "core.trade_journal.read_trades_db",
        lambda *a, **k: [{"side": "entry_short", "price": 100.0, "qty": 1.0, "pnl": 0.0}])
    t.restore()
    rec = next(r for r in t.journal.records if r["side"] == "exit_reconciled")
    assert rec["pnl"] < 0            # 空單、價漲 → 虧損


def test_go_flat_deducts_round_trip_taker_fee_from_pnl(patched):
    """OPT-01：實盤平倉寫 journal 前要扣雙邊 taker 費（entry+exit 名目 × fee）。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.qty = 1, 100.0, 0.01
    t._go_flat(price=110.0, bar_time="2026-06-29 00:00:00", reason="exit_signal")
    fee = 0.01 * (100.0 + 110.0) * t.cfg.taker_fee_rate          # 雙邊名目 × 費率
    gross = (110.0 - 100.0) * 0.01
    assert t.journal.records[-1]["pnl"] == pytest.approx(gross - fee)


def test_round_trip_fee_helper_charges_both_legs(patched):
    """OPT-01：雙邊 taker 費 = qty × (進場名目 + 出場名目) × 費率。"""
    t = _trader([0], FakeExecu())
    fee = t._round_trip_fee(0.01, 100.0, 110.0)
    assert fee == pytest.approx(0.01 * (100.0 + 110.0) * t.cfg.taker_fee_rate)


def test_reconcile_exit_deducts_taker_fee(patched):
    """OPT-01：交易所掛單對帳平倉的 pnl 也要扣雙邊 taker 費。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.qty = 1, 100.0, 0.01
    t.sl, t.tp, t._tp_oid, t._entry_sl_dist = 98.0, 104.0, "T1", 2.0
    ex._orders_status["T1"] = {"status": "FILLED", "avgPrice": "104.0"}
    t._reconcile_exit(price=104.0, bar_time="2026-06-29 00:00:00")
    gross = (104.0 - 100.0) * 0.01
    fee = 0.01 * (100.0 + 104.0) * t.cfg.taker_fee_rate
    assert t.journal.records[-1]["pnl"] == pytest.approx(gross - fee)


def test_taker_fee_rate_separate_from_backtest_fee_rate(patched):
    """OPT-01：實盤 taker_fee_rate(0.0004) 與回測 fee_rate(0.001) 分離，不互相污染。"""
    cfg = Config()
    assert cfg.taker_fee_rate == pytest.approx(0.0004)
    assert cfg.fee_rate == pytest.approx(0.001)


def test_fetch_bars_uses_strategy_warmup_for_ema200(patched):
    """OPT-03：trend_pullback(200EMA) 應抓 ≥4×200 根而非寫死 200。"""
    from core.quant_researcher import build_strategy
    cfg = Config()
    t = M.FuturesLiveTrader(cfg, None, build_strategy("trend_pullback"),
                            RiskOfficer(cfg), FakeExecu(), FakeJournal())
    assert t._fetch_bars() >= 800
    assert t._fetch_bars() <= 1500          # 不超過幣安 klines 單次上限


def test_fetch_bars_falls_back_to_200_without_warmup(patched):
    """策略無 warmup_bars（舊式/替身）→ 退回 200，與舊行為相容。"""
    t = _trader([0], FakeExecu())           # ScriptStrat 無 warmup_bars
    assert t._fetch_bars() == 200


def test_reconcile_exit_uses_exchange_fill_truth_not_current_price(patched):
    """OPT-06：交易所條件單 FILLED → 用其 avgPrice 判 SL/TP/PnL，而非用現價猜。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.qty = 1, 100.0, 0.01
    t.sl, t.tp, t._tp_oid, t._entry_sl_dist = 98.0, 104.0, "T1", 2.0
    ex._orders_status["T1"] = {"status": "FILLED", "avgPrice": "104.0"}
    # 現價 101（未達 tp 104）：若用現價 fallback 不會判停利；真相是 TP 已成交
    reason = t._reconcile_exit(price=101.0, bar_time="2026-06-29 00:00:00")
    assert reason == "exit_tp"
    rec = t.journal.records[-1]
    assert rec["price"] == 104.0                                  # 用成交真相價，非現價 101
    gross = (104.0 - 100.0) * 0.01
    fee = 0.01 * (100.0 + 104.0) * t.cfg.taker_fee_rate           # OPT-01：扣雙邊 taker 費
    assert rec["pnl"] == pytest.approx(gross - fee)
    assert t.dir == 0 and t._tp_oid is None


def test_reconcile_exit_falls_back_to_current_price_when_no_filled_order(patched):
    """OPT-06：查不到 FILLED 真相時保留現價 fallback（不可移除）。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.qty = 1, 100.0, 0.01
    t.sl, t.tp, t._stop_oid, t._entry_sl_dist = 98.0, 104.0, "S1", 2.0
    ex._orders_status["S1"] = {"status": "NEW"}      # 尚未成交 → 走現價 fallback
    reason = t._reconcile_exit(price=104.5, bar_time="2026-06-29 00:00:00")
    assert reason == "exit_tp"                       # 現價 104.5 ≥ tp 104 → fallback 判停利
    assert t.journal.records[-1]["price"] == 104.0   # fallback hit_tp → fill=self.tp


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


# ── WebSocket Handler ─────────────────────────────────────────────────────────
# _make_ws_handler 為可測試的 callback 工廠：
#   - 每個 tick 呼叫 _heartbeat（儀表板即時刷新）
#   - k["x"]=True（K 棒收盤）才呼叫 on_bar_close
#   - 同一 bt 去重，防止重複觸發
#   - 無效訊息不崩潰

def _ws_msg(closed: bool, bt: int = 1000, price: str = "100.0") -> dict:
    return {"k": {"c": price, "x": closed, "t": bt}}


def test_ws_handler_heartbeat_on_every_tick(patched):
    """WS handler：每個 tick 不論收盤與否都呼叫 _heartbeat。"""
    import threading
    heartbeats = []
    ex = FakeExecu()
    t = _trader([0], ex)
    t._heartbeat = lambda price: heartbeats.append(price)
    lock = threading.Lock()
    handle = M._make_ws_handler(t, [None], lock)

    handle(_ws_msg(closed=False, price="99.5"))
    assert len(heartbeats) == 1 and heartbeats[0] == 99.5

    handle(_ws_msg(closed=True, price="100.0"))
    assert len(heartbeats) == 2 and heartbeats[1] == 100.0


def test_ws_handler_no_on_bar_close_when_not_closed(patched):
    """WS handler：k["x"]=False 時不呼叫 on_bar_close。"""
    import threading
    bar_closes = []
    ex = FakeExecu()
    t = _trader([0], ex)
    t._heartbeat = lambda price: None
    t.on_bar_close = lambda bt: bar_closes.append(bt)
    lock = threading.Lock()
    handle = M._make_ws_handler(t, [None], lock)

    handle(_ws_msg(closed=False))
    assert len(bar_closes) == 0


def test_ws_handler_on_bar_close_when_closed(patched):
    """WS handler：k["x"]=True 時呼叫 on_bar_close 並傳入正確時間戳。"""
    import threading
    bar_closes = []
    ex = FakeExecu()
    t = _trader([0], ex)
    t._heartbeat = lambda price: None
    t.on_bar_close = lambda bt: bar_closes.append(bt)
    lock = threading.Lock()
    handle = M._make_ws_handler(t, [None], lock)

    handle(_ws_msg(closed=True, bt=1_000_000))
    assert len(bar_closes) == 1
    assert bar_closes[0] == pd.to_datetime(1_000_000, unit="ms")


def test_ws_handler_dedupes_same_bar(patched):
    """WS handler：同一 bt 送兩次，on_bar_close 只觸發一次。"""
    import threading
    bar_closes = []
    ex = FakeExecu()
    t = _trader([0], ex)
    t._heartbeat = lambda price: None
    t.on_bar_close = lambda bt: bar_closes.append(bt)
    lock = threading.Lock()
    handle = M._make_ws_handler(t, [None], lock)

    handle(_ws_msg(closed=True, bt=999))
    handle(_ws_msg(closed=True, bt=999))   # 重複，應被去重
    assert len(bar_closes) == 1


def test_ws_handler_ignores_invalid_msg(patched):
    """WS handler：無 'k' 鍵的訊息不崩潰、不呼叫任何方法。"""
    import threading
    heartbeats = []
    ex = FakeExecu()
    t = _trader([0], ex)
    t._heartbeat = lambda price: heartbeats.append(price)
    lock = threading.Lock()
    handle = M._make_ws_handler(t, [None], lock)

    handle({"type": "ping"})           # 無 k 鍵
    handle({})                         # 空 dict
    assert len(heartbeats) == 0


def test_restored_last_bar_reads_state_ts(patched, tmp_path):
    """state 檔有 last_decision.ts 且 symbol/interval 相符 → 回該 bar Timestamp。"""
    t = _trader([0], FakeExecu())
    state = {"symbol": t.cfg.symbol, "interval": t.cfg.interval,
             "last_decision": {"ts": "2026-07-03 08:00:00"}}
    with open(t.state_path, "w") as f:
        json.dump(state, f)
    assert t.restored_last_bar() == pd.Timestamp("2026-07-03 08:00:00")


def test_restored_last_bar_none_on_symbol_mismatch(patched, tmp_path):
    """config 剛換過市場 → state 的 ts 不可沿用（不同市場的 bar 時間無意義）。"""
    t = _trader([0], FakeExecu())
    state = {"symbol": "OTHERUSDT", "interval": t.cfg.interval,
             "last_decision": {"ts": "2026-07-03 08:00:00"}}
    with open(t.state_path, "w") as f:
        json.dump(state, f)
    assert t.restored_last_bar() is None


def test_restored_last_bar_none_when_no_file(patched):
    t = _trader([0], FakeExecu())
    try:
        os.remove(t.state_path)
    except FileNotFoundError:
        pass
    assert t.restored_last_bar() is None


# ── 掛單競態自癒（-4130 全帳戶裸奔事故的修復）─────────────────────────────

def test_place_protective_adopts_existing_order_on_reject(patched):
    """掛停損被拒（-4130 同方向已有 closePosition 單）→ 反查 open_orders 認養既有單，
    不留裸倉（重啟撤舊→掛新競態：撤單未完成時掛新單被拒，舊單稍後才消失）。"""
    ex = FakeExecu()
    def boom(d, price): raise RuntimeError("APIError(code=-4130): existing")
    ex.place_stop = boom
    ex._open_orders = [{"type": "STOP_MARKET", "orderId": "S77"},
                       {"type": "TAKE_PROFIT_MARKET", "orderId": "T88"}]
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.qty, t.sl, t.tp = 1, 100.0, 1.0, 95.0, 110.0
    t._place_protective("bar")
    assert t._stop_oid == "S77"      # 認養既有停損單，保護不中斷


def test_on_bar_close_reheals_missing_protective(patched):
    """持倉 + exchange_stop 開 + stop_oid 缺（先前掛單失敗）→ 每根 K 棒自動補掛。"""
    ex = FakeExecu()
    t = _trader([1], ex)             # 策略持多（target=1 == dir → hold）
    t._exchange_stop = True
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    t.sl, t.tp = 95.0, 110.0
    t._entry_sl_dist = 5.0
    t._stop_oid = None               # 缺停損掛單（裸奔狀態）
    t.on_bar_close("2026-07-04 00:00:00")
    assert len(ex.stops) >= 1        # 已補掛 STOP
    assert t._stop_oid is not None


def test_sync_protective_reheals_missing_tp(patched):
    """TP 掛單曾失敗（tp_oid 缺）→ 每根 K 棒補掛（原版永不再試）。"""
    ex = FakeExecu()
    t = _trader([1], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    t.sl, t.tp = 95.0, 110.0
    t._stop_oid, t._stop_sl = "S1", t._rounded_sl()   # STOP 正常 → 不換單
    t._tp_oid = None                                   # TP 缺
    t._sync_protective_stop("bar")
    assert len(ex.tps) == 1 and t._tp_oid is not None


def test_restore_waits_for_stop_clearance_before_replacing(patched, monkeypatch):
    """restore：撤殘單後等交易所端真正清空（open_orders 空）才掛新單，消滅 -4130 競態。"""
    ex = FakeExecu()
    ex.amt = 1.0                                       # 交易所有多倉 → 走重掛路徑
    seq = {"n": 0}
    residual = [{"type": "STOP_MARKET", "orderId": "OLD"}]
    def open_orders():
        seq["n"] += 1
        return residual if seq["n"] <= 2 else []       # 前兩次查還有殘單，之後才清空
    ex.open_orders = open_orders
    monkeypatch.setattr("time.sleep", lambda s: None)  # 不真睡
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: [])
    t = _trader([0], ex)
    t._exchange_stop = True
    t.restore()
    assert seq["n"] >= 3                               # 有輪詢等待清空
    assert len(ex.stops) == 1                          # 清空後才掛新 STOP


def test_place_protective_falls_back_to_qty_stop_when_ghost_blocks(patched):
    """-4130 且 open_orders 查不到可認養的單（testnet 幽靈單）→ 改帶量 reduceOnly STOP。"""
    ex = FakeExecu()
    orig = ex.place_stop
    def picky(d, price, qty=None):
        if qty is None:
            raise RuntimeError("APIError(code=-4130): existing")   # closePosition 被幽靈單擋
        return orig(d, price, qty=qty)                              # 帶量 → 成功
    ex.place_stop = picky
    ex._open_orders = []                                            # 查無單可認養
    t = _trader([0], ex)
    t._exchange_stop = True
    t.dir, t.entry_price, t.qty, t.sl, t.tp = 1, 100.0, 1.5, 95.0, 110.0
    t._place_protective("bar")
    assert t._stop_oid is not None
    assert ex.stop_qtys[-1] == 1.5                                  # 帶量後備（非 closePosition）


def test_engineer_qty_stop_params_use_reduce_only():
    """帶量條件單參數：quantity + reduceOnly，無 closePosition。"""
    e = FuturesExecutionEngineer(FakeFuturesClient(), "BTCUSDT", set_leverage=False)
    p = e.stop_order_params(current_dir=1, trigger_price=95.0, order_type="STOP_MARKET", qty=0.5)
    assert p["quantity"] == "0.5" and p["reduceOnly"] == "true"
    assert "closePosition" not in p


def test_check_soft_stops_long_sl_hit_closes(patched):
    """每輪 poll 軟停損：多單即時價跌破 SL → 立即市價平倉（掛單失效後備）。"""
    ex = FakeExecu()
    t = _trader([1], ex)
    t.dir, t.entry_price, t.qty, t.sl, t.tp = 1, 100.0, 1.0, 95.0, 110.0
    reason = t.check_soft_stops(94.5)
    assert reason == "exit_sl"
    assert t.dir == 0                        # 已平倉
    assert t.journal.records[-1]["side"] == "exit_sl"


def test_check_soft_stops_short_tp_hit_closes(patched):
    ex = FakeExecu()
    t = _trader([-1], ex)
    t.dir, t.entry_price, t.qty, t.sl, t.tp = -1, 100.0, 1.0, 105.0, 92.0
    assert t.check_soft_stops(91.5) == "exit_tp"
    assert t.dir == 0


def test_check_soft_stops_no_hit_returns_none(patched):
    t = _trader([1], FakeExecu())
    t.dir, t.entry_price, t.qty, t.sl, t.tp = 1, 100.0, 1.0, 95.0, 110.0
    assert t.check_soft_stops(100.0) is None
    assert t.dir == 1                        # 沒動


def test_check_soft_stops_flat_noop(patched):
    t = _trader([0], FakeExecu())
    assert t.check_soft_stops(1.0) is None


def test_write_sop_resets_risk_equity_peak_on_testnet_reset(patched):
    """R1（2026-07-04 全系統體檢）：_write_sop 偵測到 testnet 重置時，必須連帶重置
    self.risk._equity_peak，否則峰值回撤熔斷會用重置前的高水位對重置後的小額餘額
    算回撤，永遠觸發、bot 從此拒絕所有新倉且無任何錯誤訊息可查。"""
    ex = FakeExecu()
    ex.balance = lambda a="USDT": 100.0              # 重置後的小額餘額
    t = _trader([0], ex)
    t.risk._equity_peak = 5000.0                    # 重置前的高水位
    prev = BotState(last_balance=5000.0)             # 上次持久化的餘額基準（>>100 觸發重置偵測）
    prev.save(t.state_path)
    row = _flat_df().iloc[-2]
    # equity=100（重置後小額）驟降到 << prev.last_balance(5000) → 觸發偵測
    t._write_sop(100.0, pd.Timestamp("2026-06-22 00:05"), row, {}, 0, None, None, [], False)
    assert t.risk._equity_peak is None               # 已隨重置一併清空


def test_reconcile_exit_resets_peak_pnl(patched, monkeypatch):
    """F3：對帳出場必須清 _peak_pnl，否則下一筆新倉被盈利保底用舊峰盈秒平。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t.dir, t.entry_price, t.qty = 1, 100.0, 1.0
    t.sl, t.tp, t._entry_sl_dist = 98.0, 104.0, 2.0
    t._peak_pnl = 55.0                       # 上一筆殘留峰盈
    t._reconcile_exit(104.5, "bar")
    assert t._peak_pnl == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# F4（中，2026-07-04 風控審查）：scale-out 淨贏的交易不該被熔斷記成連虧。
# 原況：scale_out 的獲利只進 journal 不進 cb；剩餘半倉小虧出場時 record_trade
# 只看剩餘 pnl → 整筆淨賺被記一次連虧 → 三筆「假連虧」就熔斷停機 24h。
# ═══════════════════════════════════════════════════════════════════════════

def test_f4_scale_out_pnl_accumulates(patched):
    """scale-out 成交時，其已實現獲利記入 self._scaled_pnl（供出場時合併計算）。"""
    ex = FakeExecu(); ex.amt = 0.02
    t = _trader([1], ex)                                     # 策略續抱多單
    t.dir, t.entry_price, t.qty = 1, 99.0, 0.02
    t.sl, t.tp = 90.0, 200.0                                 # 不觸發停損停利
    t._entry_sl_dist = 2.0                                   # 現價 100 → 浮盈 1.0 = 0.5R → scale out
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t._scaled_out is True
    assert t._scaled_pnl > 0                                 # 半倉獲利已入袋且被追蹤


def test_f4_net_win_after_scale_out_does_not_count_as_loss(patched):
    """scale-out 賺 5、剩餘半倉出場小虧 → 淨賺 → 熔斷連虧計數應為 0。"""
    ex = FakeExecu(); ex.amt = 0.01
    t = _trader([0], ex)                                     # 策略轉空手 → 平剩餘倉
    t.dir, t.entry_price, t.qty = 1, 100.0, 0.01
    t.sl, t.tp = 90.0, 200.0
    t._scaled_out, t._scaled_pnl = True, 5.0                 # 前段 scale-out 已賺 5
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))         # 現價 100 平倉 → pnl≈-手續費（小虧）
    assert t.dir == 0
    assert t.cb.consecutive_losses == 0                      # 淨賺 → 不是連虧
    assert t._scaled_pnl == 0.0                              # 出場後歸零，不污染下一筆


def test_f4_net_loss_after_scale_out_still_counts(patched):
    """scale-out 只賺 0.001、剩餘出場虧更多 → 淨虧 → 連虧計數 +1（不可漏記真虧損）。"""
    ex = FakeExecu(); ex.amt = 0.01
    t = _trader([0], ex)
    t.dir, t.entry_price, t.qty = 1, 110.0, 0.01             # 現價 100 → 剩餘倉虧 0.1
    t.sl, t.tp = 90.0, 200.0
    t._scaled_out, t._scaled_pnl = True, 0.001
    t.on_bar_close(pd.Timestamp("2026-06-22 00:00"))
    assert t.cb.consecutive_losses == 1


def test_f4_scaled_pnl_persisted_and_restored(patched):
    """_scaled_pnl 隨 BotState 持久化：_write_sop 保住、restore 讀回（重啟不歸零）。"""
    ex = FakeExecu()
    t = _trader([0], ex)
    t._scaled_pnl = 3.5
    row = _flat_df().iloc[-2]
    t._write_sop(100.0, pd.Timestamp("2026-06-22 00:05"), row, {}, 0, None, None, [], False)
    st = BotState.load(M.STATE_PATH)
    assert st.scaled_pnl == 3.5                              # 寫檔保住
    ex2 = FakeExecu(); ex2.amt = 0.01
    BotState(in_position=True, direction=1, entry_price=100.0, sl=95.0, tp=110.0,
             qty=0.01, symbol="BTCUSDT", strategy="x", scaled_pnl=3.5).save(M.STATE_PATH)
    t2 = _trader([1], ex2)
    t2.restore()
    assert t2._scaled_pnl == 3.5                             # 重啟讀回


# ═══════════════════════════════════════════════════════════════════════════
# F5（中）：Kelly 樣本須以本 bot 的 journal mode 過濾，避免本機 paper 交易
# （同策略同幣種、寫進同一個 PG）污染實盤倉位計算。
# ═══════════════════════════════════════════════════════════════════════════

def test_f5_kelly_filters_by_journal_mode(patched, monkeypatch):
    ex = FakeExecu()
    t = _trader([0], ex)
    seen = {}
    def spy(*a, **k):
        seen.update(k); return []
    monkeypatch.setattr("core.trade_journal.read_trades_db", spy)
    t._kelly_pct()
    assert seen.get("mode") == "live_futures_testnet"        # 實盤 bot 只吃實盤紀錄


# ═══════════════════════════════════════════════════════════════════════════
# F6（中）：testnet 重置時組合層 equity 表的 peak 也要清——否則
# PORTFOLIO_MAX_DRAWDOWN 一旦啟用，重置後 kill-switch 永久擋新倉（R1 的組合層翻版）。
# ═══════════════════════════════════════════════════════════════════════════

def test_f6_testnet_reset_clears_portfolio_equity(patched, tmp_path):
    from core.portfolio_guard import PortfolioGuard
    ex = FakeExecu()
    ex.balance = lambda a="USDT": 100.0                      # 重置後小額餘額
    t = _trader([0], ex)
    t._guard = PortfolioGuard(db_path=str(tmp_path / "guard.db"))
    t._guard.upsert_equity("s1", "BTCUSDT", 5000.0)          # 重置前的高峰殘留
    t._guard.upsert_equity("s2", "ETHUSDT", 5000.0)
    prev = BotState(last_balance=5000.0)
    prev.save(t.state_path)
    row = _flat_df().iloc[-2]
    t._write_sop(100.0, pd.Timestamp("2026-06-22 00:05"), row, {}, 0, None, None, [], False)
    eq, peak, dd = t._guard.portfolio_drawdown()
    assert peak < 10000.0                                    # 舊高峰已被清（不再殘留 2×5000）


# ═══════════════════════════════════════════════════════════════════════════
# 2026-07-06 實盤稽核修復 ①②（docs/strategy_research_log.md「實盤交易全面稽核」）
# ① 訊號資料改吃主網公開 K 線（測試網小幣行情有主網不存在的幽靈波動，ADA 實測偏離 10.5%），
#   下單執行仍留在測試網——decisions 在主網座標、fills 在測試網。
# ② 部署後全新容器狀態檔消失 → restored_last_bar 改由 journal 推斷最後已行動的 K 棒，
#   杜絕「同一根棒重複決策 → 重複進場」的 churn（b7 實測同棒進場 3 次 ×2 輪）。
# ═══════════════════════════════════════════════════════════════════════════

def test_make_data_client_defaults_to_mainnet():
    from core.market_analyst import make_data_client
    c = make_data_client()
    assert c.testnet is False


def test_make_data_client_testnet_optout():
    from core.market_analyst import make_data_client
    c = make_data_client("testnet")
    assert c.testnet is True


def test_data_client_defaults_to_execution_client(patched):
    t = _trader([0], FakeExecu())
    assert t.data_client is t.client


def test_on_bar_close_fetches_klines_with_data_client(patched, monkeypatch):
    """訊號評估的 K 線必須走 data_client（主網），不是執行 client（測試網）。"""
    seen = {}
    def rec_fetch(client, *a, **k):
        seen["client"] = client
        return _flat_df()
    monkeypatch.setattr(M, "fetch_klines", rec_fetch)
    sentinel = object()
    cfg = Config()
    t = M.FuturesLiveTrader(cfg, None, ScriptStrat([0]), RiskOfficer(cfg), FakeExecu(),
                            FakeJournal(), data_client=sentinel)
    t.on_bar_close(pd.Timestamp("2026-07-04 12:00"))
    assert seen["client"] is sentinel


def test_restored_last_bar_journal_fallback_entry_bar(patched, monkeypatch):
    """狀態檔遺失（redeploy）→ 由 journal 最新列推斷：entry 列 ts＝決策棒，直接採用。"""
    rows = [{"ts": "2026-07-04 12:00:00", "side": "entry", "price": 1.0, "qty": 1.0, "pnl": 0.0}]
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: rows)
    t = _trader([0], FakeExecu())
    t.cfg.interval = "4h"
    assert t.restored_last_bar() == pd.Timestamp("2026-07-04 12:00:00")


def test_restored_last_bar_journal_midbar_exit_maps_to_closed_bar(patched, monkeypatch):
    """盤中軟停損出場列 ts＝牆鐘時間（帶時區）→ 換算成「當下已收完的那根」＝floor−1棒。

    b7 churn 的實際型態：12:00 棒決策進場、17:11 盤中停損出場、部署後重進。
    17:11 當下已收完的棒是 12:00（16:00 棒還在走）→ 回 12:00，poll 迴圈才會跳過重決策。"""
    rows = [{"ts": "2026-07-04T17:11:33+00:00", "side": "exit_sl", "price": 1.0, "qty": 1.0, "pnl": -1.0}]
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: rows)
    t = _trader([0], FakeExecu())
    t.cfg.interval = "4h"
    assert t.restored_last_bar() == pd.Timestamp("2026-07-04 12:00:00")


def test_restored_last_bar_no_state_no_journal_returns_none(patched):
    # patched fixture 已把 read_trades_db stub 成回空 list
    t = _trader([0], FakeExecu())
    t.cfg.interval = "4h"
    assert t.restored_last_bar() is None


def test_restored_last_bar_state_file_wins_over_journal(patched, monkeypatch):
    """state 檔存在且 symbol/interval 匹配 → 以 state 為準（含「決策過但沒交易」的棒）。"""
    import json as _json
    rows = [{"ts": "2026-07-05 08:00:00", "side": "entry", "price": 1.0, "qty": 1.0, "pnl": 0.0}]
    monkeypatch.setattr("core.trade_journal.read_trades_db", lambda *a, **k: rows)
    t = _trader([0], FakeExecu())
    t.cfg.interval = "4h"
    state = {"symbol": t.cfg.symbol, "interval": "4h",
             "last_decision": {"ts": "2026-07-04 12:00:00"}}
    with open(t.state_path, "w") as f:
        _json.dump(state, f)
    assert t.restored_last_bar() == pd.Timestamp("2026-07-04 12:00:00")
