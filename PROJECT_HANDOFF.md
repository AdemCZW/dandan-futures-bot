# 丹丹交易團隊 — 專案交接文件

> 給新對話 / 其他 AI 接手用的完整背景。日期：2026-07-06。
> 專案路徑：`/Users/adem/量化機器`，GitHub：`AdemCZW/dandan-futures-bot`（已公開，GitHub Pages 對外儀表板）。
> 舊版交接文件（2026-06-26）架構已大幅過時，本版全面重寫。

---

## 0. 最重要 — 安全紅線（絕不可違反）

- **全部都是幣安「合約測試網」(Binance Futures Testnet)，虛擬資金，不碰真錢。** 下單/持倉/餘額都在測試網。
- 訊號評估已改吃**主網**公開 K 線（2026-07-06 起，見第 6 節「稽核修復」），但這只是為了讓策略看到真實市場價格，**下單執行仍 100% 在測試網**。
- API 金鑰只在 Railway 環境變數，**永遠不顯示在對話、不打包進映像、不能用任何方式讀出完整內容**（含 `railway variables --kv` 這類會連帶洩漏金鑰的指令——安全分類器會擋，不要試圖繞過）。
- **AI 不可代替使用者下單/平倉/轉帳/改倉位**。
- **AI 不可把 repo 從 private 改成 public**（已經是 public，但這件事本身是使用者做的，AI 未來也不可替使用者做類似的存取權限變更）。
- 讀取 Railway 正式環境變數/正式服務行為需要使用者**明確授權**（不能只靠先前的籠統同意推論），部署改變**實盤交易行為**的程式碼同樣需要明確授權。

---

## 1. 系統架構（現況，2026-07-06）

```
Railway 專案 dandan-futures-bot（railway CLI 已登入 ademczw's Projects）
├── dandan-futures-bot   → https://dandan-futures-bot-production.up.railway.app
│                          單一進程跑 9 台 bot（b1~b9），run_multi_futures.py 監督器管理
├── dandan-dashboard     → https://dandan-dashboard-production.up.railway.app（休眠省錢，FastAPI+前端）
├── GitHub Pages         → 公開儀表板（前端直連 bot 容器 API，不經 dashboard）
└── Postgres（bot + dashboard 共用，交易紀錄都寫這）

舊的 dandan-shortterm / pacific-radiance / dandan-longterm 已下線（設定保留可復原）。
```

- **bot** = `run_multi_futures.py`（多 bot 監督器）+ `run_live_futures.py`（單台交易邏輯，被 import 復用）。
- 每台 bot 命名空間路由：`/health`、`/<id>/state`、`/<id>/trades`、`/<id>/close`、`/bots`（列出全部）、`/ma6`、`/klines`（圖表資料）。
- 本機開發：`.venv/bin/python -m uvicorn webapp.backend.main:app --port 8000` + 前端 `npm run dev`（vite :5173）。用 **uv 建的 `.venv`**（系統 Python 3.9 太舊）。

---

## 2. 現行 9 台 bot 配置

| Bot | 標的 | 策略 | 週期 | 槓桿 | budget | 備註 |
|---|---|---|---|---|---|---|
| b1 | SUIUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b2 | BTCUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b3 | ETHUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b4 | ARBUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b5 | XRPUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b6 | DOGEUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子，LOO顯示拖累最大（未動，見第4節） |
| b7 | ADAUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b8 | DOTUSDT | `smc_structure` | 4h | 3x | 150 | 主籃子 |
| b9 | LINKUSDT | `ma_convergence_pullback` | 4h | 3x | 50 | 純觀察倉，雙均線系統，**未證明有edge** |

b1-b8 是**唯一驗證有統計顯著 edge 的配置**：`smc_structure`/4h + `tp_R_mult=3.0`(rr3 出場)，透過 `BOTS_CONFIG` 的 `risk` 欄位覆蓋（`apply_risk_overrides`，`run_multi_futures.py`）。b9 是雙均線系統的純觀察倉，小預算，**目的是驗證系統而非獲利**，`use_htf_filter` 目前開啟但 3 年資料顯示可能不該開（見第 4 節待辦）。

