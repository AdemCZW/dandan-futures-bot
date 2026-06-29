"""全域設定。所有可調參數集中在這裡，方便回測/實盤共用。"""
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── 交易標的 ──────────────────────────────────
    symbol: str = "BTCUSDT"
    interval: str = "5m"          # 5m / 15m / 1h ...（幣安 K 線週期）
    quote_asset: str = "USDT"
    base_asset: str = "BTC"

    # ── 風控（風控官 risk-officer 使用）────────────
    start_equity: float = 10_000.0    # 起始資金（USDT），回測用
    risk_per_trade: float = 0.01      # 每筆交易最多冒總資金 1% 的風險
    max_position_pct: float = 0.30    # 單一持倉最多佔總資金 30%
    stop_loss_pct: float = 0.02       # 停損 2%（atr 不可用時的 fallback）
    take_profit_pct: float = 0.04     # 停利 4%（atr 不可用時的 fallback）
    max_daily_loss_pct: float = 0.05    # 單日虧損超過 5% 就停止當日交易
    max_peak_drawdown_pct: float = 0.20 # 從淨值高點回落超過 20% → 全停（0=停用）
    # ATR 動態停損停利（有傳入 atr 時優先於固定百分比）：
    atr_mult_sl: float = 2.0          # 停損 = entry ∓ atr_mult_sl × ATR（波動度自適應）
    tp_R_mult: float = 2.0            # 停利距離 = tp_R_mult × 停損距離（鎖定恆定風報比 R）
    chand_mult: float = 3.0           # Chandelier 追蹤停損的 ATR 倍數（趨勢單保利）
    fee_rate: float = 0.001           # 手續費 0.1%（回測估算用）
    taker_fee_rate: float = 0.0004    # 實盤合約 taker 單邊費率（OPT-01：實盤平倉記帳扣費，與回測 fee_rate 分離）
    slippage: float = 0.0             # 滑點：買成交×(1+slip)、賣成交×(1-slip)。預設 0＝不改既有結果

    # ── 策略選擇（量化研究員 quant-researcher）──────
    # "ema_cross" / "zscore_revert"（僅做多）/ "zscore_ls"（多空雙向）
    # 註：zscore_ls 只有支援做空的回測引擎會真的開空單；run_live 在現貨上會把 -1 當平倉、不放空。
    strategy: str = "ema_cross"
    strategy_params: dict = field(default_factory=dict)

    # ── 金鑰（執行工程師 execution-engineer）────────
    # 用 default_factory：每次實例化才讀環境變數，不在 import 期就凍結，
    # 這樣 load_dotenv / 之後注入的環境變數（或測試 monkeypatch）才會生效。
    api_key: str = field(default_factory=lambda: os.getenv("BINANCE_TESTNET_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_TESTNET_API_SECRET", ""))

    # ── 合約測試網金鑰（run_live_futures 使用，支援做空）────────
    # 與現貨測試網【完全獨立】，需在 https://testnet.binancefuture.com 另外產生。
    futures_api_key: str = field(default_factory=lambda: os.getenv("BINANCE_FUTURES_TESTNET_API_KEY", ""))
    futures_api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_FUTURES_TESTNET_API_SECRET", ""))
    futures_leverage: int = 1         # 預設不放大槓桿，降低爆倉風險

    # 實盤輪詢間隔（秒）。5m K 線就每 ~30s 輪詢一次足夠
    poll_seconds: int = 30
