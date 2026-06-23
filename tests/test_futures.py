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
    def __init__(self, positions=None, balances=None, price="100"):
        self._pos = positions or [{"symbol": "BTCUSDT", "positionAmt": "0"}]
        self._bal = balances or [{"asset": "USDT", "balance": "10000"}]
        self._price = price
        self.created = []
        self.leverage_calls = []

    def futures_exchange_info(self): return INFO
    def futures_change_leverage(self, **kw): self.leverage_calls.append(kw)
    def futures_create_order(self, **kw): self.created.append(kw); return {"orderId": 1, **kw}
    def futures_position_information(self, symbol=None): return self._pos
    def futures_account_balance(self): return self._bal
    def futures_symbol_ticker(self, symbol=None): return {"symbol": symbol, "price": self._price}


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
    def position_amt(self): return self.amt
    def balance(self, a="USDT"): return 10000.0
    def mark_price(self): return 100.0
    def valid_order(self, q, p): return True, "ok"
    def open_long(self, q): self.amt = float(q); self.orders.append(("open_long", float(q)))
    def open_short(self, q): self.amt = -float(q); self.orders.append(("open_short", float(q)))
    def close(self, q, d): self.amt = 0.0; self.orders.append(("close", float(q), d))


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
    assert "exit_sltp" in t.journal.logs


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
    assert t.dir == 0 and "exit_sltp" in t.journal.logs      # 暴量也要停損出場
    assert ex.orders[-1][0] == "close"
