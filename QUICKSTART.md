# 丹丹交易團隊 — 快速上手

> 虛擬資金 · 測試網 · 非投資建議

---

## 三種模式一覽

| 模式 | 金鑰 | 下單位置 | 做空 | 推薦用途 |
|------|------|---------|------|---------|
| **Paper**（入門）| 不需要 | 本機模擬 | 否 | 策略驗證、前端測試 |
| **現貨測試網** | 需現貨測試網金鑰 | 幣安現貨測試網 | 否 | 下單流程驗證 |
| **合約測試網** | 需合約測試網金鑰 | 幣安合約測試網 | ✅ | 主要使用模式 |

**Railway（雲端 24/7）**：合約 bot 持續運行，無需本機開機。

---

## 一、安裝

```bash
cd /Users/adem/量化機器
uv venv .venv --python 3.12
.venv/bin/pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
npm --prefix webapp/frontend install
```

---

## 二、儀表板（本機）

```bash
./dashboard.sh start    # 後端 + paper bot + 前端
# → http://localhost:5173
./dashboard.sh status
./dashboard.sh stop
```

五個分頁：即時監控、回測、決策流程、參數最佳化、交易日誌。

---

## 三、合約測試網（本機，真的下虛擬單，可做空）

### 3-1 申請金鑰

1. 開 **https://testnet.binancefuture.com** → 右上角登入（GitHub / Google）
2. 交易頁面下方 → **API Key**（或「···」overflow 選單）
3. 點 **Create API** → 選 **HMAC** → 建立
4. 複製 **API Key** 與 **Secret Key**（Secret 只顯示一次）

### 3-2 填金鑰

```bash
open -e /Users/adem/量化機器/.env
```

填入（等號兩邊不加空格，值不加引號）：
```
BINANCE_FUTURES_TESTNET_API_KEY=你的key
BINANCE_FUTURES_TESTNET_API_SECRET=你的secret
```

### 3-3 啟動

```bash
cd /Users/adem/量化機器

# Fibonacci 回調策略（目前主要策略，10x 槓桿，每筆最多 100 USDT 保證金）
.venv/bin/python -u run_live_futures.py \
  --strategy fib_retracement --interval 1m --poll 15 --budget 100 --leverage 10

# 多空 z-score 策略
.venv/bin/python -u run_live_futures.py --strategy zscore_ls --interval 1m --poll 15
```

### 3-4 監控

| 方法 | 位置 |
|------|------|
| 儀表板（即時監控分頁）| http://localhost:5173 |
| 合約持倉 | https://testnet.binancefuture.com → Positions |
| 狀態檔 | `cat bot_state_futures.json` |
| 成交紀錄 | `cat trades_futures.csv` |

---

## 四、Railway 雲端（24/7 自動運行）

Railway 上已有合約 bot 在跑。**不需要開本機 bot**，Railway 會自動重啟。

### 查看狀態

在 railway.app → Deployments 看 logs。

### 重新部署（修改程式後）

```bash
railway up    # 在 /Users/adem/量化機器 目錄執行
```

### 環境變數

在 railway.app → Variables 設定：
```
BINANCE_FUTURES_TESTNET_API_KEY=你的64字元key
BINANCE_FUTURES_TESTNET_API_SECRET=你的64字元secret
```

---

## 五、Paper 模式（免金鑰）

```bash
.venv/bin/python -u run_paper.py --interval 1m --poll 15 --strategy fib_retracement
```

行情來自幣安測試網（真實價格），成交是本機模擬。

---

## 六、回測與最佳化

```bash
# 回測
.venv/bin/python run_backtest.py fib_retracement --plot --report
.venv/bin/python run_backtest.py zscore_ls --plot --report

# 參數掃描 + walk-forward
.venv/bin/python run_optimize.py fib_retracement --synthetic --plot
.venv/bin/python run_optimize.py ema_cross --synthetic --include-risk

# 測試套件（218 個）
.venv/bin/python -m pytest -q
```

---

## 七、四個策略

| 策略名 | Regime | 做空 | 說明 |
|--------|--------|------|------|
| `ema_cross` | 趨勢 | 否 | EMA 快慢線交叉 |
| `zscore_revert` | 盤整 | 否 | z-score 均值回歸，僅做多 |
| `zscore_ls` | 盤整 | ✅ | z-score 均值回歸，多空雙向 |
| `fib_retracement` | 盤整 | ✅ | Fibonacci 38.2% / 61.8% 回調 + EMA 趨勢方向 |

Regime 過濾：ER + CHOP + ADX 2-of-3 多數決，非適合市況時不開倉。

---

## 八、風控參數

| 參數 | 目前值 | 說明 |
|------|--------|------|
| `atr_mult_sl` | 2.0 | 停損距離 = 2 × ATR |
| `tp_R_mult` | 2.0 | 停利 = 2 × 停損距離（期望值 > 0）|
| `chand_mult` | 3.0 | Chandelier 追蹤停損倍數 |
| `--leverage` | 10 | 合約槓桿 |
| `--budget` | 100 | 每筆最大 USDT 保證金 |

---

## 九、注意事項

- 測試網約**每月重置一次**（持倉/餘額清空，金鑰保留）
- 合約測試網 K 線歷史只保留約 **18 天**
- `.env` 已在 `.gitignore`，不會上傳到 Git；Railway 用 Variables 注入
- Railway bot 與本機 bot **不要同時跑**（會重複下單）

---

> 本專案為工程與學習用途，不構成任何投資建議。
