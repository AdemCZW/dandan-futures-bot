"""Circuit Breaker — 連續虧損熔斷測試。"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest

from core.circuit_breaker import CircuitBreaker


def _cb(max_losses=3, pause_hours=24):
    return CircuitBreaker(max_losses=max_losses, pause_hours=pause_hours)


def test_initial_state_not_tripped():
    cb = _cb()
    assert not cb.tripped
    assert cb.consecutive_losses == 0


def test_loss_increments_counter():
    cb = _cb(max_losses=3)
    cb.record_trade(pnl=-10.0)
    assert cb.consecutive_losses == 1
    assert not cb.tripped


def test_win_resets_counter():
    cb = _cb(max_losses=3)
    cb.record_trade(pnl=-10.0)
    cb.record_trade(pnl=-5.0)
    cb.record_trade(pnl=+20.0)
    assert cb.consecutive_losses == 0
    assert not cb.tripped


def test_n_consecutive_losses_trips_breaker():
    cb = _cb(max_losses=3, pause_hours=24)
    cb.record_trade(pnl=-1.0)
    cb.record_trade(pnl=-2.0)
    cb.record_trade(pnl=-3.0)
    assert cb.tripped
    assert cb.consecutive_losses == 3


def test_tripped_breaker_blocks_trading():
    cb = _cb(max_losses=2)
    cb.record_trade(pnl=-1.0)
    cb.record_trade(pnl=-2.0)
    assert cb.is_paused()


def test_pause_expires_after_hours():
    cb = _cb(max_losses=1, pause_hours=1)
    cb.record_trade(pnl=-1.0)
    assert cb.is_paused()
    # 模擬時間過了 1 小時
    cb._paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not cb.is_paused()
    assert cb.consecutive_losses == 0  # 解除後自動重置


def test_state_roundtrip():
    cb = _cb(max_losses=2, pause_hours=6)
    cb.record_trade(pnl=-5.0)
    state = cb.to_dict()
    cb2 = CircuitBreaker.from_dict(state, max_losses=2, pause_hours=6)
    assert cb2.consecutive_losses == cb.consecutive_losses
    assert cb2.tripped == cb.tripped
