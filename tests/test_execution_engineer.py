"""針對 core/execution_engineer.py 的單元測試。

策略：
  - 用 ExecutionEngineer.__new__ 建立實例以略過 __init__（避免真的去打幣安網路）。
  - 手動塞入 _filters（皆為 Decimal），把行為限縮成純函式測試。
  - _load_filters 相關測試則用「假 client」餵不同的 get_symbol_info 回傳值。

所有測試使用確定性資料與明確 assert，不寫入任何檔案。
"""
from decimal import Decimal

import pytest

from core.execution_engineer import ExecutionEngineer


# --------------------------------------------------------------------------
# 輔助：建立一個略過 __init__、可手動設定 _filters 的引擎
# --------------------------------------------------------------------------
def make_engine(filters, symbol="BTCUSDT", client=None):
    eng = ExecutionEngineer.__new__(ExecutionEngineer)
    eng.symbol = symbol
    eng.client = client
    eng._filters = filters
    return eng


def base_filters(**overrides):
    f = {
        "step_size": Decimal("0.00000001"),  # 1e-8
        "min_qty": Decimal("0.00000001"),
        "tick_size": Decimal("0.01"),
        "min_notional": Decimal("10"),
    }
    f.update(overrides)
    return f


# 一個極簡的假 Binance client：只實作 get_symbol_info
class FakeClient:
    def __init__(self, symbol_info):
        self._symbol_info = symbol_info
        self.calls = []

    def get_symbol_info(self, symbol):
        self.calls.append(symbol)
        return self._symbol_info


# --------------------------------------------------------------------------
# round_qty
# --------------------------------------------------------------------------
def test_round_qty_tiny_value_is_fixed_point_no_scientific():
    """極小值 1.23e-06 必須回傳定點字串 "0.00000123"，不得出現科學記號。"""
    eng = make_engine(base_filters(step_size=Decimal("0.00000001")))
    out = eng.round_qty(1.23e-06)
    assert out == "0.00000123"
    assert isinstance(out, str)
    assert "e" not in out.lower()  # 無科學記號


def test_round_qty_truncates_to_step_multiple_and_not_exceed_original():
    """捨去後須為 stepSize 整數倍，且 <= 原值（無條件捨去）。"""
    step = Decimal("0.001")
    eng = make_engine(base_filters(step_size=step))

    for raw in [1.0049, 1.005, 12.3456, 0.123456789, 5.0]:
        out = eng.round_qty(raw)
        d = Decimal(out)
        # 為 stepSize 整數倍
        assert (d % step) == Decimal("0")
        # 不超過原值
        assert d <= Decimal(str(raw))
        # 與原值的差距小於一個 step（確認是「捨去到最近的下一格」而非過度捨去）
        assert Decimal(str(raw)) - d < step
        assert "e" not in out.lower()


def test_round_qty_below_one_step_returns_zero():
    """小於一個 step 的量捨去後為 0，且為定點 "0"。"""
    eng = make_engine(base_filters(step_size=Decimal("1")))
    out = eng.round_qty(0.5)
    assert out == "0"
    assert Decimal(out) == Decimal("0")


def test_round_qty_exact_multiple_unchanged():
    """剛好是 step 整數倍時，數值不變。"""
    eng = make_engine(base_filters(step_size=Decimal("0.01")))
    out = eng.round_qty(12.34)
    assert Decimal(out) == Decimal("12.34")
    assert "e" not in out.lower()


# --------------------------------------------------------------------------
# round_price
# --------------------------------------------------------------------------
def test_round_price_truncates_to_tick_multiple_and_not_exceed_original():
    tick = Decimal("0.01")
    eng = make_engine(base_filters(tick_size=tick))

    for raw in [20000.123, 20000.129, 0.017, 100.0]:
        out = eng.round_price(raw)
        d = Decimal(out)
        assert (d % tick) == Decimal("0")
        assert d <= Decimal(str(raw))
        assert Decimal(str(raw)) - d < tick
        assert "e" not in out.lower()


def test_round_price_tiny_tick_no_scientific():
    """極小 tick 與極小 price 也要是定點字串。"""
    eng = make_engine(base_filters(tick_size=Decimal("0.0001")))
    out = eng.round_price(0.000123456)
    assert out == "0.0001"
    assert "e" not in out.lower()


