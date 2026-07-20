# ai_desk Phase 2：核准後執行層 設計文件

**日期**: 2026-07-20
**範圍**: 讓「已核准」的提案真的在 Binance Futures **Testnet** 掛出限價單 + 停損 + 停利，並追蹤到平倉。
**前置狀態**: Phase 1 完成（分支 `feat/ai-desk-phase1`，四角色辯論→提案→風控→人工核准）。帳戶已清空（0 持倉 0 掛單，USDT 4448.22）。

---

## 為什麼這一層要特別小心

這是整套系統**唯一會真的送出委託**的地方。前面所有層搞錯了頂多是「建議很爛」，這一層搞錯是「掛了一堆殭屍單」「重複開倉」「裸倉沒停損」。因此本設計以**硬規則擋死**為主，不依賴呼叫端自律。

---

## 不可退讓的硬規則（寫進程式，違反即拋錯）

1. **僅限 Testnet**：建立 client 後必須驗證實際請求網址含 `testnet`，否則拒絕啟動。
   （教訓：`Client.FUTURES_URL` 類別常數會顯示主網位址，具誤導性，必須驗證 `_create_futures_api_uri()` 的實際結果。）
2. **只有 `approved` 狀態的提案可以執行**。pending/rejected 一律不碰。
3. **幣種白名單**：只允許 `AI_DESK_SYMBOLS` 列出的幣種，其餘一律拒絕。用途是避免與既有 bot（歷史上跑過 BTCUSDT/SOLUSDT/DOTUSDT）搶同一帳戶的部位——bot 有「孤兒倉接管」機制（`trade_journal.py` `RECONCILED_EXIT_SIDES`），會把 ai_desk 的倉當自己的接管掉，同時污染 bot 的乾淨統計。
4. **每個幣種同時最多一個部位、一組掛單**。
5. **絕不留裸倉**：進場成交後必須立刻掛上停損；掛停損失敗 → 立即市價平倉並記錄。
6. **平倉一律 `reduceOnly`**：保證只減不反手。
7. **AI 的 confidence 不參與任何倉位/價格計算**（延續 Phase 1 不變式）。

---

## 訂單生命週期

Phase 1 的提案本質是「**等回抽到 entry 再進**」的限價單（三筆提案皆如此）。若改用市價立即進場，等於把 AI 的判斷做反（它明講「不宜追空」），因此**必須支援限價掛單**。

```
approved
   │ 掛出 LIMIT 進場單（價 = proposal.entry，量 = 風控核可的 qty）
   ▼
placed ──── 逾時未成交 ───► expired（撤單）
   │ 成交
   ▼
filled ── 立刻掛 STOP(=proposal.stop) 與 TP(=proposal.take_profit)
   │
   ├── 觸及停損 ──► closed_stop
   ├── 觸及停利 ──► closed_target
   └── 人工平倉 ──► closed_manual
```

**逾時政策**：限價單掛出後 `AI_DESK_ORDER_TTL_HOURS`（預設 **24** 小時）未成交即撤單、標為 `expired`。理由：提案基於某一根 4h 收盤的市況，隔了 6 根 K 棒後那個判斷已經過期，不該繼續掛著。

**衝突政策**：
- 同幣種已有 `placed` 未成交單，又有新的 approved 提案 → **撤舊掛新**（新判斷取代舊判斷）。
- 同幣種已有 `filled` 部位 → **跳過**新提案，不加碼、不反手，並在提案上標記 `skipped_position_open`。

---

## 對帳（不可只信本地狀態）

每次執行前先向交易所拉取實際的持倉與掛單，與本地狀態比對：
- 本地說 `placed` 但交易所查無此單 → 依交易所為準，查詢該單最終狀態（成交/已撤）後更新本地。
- 交易所有本白名單幣種的部位、但本地無對應紀錄 → **不接管、不平倉**，只記錄告警（避免誤動到 bot 的倉）。

理由：交易所才是持倉的唯一真相來源，這是既有 bot 的既定原則（`restore()`），ai_desk 沿用。

---

## 模組與介面

| 檔案 | 動作 | 職責 |
|---|---|---|
| `ai_desk/executor.py` | Create | 核心執行層：白名單/testnet 驗證、掛限價、掛停損停利、撤單、對帳。純邏輯部分（訂單參數組裝、逾時判斷、衝突判斷）抽成可離線測試的純函式 |
| `ai_desk/approval.py` | Modify | 狀態機擴充：`approved → placed → filled → closed_*|expired`，新增 `exchange_order_id`、`filled_at`、`closed_at`、`realized_pnl` 欄位（含舊 DB migration） |
| `run_ai_desk_execute.py` | Create | 手動進入點：掃描 approved/placed 提案 → 對帳 → 該掛的掛、該撤的撤。單輪跑完即退出（仿 `run_once.py`） |
| `ai_desk/webview.py` | Modify | 結果追蹤改讀**真實成交**（有真實紀錄時優先於紙上推演），並標示哪些是真實、哪些仍是推演 |

**不修改** `core/` 任何檔案。限價單能力寫在 `ai_desk/executor.py`（既有 `FuturesExecutionEngineer` 只有市價 + STOP/TP，缺限價）；但**重用**它的 `round_qty`/`round_price`/`place_stop`/`place_take_profit`/`close`，不重造輪子。

---

## 設定

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `AI_DESK_SYMBOLS` | `ETHUSDT`（保守起見預設不含 BTC） | 白名單，逗號分隔。**由使用者明確設定** |
| `AI_DESK_ORDER_TTL_HOURS` | `24` | 限價單未成交的存活時間 |
| `AI_DESK_EXEC_ENABLED` | `false` | 總開關。**預設關閉**，必須明確開啟才會真的送出委託 |

`AI_DESK_EXEC_ENABLED` 預設 false 是刻意的：即使程式寫好了，沒人明確開啟就不會有任何委託送出。

---

## 測試策略

延續 Phase 1：**所有對交易所的呼叫透過注入的假 client 測試**，測試絕不碰真實網路。

| 模組 | 測試重點 |
|---|---|
| `executor.py` 純函式 | 白名單拒絕、testnet 驗證失敗即拋錯、限價單參數組裝、TTL 逾時判斷、衝突判斷（撤舊掛新 / 有倉跳過） |
| `executor.py` 流程 | 假 client：掛單→成交→掛停損停利；掛停損失敗→立即平倉；逾時→撤單 |
| `approval.py` | 新狀態轉移合法/非法路徑、舊 DB migration |
| 對帳 | 本地與交易所不一致時以交易所為準；未知部位只告警不動作 |

---

## 分階段上線

1. **Phase 2a**：程式完成 + 全測試綠，但 `AI_DESK_EXEC_ENABLED=false`。用假 client 演練完整流程。
2. **Phase 2b**：開啟開關，**手動執行一次**，用最小數量在白名單幣種上實際掛一張單，人工確認交易所看得到、撤得掉。
3. **Phase 2c**：接上排程/後台按鈕（本機或 Railway，視前面討論的結論）。

---

## 不在範圍內

- 真實資金（永遠 testnet-only）
- 自動核准（Phase 3，明確排除）
- 加碼、反手、移動停損
- 多幣種同時大量掛單（先單幣種驗證流程）
