# 丹丹交易團隊 — 合約測試網量化交易機器人

> ⚠️ **全程使用虛擬資金，不會花到一毛真錢。** 這是工程／學習範本，**不是投資建議**。任何績效數字都不構成獲利保證。

---

## 硬性規則

1. **只連測試網。** 下單/持倉/餘額都在 Binance Futures Testnet（`testnet.binancefuture.com`），永遠 `testnet=True`。
2. **不做一鍵切換正式網的捷徑。** 切實盤風險自負，不在本專案範圍。
3. **訊號吃主網、成交在測試網。** 評估訊號/指標用幣安主網公開合約 K 線（測試網小幣行情失真），但實際下單執行 100% 留在測試網——兩者互不影響下單安全性。
4. **回測要誠實。** 已收完的 K 線才下信號、含真實成本（手續費+滑點+資金費率+成交延遲）、bootstrap 信賴下界為正才算「顯著」，不偷看未來。
5. **金鑰不入 chat。** 所有金鑰只填 `.env` 或 Railway 環境變數，程式透過 `load_dotenv()` 讀取，絕不硬寫、打印或用會外洩金鑰內容的指令讀出。

---

## 現況架構（2026-07-06）

```
Railway 專案 dandan-futures-bot
├── dandan-futures-bot   單一進程跑 9 台 bot（b1~b9）
│                        run_multi_futures.py 監督器管理，個別崩潰隔離+自動重啟
├── dandan-dashboard     FastAPI + 前端，休眠省錢（deploy.sleepApplication）
└── GitHub Pages         公開儀表板，前端直連 bot 容器 API（不經 dashboard）

Postgres（bot + dashboard 共用，交易紀錄／跨 bot 曝險都寫這）
```

單一容器內每台 bot 是獨立 daemon 監督執行緒（崩潰隔離）、獨立 state 檔
（`bot_state_<id>.json`），命名空間路由對外：

| 端點 | 說明 |
|---|---|
| `/health` | 健康檢查（Railway healthcheck） |
| `/<id>/state` | 該台目前持倉/策略/淨值 |
| `/<id>/trades` | 該台近期成交 |
| `/<id>/close` | 手動平倉（需 `X-Close-Token`） |
| `/bots` | 全部 bot 清單（id/symbol/strategy/interval） |
| `/ma6`、`/klines` | 圖表資料（主網公開 K 線，30s 快取+429/418退避） |
| `/state`、`/trades`、`/close`（無 id）| 向後相容，導向第一台 bot |

本機開發：`.venv/bin/python -m uvicorn webapp.backend.main:app --port 8000`
+ 前端 `npm run dev`（vite :5173）。用 **uv 建的 `.venv`**（系統 Python 3.9 太舊）。

---

## 現行 9 台 bot 配置

| Bot | 標的 | 策略 | 週期 | 槓桿 | budget | 備註 |
|---|---|---|---|---|---|---|
| b1 | SUIUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b2 | BTCUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b3 | ETHUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b4 | ARBUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b5 | XRPUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b6 | DOGEUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子（LOO 顯示拖累最大，觀察中） |
| b7 | ADAUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b8 | DOTUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b9 | LINKUSDT | `ma_convergence_pullback` | 4h | 3x | 50 | 純觀察倉，**未證明有 edge** |

b1-b8 是**唯一驗證有統計顯著 edge 的配置**：`smc_structure`/4h/8幣籃子池化 +
`tp_R_mult=3.0`（rr3 出場），3 年資料驗證信賴下界 +1.77。b9 是雙均線系統的觀察倉，
小預算，目的是驗證系統而非獲利。全部 9 台的策略/風控參數走 `BOTS_CONFIG` 環境變數
（JSON 陣列，Railway 上設定，逐台覆蓋 Config 出場/風控欄位白名單）。

---

## 22 個策略（`core/quant_researcher.py`）

線上實際使用的只有 `smc_structure`（b1-b8）與 `ma_convergence_pullback`（b9），
其餘為研究過程中驗證、多數**沒有找到統計顯著 edge** 而未採用的策略，保留供回測比較：

| 分類 | 策略 |
|---|---|
| 現行冠軍 | `smc_structure`（Smart Money Concept 結構，BOS+FVG） |
| 觀察中 | `ma_convergence_pullback`（六線 MA/EMA 密集發散+首次回踩） |
| 趨勢跟蹤 | `ema_cross`、`supertrend`、`trend_pullback`、`vol_momentum` |
| 通道突破 | `donchian`、`fib_channel`、`chart_pattern_breakout` |
| 均值回歸 | `zscore_revert`、`zscore_ls`、`fib_retracement`、`fib_ema`、`vwap_band_reversion`、`regression_channel` |
| 動量/擺盪 | `heikin_ashi_momo`、`macd_scalp`、`bollinger_squeeze`、`rsi2_connors`、`of_momentum` |
| 消融實驗 | `ema_fib_vol` |
| 元策略 | `consensus`（多策略投票，`min_agree`/`min_agree_range` 門檻） |

