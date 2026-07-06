# 策略研究記錄

這份文件彙整 dandan 交易系統的策略回測研究歷程——方法論、每次測試的結論、
以及對應的可重現腳本位置。目的是讓這些研究不只活在對話記憶裡，repo 本身就有
完整、可重現的紀錄。

## 方法論

所有回測一律遵守：

1. **真實資料**：幣安 USDT 本位合約公開 K 線 API（不是現貨、不是模擬）。
2. **真實成本**：`fee_rate=0.0005`（單邊 taker）、`slippage=0.0002`、
   `fill_lag=1`（訊號當根收盤才成交，下一根 open 才真正進場）、
   `funding_rate_per_8h=0.0001`。
3. **統計顯著性判準**：不看單純平均值，用 `backtest.tournament.
   bootstrap_mean_lower_bound()` 對交易 pnl 重抽樣，取信賴下界。
   **下界 > 0 才算「顯著正 edge」**；下界 ≤ 0 但期望值 > 0 記為
   「正但不顯著」（可能只是抽樣運氣）；期望值 ≤ 0 記為「負期望」。
4. **防過擬合**：調參數/加開關時用切半驗證——時間軸切一半，前半跟後半
   分別算信賴下界，新設定必須**兩半都不輸 baseline** 才算穩健改善，
   只贏一半視為過擬合、不採用。
5. **池化**：單一幣種樣本太少，8 幣（SUI/BTC/ETH/ARB/XRP/DOGE/ADA/DOT）
   合併計算才有統計力。
6. **因果性**：所有指標/樞紐點偵測都不能用到未來資料，用「截尾重算、
   前綴值不變」的方式驗證（見 `tests/test_signal_engineer.py` 系列測試）。

腳本存於 `research/scratchpad/`，klines 快取存於 `research/klines_cache/`
（`.gitignore` 排除 `*.csv`，首次執行會自動從幣安重新抓取並快取）。

## 目前的結論（累積到 2026-07-06）

**唯一通過信賴下界關卡的配置：`smc_structure` / 4h / 8 幣籃子 / rr3 出場
（`tp_R_mult=3.0`），信賴下界 +1.665，448 筆交易，勝率 35.5%。這是目前
線上 b1-b8 實際跑的配置。**

其餘測過的東西（規則策略 17 種、雙均線六線系統、ML 過濾層、古典圖表形態、
迴歸通道、多週期共振過濾、擴大籃子、換週期、`require_fvg` 開關）全部
沒有通過同樣的信賴下界關卡——詳見下方逐條記錄。

---

## 2026-07-05：① smc_structure 出場機制優化（切半防過擬合）

**腳本**：`research/scratchpad/exit_optim.py`

每個出場配置在每個幣的前半/後半資料上分別測，兩半都要贏 baseline 才採用。
候選：baseline(tp_R_mult=2.0)、rr3(3.0)、rr4(4.0)、tightSL_rr3、
trail/trail_tight/trail_loose（Chandelier 主導）。

**結果**：只有 **rr3** 兩半都贏（前半 LB +1.57 vs baseline +0.83、
後半 -2.00 vs -2.69）。全期間 LB 從 +0.53 提升到 **+1.67**（448 筆，
較原本 3 倍改善）。其他配置（如 trail_loose 前半期望最高 +8.23）只贏
樣本內、後半反而輸更多，判定過擬合，不採用。

**已部署**：`tp_R_mult=3.0` 已於 2026-07-05 上線到 8 幣籃子（BOTS_CONFIG
b1-b8 的 `risk.tp_R_mult`）。

## 2026-07-05：② 擴大籃子幣種數量

**腳本**：`research/scratchpad/basket_analysis.py`

現有 8 幣 LB +1.665，逐一加入候選幣（LINK/AVAX/NEAR/OP/INJ/APT/LTC）測試
池化下界變化。

**結果**：加入任何一個候選幣都讓下界下降（+0.87～+1.61 之間），沒有一個
贏過現有 8 幣組合。**結論：擴大籃子無效，更多幣＝更多雜訊稀釋，不是更多
分散化。**

## 2026-07-05：③ 逐幣貢獻度分析（leave-one-out）

**腳本**：`research/scratchpad/basket_analysis.py`（同上，後半段）

8 幣中每一幣單獨測都是負下界（證明「池化才有 edge」）。Leave-one-out：
拿掉 DOGE 後下界從 +1.665 上升到 +2.05（拖累最大）；SUI/ETH/ADA 貢獻
最大（拿掉後下界下降最多）。

**結論**：LOO 是樣本內判斷、過擬合風險高、Δ 值也不算大，**維持 8 幣不動**，
只記錄 DOGE 為觀察對象，未來若要調整應等待更多樣本外資料而非只憑這次
LOO 結果。

## 2026-07-05：多週期共振（HTF 過濾）消融實測

**腳本**：`research/scratchpad/htf_ablation.py`

日線 MA20/60 排列方向（`core.signal_engineer.htf_trend`，causal shift(1)
防前視）當進場方向過濾器，測試對 `smc_structure` 與
`ma_convergence_pullback`（雙均線系統）兩策略的影響。

**結果——效果完全相反**：
- `smc_structure` + HTF：**有害**（LB +0.53 → -3.37）。BOS 靠提早抓
  反轉獲利，等日線轉向才進場剛好錯過最肥段。**線上籃子維持關閉，
  永遠別開。**
- `ma_convergence_pullback` + HTF：**明顯改善**（期望 -0.27 → +2.48，
  但樣本仍不顯著）。**b9（LINKUSDT 觀察倉）已開啟此開關。**

**通用教訓**：過濾器效果取決於「訊號靠什麼賺錢」，順勢延續類配高週期
共振有效，提早反轉類配置反而扼殺 edge，不存在萬用過濾器。

