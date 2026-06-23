# 丹丹交易團隊 — 合約測試網量化交易機器人

> ⚠️ **全程使用虛擬資金，不會花到一毛真錢。** 這是工程／學習範本，**不是投資建議**。任何績效數字都不構成獲利保證。

---

## 硬性規則

1. **只連測試網。** `futures_execution_engineer.py` 永遠 `testnet=True`，合約指向 `https://testnet.binancefuture.com`。
2. **不做一鍵切換正式網的捷徑。** 切實盤風險自負，不在本專案範圍。
3. **回測要誠實。** 用「已收完」的 K 線下信號、含手續費、不偷看未來（no look-ahead）。
4. **金鑰不入 chat。** 所有金鑰只填 `.env`，程式透過 `load_dotenv()` 讀取，絕不硬寫或打印。

---

## 架構：6 個角色 = 6 個模組

| 角色 | 模組 | 職責 |
|------|------|------|
| 市場分析師 | `core/market_analyst.py` | 抓 K 線、taker_base（主動買量）、暴量異常偵測 |
| 信號工程師 | `core/signal_engineer.py` | EMA / RSI / ATR / z-score / ADX / ER / CHOP / Regime / Fibonacci / Supertrend / Donchian / CVD |
| 量化研究員 | `core/quant_researcher.py` | 7 個策略邏輯、Regime 過濾、訂單流結構閘門、產生目標倉位信號 |
| 風控官 | `core/risk_officer.py` | ATR 動態停損、R-倍數停利、Chandelier 追蹤停損、單日熔斷、槓桿感知倉位計算 |
| 執行工程師 | `core/futures_execution_engineer.py` | 合約測試網下單、精度修正（stepSize / minQty） |
| 回測工程師 | `backtest/backtester.py` | 歷史回測（多/空、滑點、Chandelier 追蹤）、績效指標 |
| 交易日誌 | `core/trade_journal.py` | SQLite + CSV 留底 |
| 狀態還原 | `core/bot_state.py` | 持久化持倉，崩潰後還原（以交易所餘額為準） |

---

## 七個策略（含 OOS 驗證結果）

| 策略名 | 類型 | 做空 | OOS 邊際（4h BTC） | 說明 |
|--------|------|------|--------------------|------|
| `supertrend` | 趨勢跟蹤 | ✅ | **+1.12%/fold ✅** | ATR 帶 Supertrend，`period=10, mult=3.0`；4h 唯一驗證有 edge 的主力策略 |
| `donchian` | 通道突破 | ✅ | +0.24%/fold ✅ | Donchian 通道（Turtle 系統），進場 20 根、出場 10 根 |
| `fib_retracement` | 均值回歸 | ✅ | ~0（4h 持平） | Fibonacci 回調 + swing pivot + Regime 過濾 + 訂單流閘門 |
| `zscore_ls` | 均值回歸 | ✅ | OOS 負 | z-score 雙向；5m/15m/1h 全虧，過度交易 |
| `zscore_revert` | 均值回歸 | 否 | OOS 負 | z-score 僅做多 |
| `ema_cross` | 趨勢 | 否 | OOS 負 | EMA 快慢線交叉，僅做多 |
| `of_momentum` | 訂單流動量 | ✅ | OOS 負 | CVD MACD（主動買賣量差），短線費用侵蝕 |

**walk-forward 結論**：短線（5m/15m）全策略 OOS 虧損，根因是效率市場 + 手續費地板 + 過度交易。4h `supertrend` 是唯一具備 OOS 正 edge 的策略。

---

## 技術指標

| 指標 | 用途 |
|------|------|
| ATR 動態停損 | `entry ∓ atr_mult_sl × ATR`，比固定百分比更貼近波動 |
| R-倍數停利 | `tp_R_mult × 停損距離`，保持正期望值 |
| Chandelier 追蹤停損 | 只向有利方向移動，鎖住利潤 |
| ADX | 趨勢強度（>25 強趨勢） |
| Efficiency Ratio (ER) | 方向效率，>0.618 趨勢，<0.382 盤整 |
| Choppiness Index (CHOP) | >61.8 盤整、<38.2 趨勢 |
| Regime | ER + CHOP + ADX 2-of-3 多數決 + debounce |
| Fibonacci 擺動樞軸 | 因果 swing pivot（無重繪），38.2%/61.8% 回調進場 |
| Supertrend | 遞迴 ATR 帶鎖定，`st_dir` 翻轉即換邊 |
| Donchian Channel | `shift(1)` 前窗滾動高低，因果通道突破 |
| CVD（累積成交量差） | 主動買量 − 主動賣量，`taker_base` 來源 |
| taker_buy_ratio | 主動買量 / 總量，EMA 平滑 |

---

## 雙 Bot 部署架構（Railway 雲端 24/7）

