# 丹丹交易團隊 — 合約測試網量化交易機器人

> ⚠️ **全程使用虛擬資金，不會花到一毛真錢。** 這是工程／學習範本，**不是投資建議**。任何績效數字都不構成獲利保證。

---

## 硬性規則

1. **只連測試網。** `execution_engineer.py` 永遠 `testnet=True`，合約指向 `https://testnet.binancefuture.com`。
2. **不做一鍵切換正式網的捷徑。** 切實盤風險自負，不在本專案範圍。
3. **回測要誠實。** 用「已收完」的 K 線下信號、含手續費、不偷看未來（no look-ahead）。

---

## 架構：6 個角色 = 6 個模組

| 角色 | 模組 | 職責 |
|------|------|------|
| 市場分析師 | `core/market_analyst.py` | 抓 K 線／長歷史／快取、暴量異常偵測 |
| 信號工程師 | `core/signal_engineer.py` | EMA / RSI / ATR / z-score / ADX / ER / CHOP / Regime / Fibonacci |
| 量化研究員 | `core/quant_researcher.py` | 策略邏輯、Regime 過濾、產生目標倉位信號 |
| 風控官 | `core/risk_officer.py` | ATR 動態停損、R-倍數停利、Chandelier 追蹤停損、單日熔斷 |
| 執行工程師 | `core/futures_execution_engineer.py` | 合約測試網下單、精度修正 |
| 回測工程師 | `backtest/backtester.py` | 歷史回測（多/空、滑點、Chandelier 追蹤）、績效指標 |
| （工具）交易日誌 | `core/trade_journal.py` | SQLite + CSV 留底 |
| （工具）狀態還原 | `core/bot_state.py` | 持久化持倉，崩潰後還原（以交易所餘額為準） |

---

## 四個策略

| 策略名 | 適用市況 | 做空 | 說明 |
|--------|---------|------|------|
| `ema_cross` | 趨勢 | 否 | EMA 快慢線交叉，僅做多 |
| `zscore_revert` | 盤整 | 否 | z-score 均值回歸，僅做多 |
| `zscore_ls` | 盤整 | ✅ | z-score 均值回歸，多空雙向 |
| `fib_retracement` | 盤整 | ✅ | Fibonacci 回調 + 擺動高低點確認，多空雙向 |

每個策略都有 **Regime 過濾**（ER + CHOP + ADX 2-of-3 多數決），只在適合的市況開倉。

---

## 技術指標（8 項 TA 優化）

| 指標 | 用途 |
|------|------|
| ATR 動態停損 | `entry ∓ atr_mult_sl × ATR`，比固定百分比更貼近波動 |
| R-倍數停利 | `tp_R_mult × 停損距離`，保持正期望值 |
| Chandelier 追蹤停損 | 只向有利方向移動，鎖住利潤 |
| ADX | 趨勢強度判斷（>25 為強趨勢） |
| Efficiency Ratio (ER) | 方向效率，>0.618 代表趨勢，<0.382 代表盤整 |
| Choppiness Index (CHOP) | >61.8 盤整、<38.2 趨勢 |
| Regime | ER + CHOP + ADX 2-of-3 投票 + debounce，決定市況 |
| Fibonacci 回調 + 擺動高低點 | 因果計算擺動樞軸（無重繪），找 38.2%/61.8% 回調進場 |

---

## 部署架構

```
Railway（雲端 24/7）          本機 Mac
─────────────────────         ──────────────────────────
run_live_futures.py           webapp/backend  ← FastAPI
  ↓ Binance Futures Testnet   webapp/frontend ← Vite/React
  ↓ 寫 bot_state_futures.json  dashboard.sh（含 paper bot）
```

> **前端連到 Railway？** 目前前端讀本機的 `bot_state_futures.json`，Railway bot 在雲端獨立的磁碟寫。
> 若要前端顯示 Railway bot 狀態，需另將 FastAPI backend 部署到 Railway（進行中）。

---

## 安裝

```bash
cd /Users/adem/量化機器
uv venv .venv --python 3.12
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
npm --prefix webapp/frontend install
```

---

## 使用方式

