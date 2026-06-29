# 丹丹交易團隊 — 專案交接文件

> 給其他對話 / AI 接手用的完整背景。日期：2026-06-26。
> 專案路徑：`/Users/adem/量化機器`，GitHub：`AdemCZW/dandan-futures-bot`。

---

## 0. 最重要 — 安全紅線（絕不可違反）

- **全部都是幣安「合約測試網」(Binance Futures Testnet)，虛擬資金，不碰真錢。**
- `run_live_futures.py` 寫死 `testnet=True`，指向 `testnet.binancefuture.com`。
- 主網只用來抓「公開價格資料」（無金鑰、無下單）。
- API 金鑰只在 `.env` / Railway 環境變數，**永遠不顯示在對話、不打包進映像**。
- **AI 不可代替使用者下單 / 平倉 / 轉帳**（交易執行是使用者自己的事）。要停 bot 可以用 `railway down`，但「平掉某個部位」屬於下單，不做。

---

## 1. 系統架構

```
Railway 專案 dandan-futures-bot（已用 railway CLI 登入 ademczw's Projects）
├── Bot1  dandan-futures-bot   → https://dandan-futures-bot-production.up.railway.app
├── Bot2  dandan-shortterm     → https://dandan-shortterm-production.up.railway.app   ← 賺錢機，動它前要先問使用者
├── Bot3  pacific-radiance     → https://pacific-radiance-production-8a19.up.railway.app
├── 儀表板 dandan-dashboard     → https://dandan-dashboard-production.up.railway.app
└── Postgres（四個 service 共用，交易紀錄都寫這）
```

- **bot** = `run_live_futures.py`，每台一個 Railway service，靠環境變數決定策略/標的/週期。
- **儀表板** = FastAPI 後端（`webapp/backend`）+ 打包好的 React 前端（`webapp/frontend`），同一個 service，前端同源呼叫 `/api`（無 CORS）。
- 每台 bot 開 HTTP 端點：`/health`、`/state`（持倉/策略/淨值）、`/trades?limit=N&mode=M`。
- 本機開發：`uv run uvicorn webapp.backend.main:app --port 8000` + 前端 `npm run dev`（vite :5173，proxy /api → :8000）。Python 用 **uv**（系統 Python 3.9 太舊，brew python 的 pyexpat 壞，一律用 uv venv，Python 3.12）。

---

## 2. 四台 bot 目前配置（2026-06-27 更新）

| Bot | service | 策略 | 標的 | 週期 | 槓桿 | 模式 / 備註 |
|-----|---------|------|------|------|------|-------------|
| **Bot1** | dandan-futures-bot | `fib_ema` | SOLUSDT | 15m | 10x | 原 fib_channel/BTC 幾乎不交易（regime 閘門太窄），改 fib_ema/SOL |
| **Bot2** | dandan-shortterm | `fib_channel` | SOLUSDT | 15m | 10x | `BOT_PARAMS={"mode":"reversion"}`（均值回歸；唯一長期在賺的一台） |
| **Bot3** | pacific-radiance | `trend_pullback` | ETHUSDT | 1h | 預設3x | 原 smc 一直虧已換掉 |
| **Bot4** | dandan-longterm | `trend_pullback` | SOLUSDT | 1h | 5x | 本 session 新增的長線台（dashboard 第四張卡，URL: dandan-longterm-production.up.railway.app） |

環境變數：`BOT_STRATEGY` / `BOT_SYMBOL` / `BOT_INTERVAL` / `BOT_LEV` / `BOT_BUDGET` / `BOT_POLL` / `PORTFOLIO_MAX_NOTIONAL`(預設15000) / `BOT_PARAMS`(JSON，覆蓋策略參數)。

**⚠️ bot 身分隔離 = `(strategy, symbol)`（不含週期）**。兩台想同策略+同標的並存（如 fib_ema/SOL 跑 15m 和 1h）會撞鍵 → 持倉互蓋、交易紀錄互相顯示。現有四台 (策略,標的) 組合各不相同才沒撞。新增 bot 前務必確認不重複；真要同策略同標的不同週期，得把隔離鍵擴成 `(strategy, symbol, interval)`（portfolio_guard + trade_journal + run_live + /trades 都要改）。

