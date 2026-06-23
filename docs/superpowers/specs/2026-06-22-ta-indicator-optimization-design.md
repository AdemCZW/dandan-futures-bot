# 技術指標後台運算優化設計（#1–8）

**日期**: 2026-06-22
**範圍**: 提升每個指標/策略的準確度與實戰性，全部 causal、誠實回測、testnet-only
**來源**: 13-agent 研究工作流（上網查證 + 對抗式查核）的綜合結論

---

## 背景與核心發現

指標**計算層皆正確且因果安全**（EMA/RSI/ATR/zscore/fib 用 `.shift(1)`、策略只讀已收盤根 `iloc[-2]`）。問題在**策略與風控層**：

1. **所有策略無條件出手** —— EMACross 盤整 whipsaw；ZScoreLongShort 空手只看 `|z|`、強趨勢逆勢接刀（最大破口）。
2. **風控用固定 % 停損停利，沒用到已算好的 ATR** —— 高波動被雜訊掃出、低波動設太寬。
3. **進場後 SL/TP 固定不動** —— 趨勢單無法保護浮盈。

## 硬約束（不可違反）

- 絕不引入 look-ahead / repaint：信號只用已收盤 K 線。新指標用 `.shift(1)` / rolling 過去窗，swing pivot 接受右側 `right` 根 lag。
- 誠實回測：含手續費、用收盤 bar、不偷看未來。
- testnet/虛擬資金，不提供切主網。
- 偏好低參數、有實證依據；以 walk-forward 防過擬合。

---

## 新增 causal 指標（core/signal_engineer.py）

| 函式 | 公式（皆只用過去與當根已收盤值） | 用途 |
|------|------|------|
| `adx(df, period=14)` | Wilder +DM/-DM/TR → RMA(ewm α=1/period) → +DI/-DI/DX → ADX | 趨勢強度 |
| `efficiency_ratio(close, period=14)` | `\|close-close.shift(p)\| / close.diff().abs().rolling(p).sum()`，0~1 | 趨勢/盤整 |
| `choppiness_index(df, period=14)` | `100*log10(ΣTR_p / (max_high_p - min_low_p)) / log10(p)`，>61.8 盤整、<38.2 趨勢 | 盤整度 |
| swing pivot（給 fib） | `high.shift(right+1).rolling(left+right+1).max()` 形式，右側 `right` 根確認後才生效 | 結構性高低點 |

全部沿用 `tests/test_signal_engineer.py` 的 prefix-invariance 慣例驗證因果性，並加手算驗證。

---

## 改動清單（依優先序）

### #1 🔴 Regime 閘門（共用層）
- `signal_engineer`: 新增 `adx/efficiency_ratio/choppiness_index`。
- `Strategy` 基類: 新增 `regime_pref`（`'trend'`/`'range'`/`'any'`）與 `_regime_ok(row)` helper。
- 各策略 `prepare()` 預算 `er/chop/adx/regime/regime_streak` 欄位（全 causal）。
- `signal()` 在「空手想開新倉」時先過 **ER+CHOP+ADX 2-of-3 多數決** 的 regime gate；與 `regime_pref` 不符 → 維持 0（只平不開）。加 `regime_confirm_bars` 去抖。
- 標記：EMACross→`'trend'`；ZScoreRevert/ZScoreLongShort/Fib→`'range'`。
- 契約不變（`prepare→signal(row,position)`），backtester/live 不需改。
- 參數: `er_period=14, er_trend=0.30, chop_period=14, chop_trend=38.2, adx_period=14, adx_trend=25, regime_confirm_bars=2`

### #2 🔴 ATR 動態停損取代固定 %
- `risk_officer.exit_levels(entry, direction, atr=None)`: 停損 = `entry ∓ atr_mult_sl*atr`，`atr=None` 時 fallback 回 `stop_loss_pct`。
- `risk_officer.check_entry(..., atr=None)`: 透傳 atr 給 `position_size` 的 stop_price → 自動波動度歸一化部位，仍受 `max_position_pct` 夾住。
- `backtester`: 進場處把 `row["atr"]` 傳入。
- 參數: `atr_mult_sl=2.0`（保留 `stop_loss_pct` 當 fallback）

### #3 🔴 停利改固定 R 倍數
- `exit_levels`: tp = `entry ± tp_R_mult*(atr_mult_sl*atr)`，fallback 回 `take_profit_pct`。
- 參數: `tp_R_mult=2.0`

### #4 🟡 RSI(50) 中線方向閘門
- EMACross: `rsi<rsi_max` → `rsi>50`（順勢確認；可並存放寬上限 80）。
- Fib 做空分支: `rsi>45` → `rsi<50`。
- 純常數，無新參數。

### #5 🟡 EMA 交叉緩衝帶
- EMACross: `bull = ema_fast>ema_slow` → `(ema_fast-ema_slow) > sep_atr_k*atr`；進出場不同門檻形成 hysteresis。
- 參數: `sep_atr_k=0.5`

### #6 🟡 Fib 順勢回調
- Fib `prepare()`: 加 `ema_trend = ema(close, 200)`。
- `signal()`: 只在 `close>ema_trend` 於支撐區做多、`close<ema_trend` 於阻力區做空（逆勢改順勢）。
- 參數: `ema_trend_period=200`