```bash
# 純回測（不需金鑰）
python run_backtest.py fib_retracement --plot --report
python run_backtest.py zscore_ls --plot --report

# 參數掃描 + walk-forward
python run_optimize.py fib_retracement --synthetic --plot
python run_optimize.py ema_cross --synthetic --include-risk

# Paper 模擬（真實行情 + 本機模擬成交，免金鑰）
python run_paper.py --interval 1m --poll 15 --strategy fib_retracement

# 合約測試網（需合約測試網金鑰，支援做空）
python run_live_futures.py --strategy fib_retracement --interval 1m --poll 15 --budget 100 --leverage 10

# 測試
.venv/bin/python -m pytest -q    # 218 個單元測試
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
- **即時監控** — 模式徽章（合約測試網 / Paper 模擬）、目前部位、SOP 6 角色決策、Regime 指標
- **回測** — 權益曲線、績效卡片、交易明細
- **決策流程** — 每一關的詳細決策
- **參數最佳化** — 熱圖 + walk-forward
- **交易日誌** — 讀 `trades.db`

---

## Railway 雲端部署（合約 bot 24/7）

```bash
# 第一次部署（需在 Railway 面板設環境變數）
railway up

# Railway 環境變數（在 railway.app → Variables 設定）
BINANCE_FUTURES_TESTNET_API_KEY=你的64字元key
BINANCE_FUTURES_TESTNET_API_SECRET=你的64字元secret
```

`railway.json` 設定 worker 模式（非 web）、失敗自動重啟（最多 10 次）。
`.railwayignore` 排除 `.venv`（195MB）+ `node_modules`（65MB）等大型目錄。

---

## 風控參數（`config.py`）

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `atr_mult_sl` | 2.0 | ATR 停損倍數 |
| `tp_R_mult` | 2.0 | R-倍數停利（停損距離的倍數）|
| `chand_mult` | 3.0 | Chandelier 追蹤停損倍數 |
| `leverage` | 10 | 合約槓桿（`--leverage` 指定）|
| `budget` | 100 | 每筆最大 USDT 保證金（`--budget` 指定）|

---

## 重啟還原

`bot_state_futures.json` 每根 K 線原子寫入：持倉方向、入場價、停損、停利、Chandelier 高低點。
重啟時以「交易所實際餘額」校正（餘額是真相，狀態檔補 entry/SL/TP），避免崩潰後重複下單。

---

## 測試套件

```bash
.venv/bin/python -m pytest -q      # 218 個測試（含前綴不變性因果驗證、Chandelier 回歸）
```

關鍵測試：
- 前綴不變性（prefix-invariance）：確保所有指標計算因果正確、不偷看未來
- Chandelier 回歸：確保 restore() 後 peak/trough 正確還原，trailing stop 不會損壞
- Regime：ER / CHOP / ADX 2-of-3 多數決邏輯
- Fibonacci：擺動高低點確認、不重繪

---

## 專案結構

```
.
├── README.md
├── QUICKSTART.md              # 快速上手
├── requirements.txt
├── requirements-web.txt
├── requirements-dev.txt
├── requirements-bot.txt       # Railway 部署最小依賴
├── config.py                  # 全域設定（含 ATR/R-mult/Chandelier 參數）
├── Dockerfile                 # Railway worker 映像
├── railway.json               # Railway 部署設定
├── .railwayignore             # Railway 上傳排除清單
├── core/
│   ├── market_analyst.py      # 市場分析師：資料來源
│   ├── signal_engineer.py     # 信號工程師：EMA/RSI/ATR/ADX/ER/CHOP/Regime/Fib
│   ├── quant_researcher.py    # 量化研究員：4 個策略 + Regime 過濾
│   ├── risk_officer.py        # 風控官：ATR 停損、R-倍數停利、Chandelier 追蹤
│   ├── execution_engineer.py  # 執行工程師：現貨測試網
│   ├── futures_execution_engineer.py  # 執行工程師：合約測試網（支援空單）
│   ├── trade_journal.py       # 交易日誌：SQLite + CSV
│   ├── plotting.py            # 視覺化
│   ├── report.py              # HTML 報表
│   ├── bot_state.py           # 持倉持久化
│   └── paper_broker.py        # Paper 模式模擬成交
├── backtest/
│   ├── backtester.py          # 回測引擎（含 Chandelier 追蹤）
│   └── optimize.py            # 參數掃描 + walk-forward
├── webapp/
│   ├── backend/               # FastAPI 後端
│   └── frontend/              # React/Vite 前端
├── tests/                     # pytest 套件（218 個測試）
├── run_backtest.py
├── run_paper.py
├── run_live.py
├── run_live_ws.py
├── run_live_futures.py        # 合約測試網主程式（Railway 部署）
└── run_optimize.py
```

---

## 三個務必記住的現實

1. **回測漂亮 ≠ 實盤賺錢。** 最常見死法是過度擬合——參數調到剛好貼合歷史，換一段資料就破功。
2. **樣本要夠多。** 認真評估要跨牛熊、上千筆交易。
3. **先模擬盤跑久一點。** 即使虛擬資金，也要觀察數天～數週，確認停損、熔斷都正常。

> 本專案為工程與學習用途，不構成任何投資建議。
