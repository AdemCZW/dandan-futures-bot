# ML 過濾器訓練教學

> 給丹丹交易團隊。目的：教你正確訓練 + 看懂品質 + 避免被假數字騙。
> 相關檔案：`ml/ml_filter.py`（特徵+訓練）、`ml/triple_barrier.py`（標籤）、
> `ml/purged_kfold.py`（驗證）、`run_train_filter.py`（主流程）、`models/*.pkl`（成品）。

---

## 0. 這東西在做什麼（一句話）

策略規則先產生「進場訊號」，ML 模型再評估「這種市場狀態下，這個訊號的勝率高不高」，
低於門檻就**否決**這一單。它不是預測價格，是**過濾掉爛訊號**。

```
策略訊號 → 取當下市場特徵 → 模型算勝率 p → p < 門檻就不進場
```

---

## 1. 訓練流程的四步（理解比背指令重要）

### 步驟 A：資料
抓 K 線（越多越好，至少幾千根）。**一定要用合約資料**，不要用現貨。

### 步驟 B：特徵（X）— 「進場當下市場長怎樣」
目前用 7 個：`atr, adx, rsi, er(效率比), choppiness(震盪度), atr_pct, vol_z(量能z)`。
全是「市場狀態/波動」類，描述環境，不是價格本身。

### 步驟 C：標籤（y）— 「這單後來贏還輸」用 **Triple Barrier**
從進場點往後看，誰先碰到：
- 上緣（+pt%）→ 贏，label **+1**
- 下緣（−sl%）→ 輸，label **−1**
- 都沒碰到就到期（vb 根 K 線）→ **0**（超時／沒結果）

模型學的是「+1 vs 非+1」。

### 步驟 D：驗證 — 用 **Purged K-Fold**（不是普通 K-fold）
時間序列不能隨機切（會偷看未來）。Purged K-Fold 會把訓練/測試之間的重疊期「清除+禁運」，
避免 label 用到測試期資料 → 才不會高估準確率。這是 López de Prado 的正統做法。

---

## 2. 怎麼跑（指令）

```bash
# 基本：用測試網資料訓練 fib_channel
uv run python run_train_filter.py fib_channel --output models/fib_channel.pkl

# 用快取/自己抓的合約資料（推薦，資料較多）
uv run python run_train_filter.py fib_channel --cache /tmp/sol15m.csv --output models/fib_channel.pkl
```

**可調旋鈕：**
| 參數 | 預設 | 意思 | 怎麼調 |
|------|------|------|--------|
| `--start` | 12 months ago | 抓多久的資料 | 越長越多樣本 |
| `--pt` | 0.03 | 上緣 +3%（贏） | 標籤太少時調小（如 0.015）讓更多單「碰到上緣」 |
| `--sl` | 0.03 | 下緣 −3%（輸） | 通常跟 pt 對稱 |
| `--vb` | 24 | 往後看幾根 K | 太多 0（超時）時調大（如 48），給更多時間碰邊 |
| `--splits` | 5 | 交叉驗證折數 | 樣本少就調小（3） |

---

## 3. ⚠️ 怎麼看懂結果（最重要 — 別被準確率騙）

訓練完會印：
```
[Labels] +1=10  -1=30  0=219          ← 標籤分布
[CV PurgedKFold] accuracy: 0.950 ...   ← 交叉驗證準確率
特徵重要度：...
```

### 致命陷阱：類別不平衡時「準確率」是假的

上面 259 筆只有 10 筆 +1。一個「永遠猜不會贏」的笨模型準確率 = 249/259 = **0.961**。
你的模型 0.950 反而**輸給笨基準** → 等於沒用、甚至扣分。

**正確判斷法 —— 自己算 baseline 比一比：**
```
baseline = (非+1 的筆數) / 總筆數
模型有用 ⟺ CV 準確率「明顯」高於 baseline（不是差不多，要明顯）
```

### 一個「健康」的訓練長怎樣
- ✅ **+1 至少幾十筆以上**（10~20 筆太少，練不出東西）
- ✅ 三類分布別太極端（理想 +1/−1/0 各佔一定比例）
- ✅ CV 準確率 **明顯 > baseline**，且 ± 標準差小（穩定）
- ✅ 特徵重要度合理（不是單一特徵 0.99 其餘 0）

### 你目前的狀況（2026-06-26 實測）
| 模型 | 準確率 | baseline | 判決 |
|------|--------|----------|------|
| fib_channel/SOL | 0.950 | 0.961 | ❌ 比不做還差（+1 只有 10 筆） |
| smc/ETH | 0.657 | 0.759 | ❌ 比不做還差 |

→ 所以我們**先把 ML 關了**（`ML_THRESHOLD=0`）。要重練到「明顯贏 baseline」才值得開。

---

## 4. 怎麼讓它真的有用（瓶頸是資料）

1. **更多正樣本**：先調 `--pt 0.015 --vb 48`，讓更多單在期限內碰到上緣 → +1 變多。
   （但 pt 太小會把雜訊也標成贏 → 要平衡，多試幾組）
2. **更多歷史**：`--start "24 months ago UTC"` + 用合約資料快取。
3. **處理不平衡**：xgboost 加 `scale_pos_weight = 非+1數/+1數`（讓模型重視稀少的贏單）。
4. **改看對的指標**：除了準確率，要看 **AUC / precision**（目前腳本只印準確率 → 該升級）。
5. **務實面**：測試網累積真實成交很慢，短期內資料量就是不夠。
   **與其硬練 ML，不如先把策略本身（規則）調好** —— 訊號夠好，ML 才有東西可濾。

---

## 5. 練好之後怎麼上線

```bash
# 1. 訓練 → 存到 models/{策略名}.pkl（檔名要跟 BOT_STRATEGY 一致才會自動載入）
uv run python run_train_filter.py fib_channel --cache /tmp/sol15m.csv --output models/fib_channel.pkl

# 2. commit（.pkl 沒被 ignore，會打包進 bot 映像）
git add models/fib_channel.pkl && git commit -m "retrain fib_channel ML filter"

# 3. 部署到對應 bot（重新 build 映像）
railway up --service dandan-shortterm --ci

# 4. 開啟過濾（門檻 0.55 是「勝率要 >55% 才放行」，可調）
railway variables set ML_THRESHOLD=0.55 --service dandan-shortterm
```

> bot 啟動時看 log 有 `[ML Filter] 已載入模型` 就代表生效；
> 每次否決會印 `ML Filter 否決（p=... < ...）`。

---

## 6. 已知的程式碼小問題（之後可修）

- `extract_features` 的 `vol_z` 用「整段資料的 mean/std」算 → 訓練時**偷看到未來**（輕微洩漏）。
  正確應該用「進場點之前的滾動視窗」算。
- `run_train_filter.py` 只印準確率，**應加印 AUC + naive baseline**，免得再被假數字騙。

要修這兩個 + 加 scale_pos_weight，跟我說「升級訓練腳本」即可。
