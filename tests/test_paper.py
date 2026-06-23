"""PaperBroker 測試：本地模擬成交的現金/持倉/手續費/滑點/夾量。"""
import pytest

from config import Config
from core.paper_broker import PaperBroker


def test_buy_updates_cash_and_base_with_fee():
    b = PaperBroker(Config(), quote_start=10000.0)
    r = b.market_buy(0.1, 100.0)            # fee 0.001, slip 0 → cost 0.1*100*1.001 = 10.01
    assert r["qty"] == pytest.approx(0.1)
    assert b.base == pytest.approx(0.1)
    assert b.cash == pytest.approx(10000 - 10.01)


def test_buy_clips_to_affordable():
    b = PaperBroker(Config(), quote_start=5.0)
    r = b.market_buy(1.0, 100.0)            # 買不起 1.0 → 夾到 ~5/(100.1)
    assert r["qty"] == pytest.approx(5.0 / 100.1, rel=1e-6)
    assert b.cash == pytest.approx(0.0, abs=1e-9)


def test_sell_clips_to_holdings_and_credits_cash():
    b = PaperBroker(Config(), quote_start=10000.0)
    b.market_buy(0.1, 100.0)
    r = b.market_sell(0.5, 100.0)           # 只持有 0.1 → 夾到 0.1
    assert r["qty"] == pytest.approx(0.1)
    assert b.base == pytest.approx(0.0, abs=1e-12)


def test_flat_roundtrip_loses_only_fees():
    b = PaperBroker(Config(), quote_start=10000.0)
    b.market_buy(0.1, 100.0)
    b.market_sell(0.1, 100.0)
    # 平盤來回只虧雙邊手續費：0.1*100*0.001 *2 = 0.02
    assert b.equity(100.0) == pytest.approx(10000 - 0.02, abs=1e-6)


def test_slippage_makes_fills_worse():
    cfg = Config(); cfg.slippage = 0.001
    b = PaperBroker(cfg, quote_start=10000.0)
    rb = b.market_buy(0.1, 100.0)
    assert rb["fill"] == pytest.approx(100.0 * 1.001)     # 買進滑點往上
    rs = b.market_sell(0.05, 100.0)
    assert rs["fill"] == pytest.approx(100.0 * 0.999)     # 賣出滑點往下


def test_equity_marks_to_price():
    b = PaperBroker(Config(), quote_start=10000.0)
    b.market_buy(0.1, 100.0)
    assert b.equity(120.0) == pytest.approx(b.cash + 0.1 * 120.0)
