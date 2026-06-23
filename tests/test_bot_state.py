"""Tests for core/bot_state.py — BotState persistence + reconcile."""
from __future__ import annotations

import json
import os

import pytest

from core.bot_state import BotState, reconcile


# --- a deterministic exit_levels: sl/tp 由 entry 與方向決定 ---
def make_levels(p, direction):
    """direction=+1 多：sl 在下、tp 在上；其餘亦對稱。固定百分比，確定性。"""
    return (p * 0.95, p * 1.10)


# ---------------------------------------------------------------------------
# 持久化：save / load
# ---------------------------------------------------------------------------

def test_save_load_roundtrip_fields_consistent(tmp_path):
    path = str(tmp_path / "bot_state.json")
    st = BotState(
        in_position=True,
        direction=-1,
        entry_price=12345.67,
        sl=13000.0,
        tp=11000.0,
        qty=0.5,
        symbol="BTCUSDT",
        strategy="zscore_ls",
    )
    st.save(path)

    loaded = BotState.load(path)

    assert loaded.in_position is True
    assert loaded.direction == -1
    assert loaded.entry_price == 12345.67
    assert loaded.sl == 13000.0
    assert loaded.tp == 11000.0
    assert loaded.qty == 0.5
    assert loaded.symbol == "BTCUSDT"
    assert loaded.strategy == "zscore_ls"
    # save() 會寫入 updated_at（ISO 字串、非空）
    assert isinstance(loaded.updated_at, str) and loaded.updated_at != ""


def test_load_missing_path_returns_default(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    assert not os.path.exists(path)

    st = BotState.load(path)

    assert st == BotState()  # 全預設
    assert st.in_position is False
    assert st.direction == 0
    assert st.entry_price == 0.0
    assert st.symbol == ""


def test_load_corrupt_json_returns_default_no_raise(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{ this is not valid json :::")  # 損毀內容

    st = BotState.load(str(path))  # 不應丟例外

    assert st == BotState()
    assert st.in_position is False


def test_load_ignores_unknown_fields(tmp_path):
    """JSON 含未知欄位時只取合法欄位、不丟 TypeError。"""
    path = tmp_path / "extra.json"
    path.write_text(json.dumps({
        "in_position": True,
        "direction": 1,
        "entry_price": 100.0,
        "symbol": "ETHUSDT",
        "bogus_field": "ignore me",
    }))

    st = BotState.load(str(path))

    assert st.in_position is True
    assert st.direction == 1
    assert st.entry_price == 100.0
    assert st.symbol == "ETHUSDT"
    assert not hasattr(st, "bogus_field")


def test_save_is_atomic_no_tmp_leftover(tmp_path):
    path = str(tmp_path / "bot_state.json")
    st = BotState(in_position=True, symbol="BTCUSDT")
    st.save(path)

    # 主檔存在、.tmp 不殘留
    assert os.path.exists(path)
    assert not os.path.exists(path + ".tmp")
    # 目錄裡只有目標檔，沒有任何 .tmp
    leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
    assert leftovers == []


def test_save_then_load_then_resave_stable(tmp_path):
    """連續 save→load→save 不破壞欄位（除 updated_at 會更新）。"""
    path = str(tmp_path / "s.json")
    st = BotState(in_position=True, direction=1, entry_price=200.0,
                  sl=190.0, tp=220.0, qty=1.0, symbol="BTCUSDT")
    st.save(path)
    loaded = BotState.load(path)
    loaded.save(path)
    again = BotState.load(path)

    assert again.in_position is True
    assert again.direction == 1
    assert again.entry_price == 200.0
    assert again.sl == 190.0
    assert again.tp == 220.0
    assert again.qty == 1.0
    assert again.symbol == "BTCUSDT"


# ---------------------------------------------------------------------------
# reconcile：四情境
# ---------------------------------------------------------------------------

def test_reconcile_holding_and_in_position_trusts_state(tmp_path):
    """有幣 + 狀態持倉 → 信任狀態，entry/sl/tp 不變。"""
    state = BotState(in_position=True, direction=-1, entry_price=30000.0,
                     sl=31000.0, tp=28000.0, qty=0.2, symbol="BTCUSDT",
                     strategy="zscore_ls")
    out, msg = reconcile(state, base_balance=0.2, dust=0.001, price=29500.0,
                         exit_levels=make_levels)

    assert out is state  # 直接信任、原物件回傳
    assert out.in_position is True
    assert out.direction == -1
    assert out.entry_price == 30000.0  # entry 不變
    assert out.sl == 31000.0
    assert out.tp == 28000.0
    assert "還原持倉" in msg


def test_reconcile_holding_no_state_recovers_entry_at_price(tmp_path):
    """有幣 + 無狀態（空手）→ 還原為持倉，entry=現價、sl/tp 由 exit_levels 重設。"""
    state = BotState(in_position=False, symbol="BTCUSDT", strategy="zscore_ls")
    price = 25000.0
    out, msg = reconcile(state, base_balance=0.3, dust=0.001, price=price,
                         exit_levels=make_levels)

    assert out.in_position is True
    assert out.direction == 1
    assert out.entry_price == price          # entry = 現價
    assert out.qty == 0.3                      # qty = 餘額
    expected_sl, expected_tp = make_levels(price, 1)
    assert out.sl == expected_sl
    assert out.tp == expected_tp
    assert out.symbol == "BTCUSDT"             # 保留 symbol/strategy
    assert out.strategy == "zscore_ls"
    assert "⚠️" in msg


def test_reconcile_no_coin_but_state_in_position_resets_flat(tmp_path):
    """無幣 + 狀態持倉 → 重設為空手（疑似外部已平倉）。"""
    state = BotState(in_position=True, direction=1, entry_price=30000.0,
                     sl=29000.0, tp=33000.0, qty=0.5, symbol="BTCUSDT",
                     strategy="zscore_ls")
    out, msg = reconcile(state, base_balance=0.0, dust=0.001, price=31000.0,
                         exit_levels=make_levels)

    assert out.in_position is False
    assert out.direction == 0
    assert out.entry_price == 0.0
    assert out.sl == 0.0
    assert out.tp == 0.0
    assert out.qty == 0.0
    assert out.symbol == "BTCUSDT"             # 保留 symbol/strategy
    assert out.strategy == "zscore_ls"
    assert "重設為空手" in msg


def test_reconcile_no_coin_flat_stays_flat(tmp_path):
    """無幣 + 空手 → 維持空手。"""
    state = BotState(in_position=False, symbol="BTCUSDT", strategy="zscore_ls")
    out, msg = reconcile(state, base_balance=0.0, dust=0.001, price=31000.0,
                         exit_levels=make_levels)

    assert out.in_position is False
    assert out.direction == 0
    assert out.entry_price == 0.0
    assert out.qty == 0.0
    assert out.symbol == "BTCUSDT"
    assert out.strategy == "zscore_ls"
    assert "空手啟動" in msg


def test_reconcile_dust_threshold_balance_below_dust_is_flat(tmp_path):
    """餘額 <= dust 視為沒有部位（剛好等於 dust 也算空手）。"""
    state = BotState(in_position=True, direction=1, entry_price=30000.0,
                     sl=29000.0, tp=33000.0, qty=0.001, symbol="BTCUSDT")
    # base_balance 等於 dust：holding = base_balance > dust = False
    out, msg = reconcile(state, base_balance=0.001, dust=0.001, price=31000.0,
                         exit_levels=make_levels)

    assert out.in_position is False
    assert "重設為空手" in msg