`BOTS_CONFIG` 環境變數（JSON，Railway 上）控制全部 9 台，**AI 無法讀取完整內容**（會觸發金鑰保護），要改必須請使用者提供內容或自己在 Railway 後台改。

---

## 3. 核心方法論（貫穿整個研究歷程，任何新測試都要照做）

1. **真實資料**：幣安 USDT 本位合約公開 K 線 API，不用現貨、不用合成資料做結論性驗證。
2. **真實成本**：`fee_rate=0.0005`、`slippage=0.0002`、`fill_lag=1`（訊號當根收盤才成交，下一根 open 才真正進場）、`funding_rate_per_8h=0.0001`。
3. **統計顯著性**：不看平均值，用 `backtest.tournament.bootstrap_mean_lower_bound()` 重抽樣，**下界 > 0 才算「顯著正 edge」**。
4. **防過擬合**：調參數用切半驗證——時間軸切一半，新設定必須**兩半都不輸 baseline** 才算穩健，只贏一半＝過擬合、不採用。
5. **池化**：8 幣（SUI/BTC/ETH/ARB/XRP/DOGE/ADA/DOT）合併才有統計力，單幣樣本太小。
6. **因果性**：所有指標/樞紐點偵測都不能用到未來資料，用「截尾重算、前綴值不變」驗證。
7. **長記憶特徵要用時間切分驗證，不能只信 shuffled CV**（2026-07-06 資金費率特徵教訓——shuffled CV 會被自相關特徵騙出假的預測力）。

完整方法論 + 逐條研究記錄在 **`docs/strategy_research_log.md`**，可重現腳本在 **`research/scratchpad/`**（含 K 線/資金費率 fetch+cache，首次執行自動抓取）。

---

## 4. 累積研究結論（目前為止，2026-07-06）

**唯一通過信賴下界關卡：`smc_structure`/4h/8幣籃子/rr3 出場。3 年資料驗證下界 +1.77（1年資料時是+1.67，樣本翻3倍後不降反升，edge 更可信）。**

**全部測過、沒有找到 edge 的方向**（別重踩，除非有新的、真的不同的角度）：
- 17 個規則策略在 15m/1h 幾乎全滅（手續費+雜訊吃死）
- 換 1h 週期會摧毀 smc_structure 和雙均線系統的表現
- 擴大籃子（加 LINK/AVAX/NEAR/OP/INJ/APT/LTC）全部降低信賴下界
- ML 過濾層（技術指標特徵）AUC 0.557，等於瞎猜
- ML 過濾層（資金費率特徵）shuffled CV 看似有效（AUC+0.03），時間切分驗證後翻盤成比瞎猜還差
- 雙均線系統（YouTube 六線密集/發散，`ma_convergence_pullback`）——出場優化、進場精選、多週期共振、合併訊號類型，五個獨立方向都測過，3年資料下界最好只到 -1.65，從未轉正
- 古典圖表形態突破（`chart_pattern_breakout`）、迴歸通道均值回歸（`regression_channel`）——兩個全新策略類別，嚴格回測後同樣沒有 edge
- `smc_structure` 的 `require_fvg` 開關——兩半都輸，維持關閉

**多週期共振（HTF 過濾）教訓**：效果取決於訊號類型。`smc_structure` 加 HTF 有害（BOS 靠提早抓反轉，等日線轉向就錯過肥段），`ma_convergence_pullback` 加 HTF 在 1 年資料看似有幫助，但 3 年資料顯示不再優於 baseline——**b9 目前仍開著 HTF，這個決定的證據基礎比想像中薄弱，待重新評估**。

---

## 5. 實盤交易稽核（2026-07-06，重要）

使用者反映實盤持續虧損、感覺一直跟市場反方向下單。對 b1-b9 全部成交紀錄做系統化稽核（`research/scratchpad/live_trade_audit.py`），發現**不是策略問題，是系統問題**：

