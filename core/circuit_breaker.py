"""Circuit Breaker — 連續虧損自動暫停機制。

連續虧損達 max_losses 筆時暫停交易 pause_hours 小時；
贏一筆即歸零計數器。狀態可序列化到 BotState 持久化。
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta


class CircuitBreaker:
    def __init__(self, max_losses: int = 3, pause_hours: float = 24):
        self.max_losses = max_losses
        self.pause_hours = pause_hours
        self.consecutive_losses = 0
        self._paused_until: datetime | None = None

    @property
    def tripped(self) -> bool:
        return self._paused_until is not None

    def record_trade(self, pnl: float) -> None:
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.max_losses and self._paused_until is None:
                self._paused_until = datetime.now(timezone.utc) + timedelta(hours=self.pause_hours)
                print(f"[熔斷] 連續虧損 {self.consecutive_losses} 筆，"
                      f"暫停至 {self._paused_until.strftime('%Y-%m-%d %H:%M UTC')}")
        else:
            self.consecutive_losses = 0

    def is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        if datetime.now(timezone.utc) >= self._paused_until:
            self._paused_until = None
            self.consecutive_losses = 0
            print("[熔斷] 暫停期已到，恢復交易")
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "consecutive_losses": self.consecutive_losses,
            "paused_until": self._paused_until.isoformat() if self._paused_until else None,
        }

    @classmethod
    def from_dict(cls, data: dict, max_losses: int = 3, pause_hours: float = 24) -> "CircuitBreaker":
        cb = cls(max_losses=max_losses, pause_hours=pause_hours)
        cb.consecutive_losses = data.get("consecutive_losses", 0)
        pu = data.get("paused_until")
        if pu:
            loaded = datetime.fromisoformat(pu)
            now = datetime.now(timezone.utc)
            # 新設定的 pause_hours 為上限：若設定縮短，舊暫停跟著縮短（立即生效）
            cap = now + timedelta(hours=pause_hours)
            effective = min(loaded, cap)
            cb._paused_until = effective if effective > now else None
        return cb