**儀表板顯示 N 台**：後端 `RAILWAY_BOT_URL` / `_2` / `_3` / `_4` → `/api/live`～`/api/live4`；前端 `Live.jsx` 4 張卡（第 4 張用 `e4?.configured` 條件渲染）。加第 5 台得照樣補一輪（service.py 變數 + main.py 端點 + api.js + Live.jsx 卡 + BOT_COLORS 顏色 + dashboard env var）。

---

## 3. 策略清單（`core/quant_researcher.py` 的 STRATEGIES，共 15 個）

常用的幾個：
- **`fib_channel`** — 斜向費波那契通道，雙向。`mode=trend`（回踩原點順勢進）或 `mode=reversion`（通道頂做空/底做多，均值回歸）。**reversion 只在 regime=range 進場、trend 只在 regime=trend 進場**（這個 regime 閘門是本 session 修的 bug：原本 reversion 被限制只在趨勢盤進場 → 頂著漲勢接刀虧損）。
- **`trend_pullback`**（本 session 新增）— 使用者設計的多指標短線打法：
  - 200EMA 定主方向（上只多/下只空，過濾逆勢假突破）
  - 進場 = EMA20>50 動能 + RSI(14) 回踩區 [40,60]（不追極端）+ KD 黃金/死亡交叉（觸發鍵），四條件 AND
  - 出場 = 趨勢翻轉（價跨 200EMA）或動能翻轉（EMA20/50 反向）
  - 回測：在 ETH/BTC/SOL 各配置都優於 smc/fib；**ETH 1h 是唯一正報酬（+2.0%、PF1.28、Sharpe0.65）**
- `smc_structure` — Smart Money（BOS 突破 + EMA 過濾）。在 ETH 表現差，已從 Bot3 換掉。
- 其他：ema_cross / supertrend / donchian / rsi2_connors / bb_squeeze_breakout / macd_scalp / vwap_band_reversion / heikin_ashi_momo / zscore 等。

**新指標**：`core/signal_engineer.py` 的 `stochastic()`（KD，本 session 新增）。其餘有 ema/rsi/atr/adx/bollinger/macd/supertrend/regime。

---

## 4. 風控（每台 bot 共用，`core/risk_officer.py`）

- **進場停損/停利**：ATR 動態。SL = entry ∓ `atr_mult_sl`(2.0)×ATR；TP 距離 = `tp_R_mult`(2.0)×SL距離（恆定 R）。ATR 不可用時退回固定 % (SL 2% / TP 4%)。
- **Scale-out（部分獲利了結 + 保本）**：浮盈達 **0.5R** 時平一半倉，剩餘半倉 **SL 移到進場成本（保本）**。
- **Chandelier 移動停利**：持倉中 SL 單向跟著走（多單只升、空單只降），= 最佳價 ∓ `chand_mult`(3.0)×ATR。價格從最佳點回測 3×ATR 就帶剩餘利潤出場。
- **Kelly Criterion** 動態倉位、跨 bot 同向暴露上限（`core/portfolio_guard.py`）、單日熔斷。
- **方向感知通道護欄**（`core/directional_guard.py`，本 session 新增，**預設停用**）：連續 `DCG_MAX_LOSSES`(預設3) 筆「同方向」平倉虧損 → 暫停『該方向』新進場，直到通道方向(`fib_ch_dir`)翻轉或冷卻 `DCG_COOLDOWN_BARS`(預設8) 根 K 棒。專治 fib_channel reversion「連虧卻不換方向、一直逆勢接刀」。只擋進場、不影響出場；被擋方向贏一筆即解封。env `DCG_ENABLED=1` 開啟（目前只在 **Bot2** 開）。與 CircuitBreaker 的差異：CB 不分方向、整台暫停；本護欄分方向、只擋虧的那邊（保留另一方向繼續做）。狀態隨 `BotState.dcg_state` 持久化，重啟不放水。

⚠️ **SL/TP 預設是「軟停損」**——bot 每輪輪詢比價後用市價單平倉。bot 當機/熔斷/網路斷那段時間，軟停損不會動。