| # | 問題 | 狀態 |
|---|---|---|
| F1 | bot 訊號原本吃測試網 K 線，但所有驗證用主網資料——測試網小幣有主網不存在的幽靈波動（ADA 實測偏離 10.5%，b7 的 TP 在主網從未存在的價位成交） | **已修復並部署**（`SIGNAL_DATA_SOURCE`，commit `21d1166`） |
| F2 | Railway 每次 redeploy 是全新容器，狀態檔消失 → 對已決策過的 K 棒重複決策 → 重複進場（b7 實測同棒進場3次×2輪） | **已修復並部署**（journal 推斷 fallback，commit `21d1166`） |
| F3 | journal 進場價記的是訊號棒收盤價，不是實際成交均價，逐筆分析會被扭曲 | **未修復** |
| F4 | 儀表板「勝率21%/14筆」混了舊時代(1h)交易、接管孤兒倉位的紀錄、部署churn重進，乾淨樣本其實只有5-6筆 | **未修復** |

修復①②已於 2026-07-06 部署驗證（9台正確接續，無重複進場）。**F1/F2 修好後，累積的實盤紀錄才真正開始檢驗那個 3 年回測驗證出來的 edge**——之前的紀錄參考價值有限（含測試網幽靈行情+部署churn污染）。

「跟市場反方向」有一部分**是策略本性不是bug**：13筆進場有11筆逆著日線趨勢，因為 smc BOS 本來就是提早抓反轉（HTF過濾對它有害，見第4節）。

---

## 6. 尚待處理的待辦（依優先序）

1. **b9 關閉 `use_htf_filter`**：3年資料顯示不再優於baseline，已決定要關，但改 `BOTS_CONFIG` 需要讀取正式環境變數（被安全機制擋），待使用者提供內容或自己在 Railway 後台修改。
2. **稽核修復③**：journal 進場價改記實際成交均價。
3. **稽核修復④**：儀表板統計排除接管/reconciled/跨週期紀錄。
4. **risk_per_trade 加碼**：在唯一驗證有效的8幣籃子上提高倉位（比繼續找新策略更划算），使用者尚未決定。
5. **DOGE 降權**：leave-one-out 顯示拖累最大，考慮降低 budget 而非整個移除（避免樣本內判斷的過擬合），尚未執行。
6. **實盤 vs 回測持續對照**：`live_trade_audit.py` 可重跑，但尚未自動化成定期對照。

---

## 7. 測試 / 驗證

- 後端 `.venv/bin/python3 -m pytest`：**1030 個測試**，全綠。前端 `npx vitest run`（`webapp/frontend/`）：**26 個測試**，全綠。
- 開發守則：**嚴格 TDD**（先寫失敗測試、看它失敗、最小實作通過）。本專案所有策略/指標/bug修復都照這流程，這個 session 沒有例外。
- 部署：`railway up --service dandan-futures-bot --ci`（bot，9台）、儀表板另有 `railway.webapp.json` 切換流程（見舊版文件第6節，機制未變）。**git push 不會自動部署**（除了 `webapp/**` 改動會觸發 dashboard 的 GitHub Actions 自動部署）。

---

## 8. 給接手對話的建議

- 先讀 `docs/strategy_research_log.md` 了解完整研究脈絡，不要重測已經記錄「別再重踩」的東西。
- 任何新策略/新特徵的假設，都要走完整套方法論（真實資料+真實成本+bootstrap信賴下界+切半或時間切分驗證）才能下結論，半吊子驗證（例如只看點估計、只用shuffled CV）在這個 repo 的標準下不算數。
- 部署到 Railway 正式服務（尤其影響交易邏輯的）一律先跟使用者確認，即使是修 bug。
- 使用者的口頭禪／偏好：直接下結論、不要曖昧；要看到具體數字（筆數/勝率/期望值/信賴下界）而非「應該有效」這種定性判斷；發現問題要直接講、不用委婉。