```
Railway Service 1（主力長線）         Railway Service 2（對照短線）
────────────────────────────         ────────────────────────────
supertrend 4h BTCUSDT                donchian 15m ETHUSDT
BOT_STRATEGY=supertrend              BOT_STRATEGY=donchian
BOT_SYMBOL=BTCUSDT                   BOT_SYMBOL=ETHUSDT
BOT_INTERVAL=4h                      BOT_INTERVAL=15m
BOT_LEV=3                            BOT_LEV=3
BOT_BUDGET=500                       BOT_BUDGET=200
OOS 驗證：+1.12%/fold ✅             OOS 驗證：−1.2%/fold ❌（對照組）
```

```
本機 Mac
──────────────────────────────────────────────
webapp/backend (FastAPI :8000)
  ├─ /api/live   → 讀 RAILWAY_BOT_URL 狀態
  ├─ /api/live2  → 讀 RAILWAY_BOT_URL_2 狀態
  ├─ /api/whales → Binance 籌碼 + Hyperliquid 前30
  └─ /api/hl-leaderboard → Hyperliquid 排行榜

webapp/frontend (Vite/React :5173)
  即時監控 · 大戶籌碼 · 回測 · 優化 · 日誌
```

---

## 安裝

```bash
cd /Users/adem/量化機器
uv venv .venv --python 3.12
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
npm --prefix webapp/frontend install
```

---

## .env 設定

```
# 合約測試網金鑰（https://testnet.binancefuture.com 產生）
BINANCE_FUTURES_TESTNET_API_KEY=你的64字元key
BINANCE_FUTURES_TESTNET_API_SECRET=你的64字元secret

# Railway Bot URL（後端呼叫 Railway 服務讀狀態）
RAILWAY_BOT_URL=https://dandan-futures-bot-production.up.railway.app
RAILWAY_BOT_URL_2=https://dandan-shortterm-production.up.railway.app
```

---

## 使用方式

```bash
# 純回測（不需金鑰）
python run_backtest.py supertrend --plot --report
python run_backtest.py donchian --plot --report
python run_backtest.py fib_retracement --plot --report

# 策略矩陣 walk-forward（無金鑰，抓公開行情）
python research_matrix.py        # 所有策略 × 所有時框的 OOS 排行
python research_structure.py     # 純 TA vs TA + 訂單流過濾 A/B 對比

# 參數掃描 + walk-forward
python run_optimize.py supertrend
python run_optimize.py donchian
python run_optimize.py fib_retracement

# Paper 模擬（真實行情 + 本機模擬成交，免金鑰）
python run_paper.py --interval 1h --poll 30 --strategy supertrend

# 合約測試網實盤（需合約測試網金鑰）
python run_live_futures.py --strategy supertrend --symbol BTCUSDT --interval 4h --leverage 3 --budget 500
python run_live_futures.py --strategy donchian --symbol ETHUSDT --interval 15m --leverage 3 --budget 200

# 測試
.venv/bin/python -m pytest -q    # 259 個單元測試
```

---

## 即時監控儀表板

```bash
./dashboard.sh start    # 一鍵啟動後端 + paper bot + 前端
                        # → http://localhost:5173
./dashboard.sh status
./dashboard.sh stop
```

五個分頁：
- **即時監控** — 長線主力 + 短線對照實驗面板、SOP 6 角色決策、技術指標可視化
- **大戶籌碼** — Binance 持倉情緒 + Hyperliquid 前 30 大戶帳戶部位
- **回測** — 權益曲線、績效卡片、交易明細
- **參數最佳化** — walk-forward 熱圖
- **交易日誌** — 讀 `trades.db`

---

## Railway 雲端部署

### 環境變數（在 railway.app → Variables 設定）

```
BINANCE_FUTURES_TESTNET_API_KEY=...
BINANCE_FUTURES_TESTNET_API_SECRET=...
BOT_STRATEGY=supertrend      # 或 donchian
BOT_SYMBOL=BTCUSDT           # 或 ETHUSDT
BOT_INTERVAL=4h              # 或 15m
BOT_LEV=3
BOT_POLL=30
BOT_BUDGET=500
```

- 設定由 Python argparse 從環境變數讀取（Railway 不展開 `${VAR}` shell 插值）
- `startCommand` 只需 `python -u run_live_futures.py`，不需帶任何參數
- Healthcheck：`/health` 端點，狀態 HTTP server 在 `main()` 最開頭啟動，確保 Railway healthcheck 必過

### 服務健康保護

`_start_state_server()` 在任何 Binance API 呼叫之前啟動，保證：
- 金鑰未設定 → process 保持存活（`while True: sleep(30)`），healthcheck 通過，可在 Railway console 診斷
- Binance API 初始化失敗 → try/except 捕獲，process 保持存活

