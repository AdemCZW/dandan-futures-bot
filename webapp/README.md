# 儀表板（React SPA + FastAPI）

可互動的前端儀表板，五個分頁：
- **即時監控**：每 5 秒自動刷新，顯示 paper bot 的即時部位 / 現價 / 權益 / 未實現損益 / 近期成交
  （資料來自 `run_paper.py` 持久化狀態 + 即時行情）。綠燈＝運行中。
- **回測**：選策略 → 跑回測 → 權益曲線 / 績效卡片 / 交易表。
- **決策流程**：攤開 6 角色 SOP 管線，逐根列出每個進出場「位置」的決策——點任一列展開該位置在
  市場分析師 / 信號工程師 / 量化研究員 / 風控官 / 執行工程師 各關的數值與判斷。
- **參數最佳化**：參數掃描熱圖 + walk-forward 樣本外彙總。
- **交易日誌**：瀏覽 `trades.db`（可依 mode 過濾，含 paper / live / 合約）。資料來源可選 `synthetic`（離線、不需金鑰）或
`testnet`（幣安現貨測試網公開行情）。

## 一鍵啟停（推薦）

專案根目錄的 `dashboard.sh` 會用 `setsid + nohup` 把後端 + paper bot + 前端三件套一起拉起來，
**完全脫離終端、關掉終端機也不會被收掉**；日誌寫到 `.run/*.log`。

```bash
./dashboard.sh start      # 一鍵啟動三件套 → http://localhost:5173
./dashboard.sh status     # 看狀態（含 HTTP 健康檢查）
./dashboard.sh stop       # 全部停止
./dashboard.sh restart
# 可調：INTERVAL=5m POLL=30 STRATEGY=zscore_revert ./dashboard.sh start
```

> 服務已在跑時別重複 `start`（會撞埠）；先 `stop` 或用 `restart`。

## 啟動（手動，兩個終端）

```bash
# 1) 後端 FastAPI（在專案根目錄）
.venv/bin/uvicorn webapp.backend.main:app --port 8000

# 2) 前端 Vite（另一個終端）
cd webapp/frontend
npm install          # 第一次才需要
npm run dev          # 開 http://localhost:5173
```

前端的 `vite.config.js` 已把 `/api` 代理到 `http://localhost:8000`，所以開發時不必處理跨網域。

## 結構

```
webapp/
├── backend/
│   ├── main.py      # FastAPI app：路由 / CORS / Pydantic 驗證
│   └── service.py   # 橋接 core/backtest，回傳 JSON（不含 Web 框架相依，易測）
└── frontend/
    ├── src/
    │   ├── App.jsx              # 分頁外框
    │   ├── api.js               # 後端 client
    │   └── components/          # Backtest / Optimize / Journal
    └── vite.config.js
```

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET  | `/api/health` | 健康檢查 |
| GET  | `/api/strategies` | 策略清單與預設參數 |
| POST | `/api/backtest` | 回測：回傳績效、權益曲線、交易 |
| POST | `/api/explain` | 決策軌跡：6 角色 SOP 流程 + 每個位置的逐關決策 |
| POST | `/api/optimize` | 參數掃描熱圖 + walk-forward 彙總 |
| GET  | `/api/trades` | 讀 `trades.db` 交易留底（可依 mode 過濾）|
| GET  | `/api/live` | paper bot 即時狀態（部位/現價/權益/未實現/近期成交）|

**即時監控**搭配 `run_paper.py` 使用：先在背景跑 `python -u run_paper.py --interval 1m --poll 15`，
即時監控分頁就會每 5 秒顯示它的最新決策與權益。

後端測試：`.venv/bin/python -m pytest tests/test_api.py`（全用 synthetic，離線）。

> 仍是測試網／虛擬資金、非投資建議。CORS 在開發時放行所有來源，正式部署請收斂白名單。
