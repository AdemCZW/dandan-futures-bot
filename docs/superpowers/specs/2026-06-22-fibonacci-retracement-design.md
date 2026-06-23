# Fibonacci Retracement 策略設計

**日期**: 2026-06-22  
**範圍**: 新增斐波那契回調指標 + 策略，納入現有 6 角色架構

---

## 背景

現有策略：`ema_cross`、`zscore_revert`、`zscore_ls`。  
需求：加入以斐波那契 0.618 黃金比例為核心的進出場訊號。

---

## 指標層（`core/signal_engineer.py`）

新增函式 `fib_retracement(df, lookback=50)`，輸出以下欄位：

| 欄位 | 說明 |
|------|------|
| `fib_high` | 滾動 lookback 根的最高價（不含當根） |
| `fib_low` | 滾動 lookback 根的最低價（不含當根） |
| `fib_pos` | `(close - fib_low) / (fib_high - fib_low)`，0=在低點、1=在高點 |
| `fib_382` | `fib_low + 0.382 * (fib_high - fib_low)`（38.2% 水位）|
| `fib_618` | `fib_low + 0.618 * (fib_high - fib_low)`（61.8% 水位）|

規則：
- 使用 `.shift(1)` 確保 causal（不用到當根資料，無 look-ahead）
- `fib_high - fib_low == 0` 時，`fib_pos` 填 `NaN`（避免除零）

---

## 策略層（`core/quant_researcher.py`）

新增 `FibRetracementStrategy`：

```
name = "fib_retracement"
allow_short = True
defaults = {"lookback": 50, "rsi_period": 14}
```

### 進出場邏輯

| 條件 | 目標倉位 |
|------|---------|
| `fib_pos < 0.382` 且 `rsi < 55` | `+1`（做多）|
| `fib_pos > 0.618` 且 `rsi > 45` | `-1`（做空）|
| 持多（+1）且 `fib_pos > 0.55` | `0`（平多）|
| 持空（-1）且 `fib_pos < 0.45` | `0`（平空）|
| 其他 | 維持現有倉位 |

NaN guard：任何指標為 NaN 時回傳現有 position。

---

## 整合

- `enrich()` 呼叫 `fib_retracement()` 寫入標準 DataFrame
- `STRATEGIES` 字典加入 `"fib_retracement": FibRetracementStrategy`
- 前端 `/api/strategies` 自動揭露（無需改後端）

---

## 測試

- `tests/test_signal_engineer.py`：fib_pos 範圍 [0,1]、除零安全、causal 驗證
- `tests/test_strategies.py`：fib 策略做多/做空/平倉/NaN guard 各場景

---

## 不在範圍內

- Fibonacci 延伸（Extension）
- Fibonacci 時間帶
- 多時間框架 Fibonacci