## 2026-07-05：換週期（1h）驗證

**腳本**：`research/scratchpad/tf_1h_check.py`（同時測 smc_structure 與雙均線）

使用者想切到 1h 累積更多交易樣本，先驗證會不會破壞現有 edge。

**結果**：`smc_structure` 4h→1h：448 筆 LB+1.67 → 1596 筆 LB -1.30（負）。
雙均線系統 4h→1h 同樣崩壞（見下一條）。**結論：1h 换週期會摧毀現有的
唯一驗證邊際優勢，不建議切換。**

## 2026-07-05：雙均線系統（YouTube 六線密集/發散）完整驗證

**腳本**：`research/scratchpad/dualma_1h.py`

還原使用者分享的 YouTube 影片系統：MA20/60/120 + EMA20/60/120 六線，
密集(糾結)→發散判斷 + 首次回踩 20 均線不破進場（`ma_convergence_pullback`，
`core/quant_researcher.py`）。

**結果（4h vs 1h，有無日線共振）**：

| 配置 | 筆數 | 勝率 | 期望/筆 | 信賴下界 | 判決 |
|---|---|---|---|---|---|
| 4h 原版 | 160 | 38.1% | +1.40 | −3.54 | ⚠ 不顯著 |
| 4h +日線共振(b9現行) | 86 | 40.7% | +5.06 | −2.14 | ⚠ 不顯著(最佳) |
| 1h 原版 | 474 | 29.7% | −3.90 | −6.46 | ❌ 負 |
| 1h +日線共振 | 217 | 35.0% | +0.19 | −4.05 | ⚠ 不顯著 |

也測過出場優化（tp_R_mult 2→3/5、Chandelier trail）跟進場精選（回踩確認
勝率 37.5% vs 死叉 29.5%），方向都對但樣本仍不足以顯著。

**結論：均線族系統的瓶頸是訊號密度不夠，不是缺少某個過濾器。** 這是第三次
獨立驗證同一件事（先前 ML 過濾層、`ema_fib_vol` 消融都指向同結論）。

## 2026-07-06：`is_breakout` 密集前提 bug 修正

使用者反映雙均線圖表訊號位置跟影片對不上，追查發現 `is_breakout` 原本
沒檢查是否真的經歷過密集(is_density)，會把「已經在半路的強趨勢」誤標成
密集突破。修正後（`require_density_for_breakout` 開關，僅圖表面板開啟，
b9 實盤暫不受影響）驗證：LINK 4h 300 根 breakout 3→1、8 幣池化回測
原版 160→33 筆、+HTF 86→10 筆，樣本大減、下界更負——**強化既有結論，
不是新問題**：原本的 160/86 筆本身就摻了一部分誤判的假突破。

## 2026-07-06：古典圖表形態 + 迴歸通道（兩個全新策略）

**腳本**：`research/scratchpad/new_strategies_backtest.py`

使用者要求測試 TradingView 兩個編輯精選腳本背後的核心概念：
- **Chart Patterns Screener**（三角形/楔形收斂突破）→ 新策略
  `chart_pattern_breakout`（`core.signal_engineer.trendline_pair()` +
  `core/quant_researcher.py`，TDD）
- **Polynomial/Linear Regression Volume Profile**（迴歸配適通道）
  → 新策略 `regression_channel`（滾動 OLS 迴歸通道，TDD）

兩者訊號來源跟現有八個策略完全不同（真實樞紐趨勢線 / 統計迴歸，而非
六線價差或 BOS 結構）。

**結果**：

| 配置 | 筆數 | 期望 | 信賴下界 | 判決 |
|---|---|---|---|---|
| chart_pattern_breakout(預設) | 581 | −1.80 | −3.43 | ❌ |
| chart_pattern(嚴格收斂0.5+寬樞紐8/8) | 285 | +1.46 | −1.30 | ⚠ 不顯著 |
| regression_channel(w=100預設) | 742 | −3.25 | −4.85 | ❌ |
| regression_channel(w=200,band=2.5) | 250 | +0.97 | −2.45 | ⚠ 不顯著 |

**結論：這是本輪研究第三/四次獨立驗證同一件事**——調緊參數可以把點估計
期望值從負轉正，但樣本同時大減，信賴下界永遠還是負的。古典圖表形態、
統計迴歸通道，跟均線收斂系統一樣，沒有證明出統計顯著的 edge。冠軍不變。

## 2026-07-06：`require_fvg` 開關切半驗證

**腳本**：`research/scratchpad/require_fvg_split_half.py`

在已驗證有效的 `smc_structure` 策略內部微調（不是新策略）：測試要求
BOS 突破需同時伴隨 Fair Value Gap（`require_fvg=True`，現行預設 False）
是否更好。

**結果**：兩半都輸——前半 LB +1.57→−2.10、後半 −2.00→−4.32，全期間
+1.665→−0.980，樣本從 448 筆砍到 332 筆但勝率沒有提升反而略降
（35.5%→33.7%）。**結論：`require_fvg` 維持預設關閉，不要開啟。**
BOS 突破本身就是主要訊號來源，額外要求 FVG 同時出現篩掉的多半是好訊號。

---

## 尚待驗證/待辦

- **risk_per_trade 加碼**：在唯一驗證有效的 8 幣籃子上提高倉位（而非
  分散到新策略），尚未執行。
- **DOGE 降權**：LOO 顯示 DOGE 拖累最大，考慮降低其 budget/risk_per_trade
  而非整個移除（避免樣本內判斷的過擬合風險），尚未執行。
- **實盤 vs 回測持續對照**：目前沒有系統化比對 b1-b8 實際交易跟同期間
  回測預期的差異，屬於驗證基礎設施缺口，尚未建立。