# --------------------------------------------------------------------------
# valid_order：用「修整後」數量檢查
# --------------------------------------------------------------------------
def test_valid_order_uses_rounded_qty_for_notional_check():
    """原始 qty 名目達標，但捨去後跌破 min_notional 應回 False。

    step=0.001、min_notional=10、price=10000。
    qty=0.0019 名目=19（>10），但捨去到 0.001 後名目=10... 改用更貼邊的例子：
    qty=0.0010009 -> 名目=10.009（>10），捨去到 0.001 -> 名目=10.0（剛好等於，不算跌破）。
    為了確實跌破，用 price 讓捨去後 < min_notional：
    qty=0.0019、捨去到 0.001、price=5000 -> 修整後名目=5 < 10 -> False，
    而「未修整」名目=0.0019*5000=9.5 仍 < 10，無法區分。
    故採 price=9999：原始 0.0019*9999=18.99(>10)，修整 0.001*9999=9.999(<10)。
    """
    eng = make_engine(
        base_filters(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("10"),
        )
    )
    ok, msg = eng.valid_order(qty=0.0019, price=9999)
    # 修整後數量 0.001，名目 9.999 < 10
    assert ok is False
    assert "名目金額" in msg


def test_valid_order_passes_when_rounded_notional_meets_min():
    eng = make_engine(
        base_filters(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("10"),
        )
    )
    # 修整後 0.002，名目 0.002*10000=20 >= 10
    ok, msg = eng.valid_order(qty=0.0025, price=10000)
    assert ok is True
    assert msg == "ok"


def test_valid_order_min_qty_boundary_just_below_fails():
    """修整後數量低於 min_qty 應回 False（含 min_qty 訊息）。"""
    eng = make_engine(
        base_filters(
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.01"),
            min_notional=Decimal("0"),
        )
    )
    # qty=0.009 捨去到 0.009 < min_qty 0.01
    ok, msg = eng.valid_order(qty=0.009, price=100000)
    assert ok is False
    assert "最小下單量" in msg


def test_valid_order_min_qty_boundary_exact_passes():
    """剛好等於 min_qty（且名目達標）應通過。"""
    eng = make_engine(
        base_filters(
            step_size=Decimal("0.01"),
            min_qty=Decimal("0.01"),
            min_notional=Decimal("0"),
        )
    )
    ok, msg = eng.valid_order(qty=0.01, price=100000)
    assert ok is True
    assert msg == "ok"


# --------------------------------------------------------------------------
# _load_filters
# --------------------------------------------------------------------------
def _symbol_info(filters_list):
    return {"symbol": "BTCUSDT", "filters": filters_list}


LOT_SIZE = {"filterType": "LOT_SIZE", "stepSize": "0.00001000", "minQty": "0.00001000"}
PRICE_FILTER = {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"}


def test_load_filters_none_symbol_info_raises_valueerror():
    """get_symbol_info 回 None 時應丟 ValueError。"""
    eng = make_engine({}, symbol="NOSUCHPAIR", client=FakeClient(symbol_info=None))
    with pytest.raises(ValueError):
        eng._load_filters()


def test_load_filters_with_notional_filter():
    """有 NOTIONAL filter 時，min_notional 取自 NOTIONAL.minNotional。"""
    info = _symbol_info(
        [
            LOT_SIZE,
            PRICE_FILTER,
            {"filterType": "NOTIONAL", "minNotional": "10.00000000"},
        ]
    )
    eng = make_engine({}, client=FakeClient(symbol_info=info))
    f = eng._load_filters()
    assert f["min_notional"] == Decimal("10.00000000")
    assert f["step_size"] == Decimal("0.00001000")
    assert f["min_qty"] == Decimal("0.00001000")
    assert f["tick_size"] == Decimal("0.01000000")


def test_load_filters_fallback_to_min_notional_filter():
    """沒有 NOTIONAL、只有舊式 MIN_NOTIONAL 時，fallback 取得 minNotional。"""
    info = _symbol_info(
        [
            LOT_SIZE,
            PRICE_FILTER,
            {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"},
        ]
    )
    eng = make_engine({}, client=FakeClient(symbol_info=info))
    f = eng._load_filters()
    assert f["min_notional"] == Decimal("5.00000000")


def test_load_filters_notional_takes_precedence_over_min_notional():
    """兩者皆存在時，NOTIONAL 優先（get('NOTIONAL', ...) 先命中）。"""
    info = _symbol_info(
        [
            LOT_SIZE,
            PRICE_FILTER,
            {"filterType": "NOTIONAL", "minNotional": "10.00000000"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5.00000000"},
        ]
    )
    eng = make_engine({}, client=FakeClient(symbol_info=info))
    f = eng._load_filters()
    assert f["min_notional"] == Decimal("10.00000000")


def test_load_filters_no_notional_filter_defaults_to_zero():
    """完全沒有任何 notional filter 時，min_notional 預設為 0。"""
    info = _symbol_info([LOT_SIZE, PRICE_FILTER])
    eng = make_engine({}, client=FakeClient(symbol_info=info))
    f = eng._load_filters()
    assert f["min_notional"] == Decimal("0")