---

## 核心研究方法論（任何新策略/新特徵都要照做）

1. **真實資料**：幣安 USDT 本位合約公開 K 線 API，不用現貨、不用合成資料做結論性驗證。
2. **真實成本**：`fee_rate=0.0005`、`slippage=0.0002`、`fill_lag=1`（訊號當根收盤才成交，下一根 open 才進場）、`funding_rate_per_8h=0.0001`。
3. **統計顯著性**：不看平均值，用 bootstrap 重抽樣，**信賴下界 > 0 才算「顯著正 edge」**。
4. **防過擬合**：調參數用切半驗證——新設定必須**兩半都不輸 baseline** 才算穩健。
5. **池化**：多幣種合併才有統計力，單幣樣本太小。
6. **因果性**：所有指標都不能用到未來資料（因果重算驗證，無 look-ahead）。

完整方法論 + 逐條研究記錄在 **`docs/strategy_research_log.md`**，可重現回測腳本在
**`research/scratchpad/`**（含 K 線快取，首次執行自動抓取）。

---

## 累積研究結論（別重踩）

**已驗證有效、線上使用：** `smc_structure`/4h/8幣籃子/rr3 出場（信賴下界 +1.77，3 年資料驗證，樣本翻 3 倍不降反升）。

**全部測過、沒找到 edge 的方向**（除非有新的、真的不同的角度，否則不用重測）：
- 17 個規則策略在 15m/1h 幾乎全滅（手續費+雜訊吃死）
- 換 1h 週期會摧毀 smc_structure 和雙均線系統的表現
- 擴大籃子（加 LINK/AVAX/NEAR/OP/INJ/APT/LTC）全部降低信賴下界
- 技術指標 ML 過濾層（AUC 0.557，等於瞎猜）、資金費率 ML 特徵（shuffled CV 假警報，時間切分後翻盤）
- 雙均線系統五個獨立優化方向（出場/進場/多週期共振/合併訊號類型）3年資料下界最好只到 -1.65，從未轉正
- 古典圖表形態突破、迴歸通道均值回歸——兩個全新策略類別同樣沒有 edge

多週期共振（HTF 過濾）教訓：效果取決於訊號類型。`smc_structure` 加 HTF 有害（BOS 靠提早抓反轉，等日線轉向就錯過肥段）；`ma_convergence_pullback` 加 HTF 在 3 年資料下不再優於 baseline。

---

## 安裝

```bash
cd /Users/adem/量化機器
uv venv .venv --python 3.12
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
npm --prefix webapp/frontend install
```

雲端部署（Railway，`requirements-bot.txt` 最小依賴）用 `Dockerfile`，本地開發/回測用完整 `requirements.txt`。

---

## .env 設定（本機開發用，Railway 用面板 Variables）

```
BINANCE_FUTURES_TESTNET_API_KEY=你的64字元key
BINANCE_FUTURES_TESTNET_API_SECRET=你的64字元secret

# 本機前端讀取 Railway 上 9 台 bot 即時狀態
RAILWAY_BOT_URL=https://dandan-futures-bot-production.up.railway.app
```

參考 `.env.example`（部分內容對應舊架構，以此 README 為準）。

---

## 使用方式

```bash
# 純回測（不需金鑰，抓公開行情）
python run_backtest.py smc_structure --plot --report
python run_backtest.py ma_convergence_pullback --plot --report

# 策略研究腳本（可重現，見 research/scratchpad/）
python research_matrix.py        # 策略 × 時框 OOS 矩陣
python research_structure.py     # 純 TA vs TA + 訂單流過濾 A/B 對比

# 參數掃描 + walk-forward
python run_optimize.py smc_structure

# Paper 模擬（真實行情 + 本機模擬成交，免金鑰）
python run_paper.py --interval 4h --poll 30 --strategy smc_structure

# 合約測試網單台實盤（需合約測試網金鑰）
python run_live_futures.py --strategy smc_structure --symbol BTCUSDT --interval 4h --leverage 3 --budget 150

# 多台合一（Railway 正式部署走這個，需 BOTS_CONFIG 環境變數 JSON 陣列）
python run_multi_futures.py

# 測試
.venv/bin/python -m pytest -q            # 後端 1030+ 個測試
npx vitest run                           # 前端 26 個測試（webapp/frontend/）
```

---

## 儀表板