---

## 風控參數（`config.py`）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `atr_mult_sl` | 2.0 | ATR 停損倍數 |
| `tp_R_mult` | 2.0 | R-倍數停利（停損距離的倍數）|
| `chand_mult` | 3.0 | Chandelier 追蹤停損 ATR 倍數 |
| `futures_leverage` | 由 `--leverage` 指定 | 合約槓桿（固定 `testnet=True`）|
| `max_position_pct` | 由 `--budget` 動態計算 | 每筆保證金上限 / 帳戶餘額 |

**槓桿倉位計算**：`max_notional = equity × max_position_pct × leverage`

---

## 重啟還原

`bot_state_futures.json` 每根 K 線原子寫入：持倉方向、入場價、停損、停利、Chandelier 高低點。
重啟時以「交易所實際帶號持倉量」為真相（`position_amt()`），狀態檔補 entry/SL/TP，避免崩潰後重複下單。

---

## 測試套件

```bash
.venv/bin/python -m pytest -q      # 259 個測試
```

關鍵測試：
- **前綴不變性**（prefix-invariance）：所有指標確保因果正確，不偷看未來
- **Chandelier 回歸**：restore() 後 peak/trough 正確，trailing stop 不損壞
- **Regime**：ER / CHOP / ADX 2-of-3 多數決邏輯
- **Fibonacci**：swing pivot 無重繪
- **Supertrend**：遞迴帶鎖定邏輯、方向翻轉
- **Donchian**：因果通道（shift(1) 前窗）
- **CVD / taker_buy_ratio**：訂單流因果

---

## Walk-Forward 驗證工具

| 腳本 | 用途 |
|------|------|
| `research_matrix.py` | 全策略 × 全時框 OOS 矩陣，從公開 mainnet 行情抓資料，無需金鑰 |
| `research_structure.py` | A/B 對比：純 TA vs TA + 訂單流過濾，量化過濾器貢獻 |
| `run_optimize.py` | 單策略網格掃描 + walk-forward（支援 supertrend / donchian / of_momentum）|

---

## 專案結構

```
.
├── README.md
├── QUICKSTART.md
├── config.py                       # 全域設定
├── Dockerfile                      # Railway worker 映像
├── railway.json                    # Railway 部署設定（startCommand / healthcheckPath）
├── research_matrix.py              # OOS 策略矩陣研究
├── research_structure.py           # 訂單流過濾 A/B 研究
├── core/
│   ├── market_analyst.py           # 市場分析師：K 線 + taker_base
│   ├── signal_engineer.py          # 信號工程師：14 項指標（含 Supertrend/Donchian/CVD）
│   ├── quant_researcher.py         # 量化研究員：7 個策略
│   ├── risk_officer.py             # 風控官：停損/停利/Chandelier/槓桿倉位
│   ├── futures_execution_engineer.py  # 執行工程師：合約測試網
│   ├── trade_journal.py            # 交易日誌
│   ├── bot_state.py                # 持倉持久化
│   └── paper_broker.py             # Paper 模擬成交
├── backtest/
│   ├── backtester.py               # 回測引擎
│   └── optimize.py                 # 參數掃描 + walk-forward
├── webapp/
│   ├── backend/
│   │   ├── main.py                 # FastAPI 路由（/api/live /api/live2 /api/whales /api/hl-leaderboard）
│   │   └── service.py              # 業務邏輯（Railway 狀態讀取、Hyperliquid 排行榜、Binance 籌碼）
│   └── frontend/
│       └── src/
│           ├── components/
│           │   ├── Live.jsx         # 即時監控（含 ExperimentStrip 對照實驗面板）
│           │   ├── TechPanel.jsx    # 技術指標可視化（Supertrend/Donchian/RSI/ATR）
│           │   ├── Whales.jsx       # 大戶籌碼（Binance + Hyperliquid）
│           │   ├── Backtest.jsx
│           │   ├── Optimize.jsx
│           │   └── Journal.jsx
│           ├── api.js               # API client
│           └── styles.css           # HUD 科幻主題
├── tests/                          # pytest 套件（259 個測試）
├── run_live_futures.py             # 合約測試網主程式（Railway 部署）
├── run_paper.py                    # Paper 模擬主程式
├── run_backtest.py                 # 回測主程式
└── run_optimize.py                 # 優化主程式
```

---

## 三個務必記住的現實

1. **回測漂亮 ≠ 實盤賺錢。** 最常見死法是過度擬合——參數調到剛好貼合歷史，換一段資料就破功。
2. **樣本要夠多。** 認真評估要跨牛熊、上千筆交易。
3. **先模擬盤跑久一點。** 即使虛擬資金，也要觀察數天～數週，確認停損、熔斷都正常。

> 本專案為工程與學習用途，不構成任何投資建議。