- **交易所掛單式硬停損**（`EXCHANGE_STOP_ENABLED`，本 session 新增，**預設關，逐台 env 開**）：進場後額外在交易所掛 `STOP_MARKET`@sl + `TAKE_PROFIT_MARKET`@tp（皆 `closePosition='true'`、`workingType=CONTRACT_PRICE` 對齊軟停損），bot 死了交易所仍會守。`self.sl` 被吊燈/scale-out 移動 → cancel/replace 換單（TP 進場後固定不動）；平倉先撤殘單再市價平；重啟 `restore` 撤殘單並依還原 SL/TP 重掛。**對帳**：每輪先讀 `position_amt`，本地以為持倉但交易所實際無倉（STOP/TP 已觸發）→ `_reconcile_exit` 補記平倉（依現價 vs tp/sl 判 `exit_tp`/`exit_trail`/`exit_breakeven`/`exit_sl`）+ 清狀態，**不重複下市價單**。軟停損保留為後備。`stop_oid`/`tp_oid` 隨 `BotState` 持久化。實作：`core/futures_execution_engineer.py`（`place_stop`/`place_take_profit`/`cancel_order`/`cancel_all_stops`/`open_orders`）+ `run_live_futures.py`（`_place_protective`/`_cancel_protective`/`_sync_protective_stop`/`_reconcile_exit`）。**旗標關時全部 no-op**，四台 bot 現行行為不變；client 仍 `testnet=True`，掛的是測試網虛擬單。

**結單原因細分**（本 session，`FuturesLiveTrader._classify_exit`）：SL/TP 觸發的平倉不再都記成 `exit_sltp`，改細分成 `exit_tp`(觸及停利目標) / `exit_trail`(停損已移到成本之上→吊燈移動停利鎖利) / `exit_breakeven`(回成本價→保本) / `exit_sl`(跌破成本→真停損)；訊號平倉仍記 `exit_signal`、部分了結 `scale_out`。**純標籤分類、不改變平倉行為**。前端 `lib/trades.js::exitReason()` 把這些（含舊 `exit_sltp` 用損益推斷的後備）映射成中文標籤 + hover 詳細說明；Journal/Explain 分頁也同步。所有新 side 字串都以 `exit` 開頭 → `pairTrades`/`trade_markers`/已實現統計皆相容。

---

## 5. 儀表板前端（`webapp/frontend`，React + Vite + lightweight-charts v5）

分頁：即時監控 / K線圖表 / 大戶籌碼 / 帶單追蹤 / 回測 / 決策流程 / 參數最佳化 / 交易日誌。

本 session 對「即時監控」做的：
- 三台 bot 卡片，每張內嵌 **迷你 K 線圖**（`MiniChart.jsx`）：蠟燭 + 策略對應技術線（fib_channel 疊 7 條費波那契通道線、smc/trend_pullback 疊 EMA）+ **進場/SL/TP 價格線** + **進出場標記**（買▲/空▼/平●，吸附最近 K 棒）。
- 時間顯示改 **台灣時間 (UTC+8)**。
- **桌面兩欄並排**：bot 卡片網格 `.bots-grid`（`styles.css`）桌面固定 2 欄、手機 ≤640px 降 1 欄（取代原 `auto-fit`，避免大螢幕跑 3–4 欄、中螢幕反而 1 欄的不穩）。
- **進階統計區**（本 session）：每張卡顯示 **最大回撤% / 每筆夏普 / 多單拆分 / 空單拆分**（`MiniStat` 2×2 格，hover 有說明）。數字來自後端 `service.trade_stats()`（配對全量 2000 筆算，與勝率同口徑）：回撤以 $5000 基底權益曲線、夏普=每筆 ROI 的 mean/std（非年化）、多空各自勝率+損益。前端 `Live.jsx` 直接顯示後端欄位（`max_drawdown_pct`/`sharpe`/`long_*`/`short_*`），純函式測試在 `tests/test_trade_stats.py`。

---

## 6. 部署機制（重要，容易踩坑）

