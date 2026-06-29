"""方向感知通道護欄 — fib_channel reversion 的「連虧不換方向」防呆。

問題：均值回歸在通道頂做空/底做多。當市場跳出震盪、走出單邊趨勢時，
通道方向(ch_dir)會慢半拍，reversion 仍一直逆勢接刀 → 連續同方向虧損。

本護欄：連續 max_losses 筆「同方向」平倉虧損 → 暫停『該方向』新進場，直到
  (a) 通道方向相對封鎖當下翻轉（通道已重新形成），或
  (b) 冷卻 cooldown_bars 根 K 棒過去。
只擋『新進場』，不影響既有持倉的出場與風控。被擋方向贏一筆即歸零並解封。

與既有 CircuitBreaker 的差異：CircuitBreaker 是「不分方向、連虧就整台暫停」；
本護欄「分方向、只擋虧的那一邊」，保留另一方向繼續交易。預設停用（opt-in），
僅在需要的 bot（如 Bot2）以 env 開啟，其餘 bot 行為完全不變。
"""
from __future__ import annotations


class DirectionalChannelGuard:
    def __init__(self, max_losses: int = 3, cooldown_bars: int = 8, enabled: bool = True):
        self.max_losses = int(max_losses)
        self.cooldown_bars = int(cooldown_bars)
        self.enabled = bool(enabled)
        self.loss_dir = 0          # 目前連虧方向 +1/-1（0=無）
        self.loss_count = 0        # 同方向連虧筆數
        self.blocked_dir = 0       # 被封鎖的方向 +1/-1（0=未封鎖）
        self.cooldown_left = 0     # 剩餘冷卻 K 棒數
        self.block_chdir = 0       # 封鎖當下的通道方向（翻轉即解封）
        self._last_ch_dir = 0      # 最近一次 on_bar 看到的通道方向

    # ── 每根 K 棒 ────────────────────────────────────────────────────────────
    def on_bar(self, ch_dir: int = 0) -> None:
        """每根 K 棒收盤呼叫一次：更新通道方向、推進冷卻、必要時解封。"""
        self._last_ch_dir = int(ch_dir or 0)
        if self.blocked_dir == 0:
            return
        # 通道方向翻轉 → 通道已重新形成，解封
        if (self.block_chdir != 0 and self._last_ch_dir != 0
                and self._last_ch_dir != self.block_chdir):
            self._unblock()
            return
        if self.cooldown_left > 0:
            self.cooldown_left -= 1
            if self.cooldown_left <= 0:
                self._unblock()

    # ── 平倉回報 ────────────────────────────────────────────────────────────
    def record_trade(self, direction: int, pnl: float) -> None:
        """平倉時呼叫。direction = 該倉方向 (+1/-1)，pnl = 本筆損益。"""
        direction = int(direction)
        if pnl < 0:
            if direction == self.loss_dir:
                self.loss_count += 1
            else:
                self.loss_dir = direction
                self.loss_count = 1
            if self.loss_count >= self.max_losses:
                self.blocked_dir = direction
                self.cooldown_left = self.cooldown_bars
                self.block_chdir = self._last_ch_dir
        else:                                   # 贏或打平
            if direction == self.loss_dir:
                self.loss_dir = 0
                self.loss_count = 0
            if direction == self.blocked_dir:
                self._unblock()

    # ── 進場閘門 ────────────────────────────────────────────────────────────
    def blocks(self, direction: int) -> bool:
        """該方向是否正被封鎖（停用時恆 False）。"""
        return self.enabled and self.blocked_dir != 0 and int(direction) == self.blocked_dir

    def _unblock(self) -> None:
        self.blocked_dir = 0
        self.cooldown_left = 0
        self.block_chdir = 0
        self.loss_dir = 0
        self.loss_count = 0

    # ── 持久化 ──────────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "loss_dir": self.loss_dir,
            "loss_count": self.loss_count,
            "blocked_dir": self.blocked_dir,
            "cooldown_left": self.cooldown_left,
            "block_chdir": self.block_chdir,
        }

    @classmethod
    def from_dict(cls, data: dict, max_losses: int = 3,
                  cooldown_bars: int = 8, enabled: bool = True) -> "DirectionalChannelGuard":
        g = cls(max_losses=max_losses, cooldown_bars=cooldown_bars, enabled=enabled)
        if data:
            g.loss_dir      = int(data.get("loss_dir", 0))
            g.loss_count    = int(data.get("loss_count", 0))
            g.blocked_dir   = int(data.get("blocked_dir", 0))
            g.cooldown_left = int(data.get("cooldown_left", 0))
            g.block_chdir   = int(data.get("block_chdir", 0))
        return g
