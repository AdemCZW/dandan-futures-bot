"""機器人狀態持久化 + 重啟還原 — 解決「崩潰重啟後把帳上的幣當成空手」的問題。

run_live 把每次進出場後的持倉狀態原子寫入 bot_state.json。重啟時：
  1. 讀回上次狀態（entry_price / sl / tp 才會精確）。
  2. 以「交易所實際餘額」為準做校正（reconcile）——餘額才是真相，狀態檔只是補充
     entry/sl/tp 這些餘額看不出來的資訊。

三種需要處理的情況：
  - 帳上有幣 + 狀態說持倉 → 信任狀態（entry/sl/tp 完整）。
  - 帳上有幣 + 狀態說空手（狀態檔遺失/損毀）→ 還原為持倉，entry 以現價估、sl/tp 重設並警告。
  - 帳上沒幣 + 狀態說持倉（外部已平倉/被別處賣掉）→ 重設為空手並警告。
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone


@dataclass
class BotState:
    in_position: bool = False
    direction: int = 0          # +1 多 / -1 空 / 0 空手
    entry_price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    qty: float = 0.0
    symbol: str = ""
    strategy: str = ""
    updated_at: str = ""
    cb_consecutive_losses: int = 0    # Circuit Breaker：連續虧損計數
    cb_paused_until: str = ""         # Circuit Breaker：暫停到期時間（ISO 字串，空=未暫停）
    last_balance: float = 0.0         # 上次已知帳戶餘額，用於測試網重置偵測

    def save(self, path: str) -> None:
        """原子寫入：先寫 .tmp 再 rename，避免崩潰時寫到一半留下半截檔。"""
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "BotState":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return cls()
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


def detect_testnet_reset(
    current: float,
    last: float,
    drop_pct: float = 0.90,
    min_ref: float = 200.0,
) -> bool:
    """判斷帳戶餘額是否因測試網清帳而大幅歸零。

    current  目前餘額
    last     上一次記錄的餘額（0 代表首次啟動，不偵測）
    drop_pct 觸發閾值：跌幅 ≥ drop_pct 視為重置（預設 90%）
    min_ref  last 必須 ≥ min_ref 才偵測，避免小帳號誤觸發（預設 200 USDT）
    """
    if last < min_ref:
        return False
    return (1.0 - current / last) >= drop_pct


def reconcile(state: BotState, base_balance: float, dust: float, price: float,
              exit_levels) -> tuple[BotState, str]:
    """以交易所實際餘額為準校正持久化狀態。

    參數：
      base_balance  交易所目前 base asset 餘額（如 BTC）
      dust          視為「沒有部位」的門檻（建議用 LOT_SIZE.minQty）
      price         現價（狀態檔遺失時用來估 entry / 重設 sl,tp）
      exit_levels   callable(entry_price, direction) -> (sl, tp)，通常傳 risk.exit_levels
    回傳 (校正後的 BotState, 給使用者看的訊息)。
    """
    holding = base_balance > dust

    if holding and state.in_position:
        return state, f"還原持倉：entry {state.entry_price:.2f} / SL {state.sl:.2f} / TP {state.tp:.2f}"

    if holding and not state.in_position:
        sl, tp = exit_levels(price, 1)
        recovered = BotState(in_position=True, direction=1, entry_price=price,
                             sl=sl, tp=tp, qty=base_balance,
                             symbol=state.symbol, strategy=state.strategy)
        return recovered, (f"⚠️ 帳上有 {base_balance:.6f} 但無狀態檔：以現價 {price:.2f} 估 entry、"
                           f"重設 SL {sl:.2f}/TP {tp:.2f}（entry 可能不準，建議人工確認）")

    if not holding and state.in_position:
        return BotState(symbol=state.symbol, strategy=state.strategy), \
            "⚠️ 狀態說持倉但帳上沒幣（疑似外部已平倉）：重設為空手"

    return BotState(symbol=state.symbol, strategy=state.strategy), "空手啟動（無未平倉部位）"