```bash
./dashboard.sh start    # 一鍵啟動後端 + 前端
                         # → http://localhost:5173
./dashboard.sh status
./dashboard.sh stop
```

分頁：
- **即時監控** — 動態 N 台 bot 卡片（>4 台自動收合摘要）、K 線圖（六均線密集/發散標記）
- **決策流程** — SOP 6 角色決策鏈（市場分析師→信號工程師→量化研究員→風控官→執行工程師）
- **回測** — 權益曲線、績效卡片、交易明細
- **參數最佳化** — walk-forward 熱圖
- **交易日誌** — 讀共用 Postgres

公開版（GitHub Pages）只保留 即時監控/K線/決策流程(簡化)/交易日誌 四分頁（回測/最佳化需要本機 vectorbt/optuna 後端）。

---

## 架構：6 個角色 = 6 個模組

| 角色 | 模組 | 職責 |
|------|------|------|
| 市場分析師 | `core/market_analyst.py` | 抓 K 線、taker_base（主動買量）、暴量異常偵測 |
| 信號工程師 | `core/signal_engineer.py` | EMA/RSI/ATR/z-score/ADX/ER/CHOP/Regime/Fibonacci/Supertrend/Donchian/CVD/HTF趨勢 |
| 量化研究員 | `core/quant_researcher.py` | 22 個策略邏輯、Regime 過濾、訂單流結構閘門 |
| 風控官 | `core/risk_officer.py` | ATR 動態停損、R-倍數停利、Chandelier 追蹤停損、單日/組合熔斷、Kelly 倉位 |
| 執行工程師 | `core/futures_execution_engineer.py` | 合約測試網下單、精度修正、清算距離守衛 |
| 回測工程師 | `backtest/{backtester,optimize,tournament,vbt_optimize}.py` | 回測引擎、walk-forward、bootstrap 信賴下界錦標賽 |
| 交易日誌 | `core/trade_journal.py` | Postgres/SQLite + CSV 留底 |
| 狀態還原 | `core/bot_state.py` | 持久化持倉，重啟以 journal 推斷 fallback（防重複進場） |
| 跨 bot 風控 | `core/portfolio_guard.py`、`core/circuit_breaker.py`、`core/directional_guard.py` | 集中度上限、連虧暫停、方向感知護欄 |
| 圖表資料 | `core/chart_data.py` | K 線/六均線 overlay，供 `/ma6`、`/klines` 端點 |
| 即時狀態 | `core/live_status.py` | 單台 bot state + 交易統計 enrichment（輕量，供雲端容器吐資料） |

---

## 專案結構

```
.
├── README.md / QUICKSTART.md / PROJECT_HANDOFF.md   # PROJECT_HANDOFF.md 為現況權威文件
├── docs/strategy_research_log.md                    # 逐條策略研究記錄
├── config.py                       # 全域設定
├── Dockerfile                      # Railway bot 映像（requirements-bot.txt）
├── railway.json                    # Railway 部署設定
├── run_multi_futures.py            # 多 bot 監督器（BOTS_CONFIG 驅動，正式部署入口）
├── run_live_futures.py             # 單台合約測試網交易邏輯（被 run_multi_futures import 復用）
├── run_backtest.py / run_paper.py / run_optimize.py
├── research_matrix.py / research_structure.py
├── research/
│   ├── scratchpad/                 # 可重現研究腳本（K線/資金費率 fetch+cache）
│   ├── klines_cache/ / funding_cache/ / live_audit/
├── core/                           # 見上方「6 個角色」表
├── backtest/
├── webapp/
│   ├── backend/                    # FastAPI（main.py / service.py）
│   └── frontend/src/components/    # Live/DualMa/Chart/Explain/LiveDecisions/Backtest/Optimize/Journal/TechPanel
├── tests/                          # pytest 套件
└── .github/workflows/              # ci.yml / deploy-dashboard.yml / deploy-pages.yml
```

---

## 三個務必記住的現實

1. **回測漂亮 ≠ 實盤賺錢。** 最常見死法是過度擬合——參數調到剛好貼合歷史，換一段資料就破功。
2. **樣本要夠多。** 認真評估要跨牛熊、上千筆交易，單一策略單一市場樣本太小，edge 常常在「市場×週期」而非單一策略本身。
3. **系統誠實比策略聰明更重要。** 2026-07 的實盤稽核發現虧損主因不是策略沒 edge，而是測試網幽靈行情+部署重複進場等系統性 bug；修好系統誠實度，回測驗證出的 edge 才有意義去檢驗。

> 本專案為工程與學習用途，不構成任何投資建議。給接手對話：先讀 `PROJECT_HANDOFF.md` 了解安全紅線與待辦，再讀 `docs/strategy_research_log.md` 了解完整研究脈絡。
