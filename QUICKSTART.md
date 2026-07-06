# 丹丹交易團隊 — 快速上手

> 虛擬資金 · 測試網 · 非投資建議
> 想了解完整架構與待辦，讀 `PROJECT_HANDOFF.md`；想了解策略研究脈絡，讀 `docs/strategy_research_log.md`。

---

## 三種模式一覽

| 模式 | 金鑰 | 下單位置 | 推薦用途 |
|------|------|---------|---------|
| **回測 / Paper**（入門）| 不需要 | 無 / 本機模擬 | 策略驗證、前端測試 |
| **合約測試網（單台）** | 需合約測試網金鑰 | 幣安合約測試網 | 本機驗證單一策略下單流程 |
| **合約測試網（多台合一）** | 需合約測試網金鑰 + `BOTS_CONFIG` | 幣安合約測試網 | 正式部署形態（Railway 跑 9 台 b1~b9）|

**Railway（雲端 24/7）**：`dandan-futures-bot` 單一容器跑 9 台 bot，無需本機開機。

---

## 一、安裝

```bash
cd /Users/adem/量化機器
uv venv .venv --python 3.12
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
npm --prefix webapp/frontend install
```

系統 Python 3.9 太舊，一定要用 uv 建的 `.venv`。

---

## 二、儀表板（本機）

```bash
./dashboard.sh start    # 後端 + 前端
# → http://localhost:5173
./dashboard.sh status
./dashboard.sh stop
```

分頁：即時監控（動態 N 台 bot）、決策流程、回測、參數最佳化、交易日誌。

---

## 三、回測與研究（免金鑰，抓公開行情）

```bash
# 回測現行冠軍與觀察策略
.venv/bin/python run_backtest.py smc_structure --plot --report
.venv/bin/python run_backtest.py ma_convergence_pullback --plot --report

# 策略研究腳本（可重現，含 K 線快取）
.venv/bin/python research_matrix.py        # 策略 × 時框 OOS 矩陣
.venv/bin/python research_structure.py     # 純 TA vs TA+訂單流過濾 A/B

# 更嚴格的可重現研究腳本
ls research/scratchpad/                     # bootstrap 信賴下界驗證、切半驗證等

# 參數掃描 + walk-forward
.venv/bin/python run_optimize.py smc_structure --synthetic --plot

# 測試套件
.venv/bin/python -m pytest -q               # 後端
npx --prefix webapp/frontend vitest run     # 前端 26 個
```

---

## 四、合約測試網（本機單台，真的下虛擬單，可做空）

### 4-1 申請金鑰

1. 開 **https://testnet.binancefuture.com** → 右上角登入（GitHub / Google）
2. 交易頁面下方 → **API Key**（或「···」overflow 選單）
3. 點 **Create API** → 選 **HMAC** → 建立
4. 複製 **API Key** 與 **Secret Key**（Secret 只顯示一次）

### 4-2 填金鑰

```bash
cp .env.example .env
open -e /Users/adem/量化機器/.env
```

填入（等號兩邊不加空格，值不加引號）：
```
BINANCE_FUTURES_TESTNET_API_KEY=你的key
BINANCE_FUTURES_TESTNET_API_SECRET=你的secret
```

### 4-3 啟動（現行主力策略：smc_structure / 4h / 3x）

```bash
cd /Users/adem/量化機器
.venv/bin/python -u run_live_futures.py \
  --strategy smc_structure --symbol BTCUSDT --interval 4h --leverage 3 --budget 150
```

### 4-4 監控

| 方法 | 位置 |
|------|------|
| 儀表板（即時監控分頁）| http://localhost:5173 |
| 合約持倉 | https://testnet.binancefuture.com → Positions |
| 狀態檔 | `cat bot_state_futures.json` |

---

## 五、合約測試網（多台合一，正式部署形態）

9 台 bot 跑在單一進程，由 `run_multi_futures.py` 監督器管理，設定來自
`BOTS_CONFIG` 環境變數（JSON 陣列，逐台描述 id/strategy/symbol/interval/leverage/budget/risk）。

```bash
# 本機測試多台合一（需先在環境設好 BOTS_CONFIG）
.venv/bin/python -u run_multi_futures.py
```

命名空間路由對外：`/health`、`/<id>/state`、`/<id>/trades`、`/<id>/close`、
`/bots`、`/ma6`、`/klines`。正式的 9 台配置見 `PROJECT_HANDOFF.md` 第 2 節。

---

## 六、Paper 模式（免金鑰）

```bash
.venv/bin/python -u run_paper.py --interval 4h --poll 30 --strategy smc_structure
```

行情來自幣安公開 K 線（真實價格），成交是本機模擬。

---

## 七、Railway 雲端（24/7 自動運行）

Railway 上 `dandan-futures-bot` 已在跑 9 台。**git push 不會自動部署**
（除了 `webapp/**` 改動會觸發 dashboard 的 GitHub Actions）。要部署新 code：

```bash
railway up --service dandan-futures-bot --ci    # bot（9 台）
```

環境變數在 railway.app → Variables 設定（金鑰 + `BOTS_CONFIG` + `DATABASE_URL` 等）。
⚠️ 部署改變交易行為的程式碼需先跟使用者確認，即使是修 bug。

---

## 八、風控參數（`config.py`）

| 參數 | 現行值 | 說明 |
|------|--------|------|
| `atr_mult_sl` | 2.0 | 停損距離 = 2 × ATR |
| `tp_R_mult` | 2.0（籃子覆蓋成 3.0）| 停利 = R × 停損距離；b1-b8 用 rr3 出場 |
| `chand_mult` | 3.0 | Chandelier 追蹤停損倍數 |
| `risk_per_trade` | 0.003 | 每筆冒總資金 0.3% 風險（波動正規化倉位）|
| `--leverage` | 3 | 合約槓桿 |
| `--budget` | 150（b9 為 50）| 每筆最大 USDT 保證金 |

---

## 九、注意事項

- 測試網約**每月重置一次**（持倉/餘額清空，金鑰保留）；系統有 `reset_equity_peak()` 偵測重置避免熔斷永久觸發
- 訊號評估吃**主網**公開 K 線（測試網小幣行情失真），下單執行留測試網
- `.env` 已在 `.gitignore`，不會上傳 Git；Railway 用 Variables 注入
- Railway bot 與本機同 symbol bot **不要同時跑**（同帳戶淨倉會互搶）

---

> 本專案為工程與學習用途，不構成任何投資建議。