### #7 🟡 Fib swing pivot 取代固定 rolling
- `fib_retracement`: 高低點來源改為「已確認 swing pivot」（左右各 `right` 根，右側確認後生效）。
- 明文禁用標在 pivot 當根、會回溯改寫的 ZigZag 版本。
- 參數: `pivot_left=3, pivot_right=3`

### #8 🟡 Chandelier 追蹤停損
- `risk_officer`: 新增純函式 `update_trailing_stop(prev_stop, extreme_since_entry, atr, direction)`（多單只升不降、空單對稱）。
- `backtester`: 持倉段新增 `highest/lowest_since_entry`，每根用已收盤 high/low + atr 更新；本根用「上一根」算出的 stop 判定觸發。
- live 端只在收盤 bar 後推進（用 `iloc[-2]` 的 atr）。
- 參數: `chand_period=22, chand_mult=3.0`

---

## config.py 新增欄位

`atr_mult_sl=2.0, tp_R_mult=2.0, chand_mult=3.0` 並列既有 `stop_loss_pct/take_profit_pct`（fallback）。

## optimize.py

`RISK_KEYS` 加入 `atr_mult_sl, tp_R_mult, chand_mult`；walk-forward 網格 `atr_mult_sl∈{1.5,2.0,2.5}`、`tp_R_mult∈{1.5,2,3}`、`chand_mult∈{2.5,3.0}`，含手續費/收盤 bar，避免手挑單一倍數。

---

## 測試計畫（TDD）

1. 新指標 causality：prefix-invariance（`fn(series[:k])` 與 `full[:k]` 重疊區一致，NaN 位置也一致）。
2. ADX/ER/CHOP 手算驗證（小段 OHLC）；ER 對單調序列≈1、鋸齒≈0。
3. swing pivot 非重繪：pivot 只在右側 `right` 根後出現；末根擾動不改變較早 pivot；fib_pos prefix-invariance。
4. ATR 停損/R 停利手算 + `atr=None` 向後相容回歸 + direction=-1 對稱。
5. position_size: atr 變大 → qty 變小，且不超過 max_position_pct。
6. Chandelier: trailing 單調不降、回落觸及平倉、只依「上一根」stop（無 look-ahead），空單對稱。
7. regime gate 行為: 盤整段 EMACross 不進場、趨勢段 ZScoreLS 不逆勢開單；去抖需連續 confirm_bars。
8. 端到端因果回歸: backtester `test_no_lookahead_equity_prefix_invariant` 擴充後仍成立。
9. walk-forward: 新參數納入 GRID 後仍能跑、OOS 欄位齊全、min_trades 防呆生效。

---

## 實作後對抗式審查修正（11-agent review）

實作完成後跑了一輪對抗式審查（4 維度 reviewer × 逐條 verify），確認並修掉：

- **M-1（high）swing pivot 非嚴格比較**：`is_ph = high.shift(right) == rolling.max()` 在平台/盤整段會逐根誤標，`ffill` 把真正較高的 pivot 拉低、汙染 `fib_pos`（且 Fib 正好只在盤整盤運作）。改為**右側嚴格 `>` + 左側含等號 `>=`**，平台只標最後一根。補平台回歸測試（high/low 兩側）。
- **restore() trough 未初始化**：重啟還原空單時 `self.trough` 殘留 0 → Chandelier 把 SL 砸成 ≈3×ATR（空單會被次根誤平）。restore 已以 `entry_price` 為 peak/trough 起點；補 2 個離線回歸測試（壞版本 RED、修後 GREEN）。
- **S-1（medium）現貨實盤未接 ATR**：`run_live.py` / `run_live_ws.py` 補上 `check_entry`/`exit_levels` 的 `atr`（與 paper/futures/backtester 對齊；Chandelier trailing 仍僅 futures+backtester 有）。
- **N-1 restore-else 用 ATR**：新增 `_latest_atr()`，重建 SL/TP 改用 ATR。
- **N-2 暖機不足防呆**：futures 迴圈在 `dropna()` 後 <2 列時印訊息並跳過，不再靠例外吞 IndexError。
- **非問題（誤報，已查證）**：live 用 close、backtest 用 high/low 當 Chandelier 種子——皆 causal、非 look-ahead，僅首根 trailing 寬窄的細微回測/實盤分歧。

三大約束審查結論：**無 look-ahead（prefix-invariance 全綠）、誠實回測、testnet-only 均守住**。

## 不在範圍內（對抗式查核否決，附原因）

- **RSI 背離**：擺動點偵測天然 repaint/look-ahead。
- **多週期 RSI 確認**：讀進行中高週期 bar = 用未來資訊。
- **MACD histogram 動量確認**：與 12/26 EMA 主訊號高度共線、冗餘。
- **低延遲均線（HMA/DEMA/TEMA/KAMA）替換**：低延遲在盤整更吵、可能加劇 whipsaw；擴大搜尋面易事後挑選。
- **Hurst exponent regime**：非 battle-tested、估計自由度高、過擬合 high。已用 ER+CHOP+ADX 取代。
- **Golden Pocket 0.618–0.65 數值魔力**：勝率宣稱無公開樣本，ScienceDirect 實證 fib 水位與隨機無顯著差異。（0.786 僅作為可調的失效停損參數）
- **Connors RSI(2) 線性比例倉位**：需擴充 backtester 的分數倉位契約，屬較大改動，列後續。