- **git push 不會自動部署！** 這些 service 不是 GitHub 自動部署，`railway redeploy` 只重跑舊 image。
- 部署新 code 要：`railway up --service <name> --ci`（上傳本機工作目錄，讀 `.railwayignore`，Dockerfile builder）。
- **儀表板比較麻煩**：repo 的 `railway.json` 固定檔名、鎖死所有 service 用 `Dockerfile`（bot 用），且**優先於** `RAILWAY_DOCKERFILE_PATH` / `RAILWAY_CONFIG_FILE` 環境變數。儀表板要用 `Dockerfile.webapp`（多階段 node 建前端 + python serve），只能：
  ```
  cp railway.webapp.json railway.json      # 暫時換成 webapp config
  railway up --service dandan-dashboard --ci
  git checkout railway.json                # 還原成 bot config（重要！）
  ```
  （每個 deployment 各吃自己快照裡的 config，所以換完還原不影響別台。）
- `railway.json` 的 `startCommand` **不展開 `$PORT`**，故 webapp 不設 startCommand，改用 `Dockerfile.webapp` 的 shell 形式 CMD。
- 改 env var（`railway variables set/delete --service <name>`）會觸發重啟，但用的是上次 `railway up` 的 image。
- **儀表板自動部署**（本 session 新增，`.github/workflows/deploy-dashboard.yml`）：push 到 `main` 且異動 `webapp/**` / `Dockerfile.webapp` / `railway.webapp.json` 時，先跑 `pytest`（CI gate，`needs: test`）綠燈才部署。CI runner 內 `cp railway.webapp.json railway.json`（拋棄式 checkout 不需還原），`railway up --service dandan-dashboard --ci`，**只動儀表板、絕不碰四台 bot**。**一次性設定（使用者手動）**：Railway 專案建 Project Token → GitHub repo Settings→Secrets→Actions 加 `RAILWAY_TOKEN`。設好後 webapp 改動 push 即自動「測試→部署」。**bot 仍須各自手動 `railway up`**（此 workflow 不部署 bot）。`ci.yml` 與此 workflow 的 pytest 都 `--deselect` 掉既有已知無關失敗 `test_vbt_optimize ... TestVbtSharpe::test_returns_neg_inf_when_too_few_trades`（讓 CI 可綠、gate 才有意義）。

---

## 7. 關鍵教訓 / 已知限制（別重蹈覆轍）

1. **回測 ≠ 測試網實盤。** 回測用主網「合約」公開資料（**絕不可用現貨資料**，要用 `futures_historical_klines` / fapi.binance.com）。但測試網有自己一套價格訂單簿，跟主網不同 → 回測重現不了實盤訊號。**不能用對不上的回測去否定實盤獲利。** 回測只能做「相對篩選」（哪個策略相對好），不能保證實盤。
2. **評估 bot 要看它自己的真實成交紀錄**（測試網實單），不是回測。
3. **樣本都還小**（十幾到幾十筆），任何「這策略會賺」的結論都要保守。唯一有正期望實盤證據的是 Bot2 的均值回歸（仍小樣本）。
4. **portfolio_guard** 本 session 修了兩個 bug：identity 改用 `(strategy, symbol)` 複合鍵（兩台同策略不同標的不再互相覆蓋）；且原本 `conn.execute()` 在 PG 上不存在（只有 cursor 有）→ guard 在 Railway 一直丟錯形同停用，已改用 cursor + fail-open。
5. **交易紀錄共用同一個 Postgres**，用 `(strategy, symbol)` 過濾，所以兩台同跑 fib_channel（BTC/SOL）不會互相污染顯示。

### 全面審查修復（本 session，多 agent 對抗式審查 → 確認 9 個影響四台現行行為的真實漏洞，全 TDD 修復）
6. **Kelly 之前完全失效（HIGH）**：`_kelly_pct` 用 `mode="exit"` 過濾，但 mode 欄永遠是 `live_futures_testnet`、平倉記在 `side` 欄 → 永遠 0 筆。已改用 `side` 前綴篩平倉 + 用 `self.cfg`（非 env）隔離。**⚠️ 修好後 Kelly 真的會作用**：正期望時把倉位縮向 Kelly 比例（永遠 ≤ budget），負期望/無訊號退回 budget（不會把 bot 關掉）。`risk_officer.position_size` 同步改成 `min(kelly, max_position_pct)`（之前是「取代」budget → 可達 1.5× 餘額）。
7. **`_write_sop` 每根 K 棒覆寫狀態檔、抹掉持久化欄位（HIGH）**：原本寫的 raw dict 漏掉 cb/dcg/scaled_out/entry_sl_dist/last_balance/stop_oid/tp_oid → 重啟後熔斷/護欄/scale-out 全歸零、且 `last_balance` 永遠 0 使**測試網重置偵測永久失效**。已改成以完整 `BotState` 為底寫檔（`{**asdict(bs), 顯示欄位}`）。
8. **熔斷暫停期間持倉裸奔（HIGH）**：`on_bar_close` 的 `is_paused()` 早退在 SL/TP 比價之前 → 暫停的 24h 內持倉無軟停損。已把「對帳 + 方向性停損停利」移到熔斷閘門之前；熔斷只擋新進場/加碼。
9. **PortfolioGuard 連線洩漏（MED）**：`_conn` 每次新開 psycopg2 連線、psycopg2 的 `with` 不關閉 → Railway Postgres 連線單調累積。已把 `_conn` 改成 `@contextmanager`（commit + `finally: close`）。
10. **scale-out 後未更新共用 DB 暴露（MED）**：平半倉後共用 DB 仍記全額 notional → 高估暴露、誤擋他台同向進場。已補 `upsert_position(減半量)`；scale-out 量也改用 `round_qty` floor 後值記帳（消除 sub-step 漂移）。
11. **測試網重置歸零持倉時未清共用 DB（MED）**：留幽靈 notional 占用他台暴露上限。已在重置分支補 `clear_position` + `_cancel_protective`。
12. **（暫緩，LOW）check_exposure 與 upsert_position 跨進程非原子（TOCTOU）**：兩台同向 bot 若在各自 upsert 前都讀到舊快照，同向暴露可短暫超 `PORTFOLIO_MAX_NOTIONAL`。僅模擬倉風控上限、無資金風險，需 PG advisory lock / `SELECT FOR UPDATE` 才根治，**尚未做**（已記錄為已知限制）。

---

## 8. 測試 / 驗證

- 測試在 `tests/`，用 `uv run pytest`。本 session 全套 **676 個通過**（含 `test_trade_stats.py` 13 + STOP 執行層/生命週期/修復 + 手動平倉 + 全面審查 9 漏洞回歸；前端 vitest 17）。
- **手動平倉鈕（結算）**：bot 端 `POST /close`（帶 `X-Close-Token`，未設 `CLOSE_TOKEN`→403 停用）寫 `close_request.flag`，主迴圈讀旗標在主執行緒 `manual_close()`（close-only，不暫停）。儀表板 `service.close_position` 持 `CLOSE_TOKEN` 轉發；前端持倉卡顯示「手動平倉」鈕 + 二次確認。**要啟用需在四台 bot + 儀表板都設 `CLOSE_TOKEN` 同一密鑰，且 bot 須重新部署含新 code。**
- **唯一已知失敗**：`tests/test_vbt_optimize.py::TestVbtSharpe::test_returns_neg_inf_when_too_few_trades`（既有問題，與本 session 改動無關，跑全套時 `--deselect` 掉它）。
- 開發守則：**TDD**（先寫失敗測試 → 看它失敗 → 最小實作通過）。本專案策略/指標都照這個流程。

---

## 9. 目前待觀察 / 可能的下一步

- **Bot1**（剛改 15m 順勢）：trend mode 在 15m 雜訊大，若一直被洗建議退回 1h/4h（4h 回測較穩）。
- **Bot3**（剛換 trend_pullback/ETH/1h）：觀察實盤是否如回測有正期望。
- **Bot2**：regime 修正後上漲趨勢不再做空，看是否減少虧損。
- **交易所硬停損已實作但預設關**：要逐台啟用，在該 bot 的 Railway 設 `EXCHANGE_STOP_ENABLED=1` 並重新 `railway up`（需先把含新 code 的 bot 重新部署）。建議先在 Bot1 開、觀察掛單/觸發/對帳正常再推廣；Bot2 賺錢機動它前先問使用者。
- **儀表板自動部署需使用者加 `RAILWAY_TOKEN` secret** 才會生效（見第 6 節）。
- 可選優化（使用者尚未決定）：Telegram/Line 推播、四台合計淨值、固定 % 獲利回吐保護參數。
