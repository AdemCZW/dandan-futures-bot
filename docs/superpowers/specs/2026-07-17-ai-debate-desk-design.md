# AI 分析辯論台（ai_desk）設計文件

**日期**: 2026-07-17
**範圍**: 新增一個獨立的 LLM 驅動分析/決策子系統，與現有 `core/` 六角色確定性引擎並存，不修改、不取代任何現有角色。

---

## 背景與動機

現有 dandan 系統（`core/market_analyst.py` → `signal_engineer.py` → `quant_researcher.py` → `risk_officer.py` → `futures_execution_engineer.py` → `trade_journal.py`）是一套完全確定性的規則引擎：固定公式算指標、固定規則產生訊號。這套引擎已證實在 3 年 8 幣回測下多數規則策略無 edge（見 `docs/strategy_research_log.md`），其價值在於「可回測、可驗證、成本為零」，但本質上跟交易所自帶的條件單沒有根本差異。

使用者的核心訴求（原話）：「我需要一個 AI 會資料處理跟分析，還有技術數分析的下單過程，不是只是單純量化機器人，那跟普通的交易所其實沒啥區別」。

調研 OpenAlice（TypeScript，重用使用者本機已登入的 Claude Code/Codex CLI 做推理，AGPL-3.0）與 TradingAgents（Python + LangGraph，analyst→bull/bear 辯論→trader→risk 委員會的多角色架構，Apache-2.0）後的結論：兩者的**平台本身**都不值得整合（見 `OpenAlice評估報告.md`），但兩者共同指向一個 dandan 現在缺的東西——一層**真正由 LLM 做判斷、而非套公式**的分析層，且可透過角色化 prompt + 結構化辯論的方式做出來，不需要依賴任一平台。

本設計即：抽取「analyst→bull→bear→trader 辯論」這個概念，以及 OpenAlice「持久記憶」「人工核准才執行（Trading as Git）」兩個概念，做成一個新的、獨立的 Python 套件 `ai_desk/`，插在現有引擎的「訊號」與「執行」之間，作為建議來源，而非取代者。

---

## 不可退讓的原則（Non-negotiables）

以下五條貫穿整份設計，任何實作偏離都視為 bug：

1. **僅限 Binance Futures Testnet，虛擬資金** —— 與現有所有 bot 相同的基線，本系統不例外。
2. **AI 永遠只提案，不直接執行** —— `ai_desk` 的輸出是 `TradeProposal`，必須先後經過「風控官硬性規則」與「人工核准」兩道關卡，才能進入現有 `futures_execution_engineer.py`。
3. **風控官（`core/risk_officer.py`）的既有硬規則不可被繞過** —— AI 的信心分數再高，倉位大小、強制停損、每日虧損熔斷等既有確定性規則一律以現有 `RiskOfficer` 為準，不新增例外通道。
4. **禁止宣稱「歷史回測驗證」** —— LLM 推理無法誠實回測（成本＋訓練資料可能已包含歷史事件本身，構成 lookahead 污染）。驗證方式改為「只看未來」：正式上線後累積的真實測試網決策樣本，套用與其餘策略相同的 bootstrap 信賴下界方法論，不做、也不宣稱任何歷史回放驗證。
5. **與現有 9 個生產 bot 完全隔離** —— 新套件、新進入點腳本、（未來若需要）新的獨立 Railway service；不修改任何現有 bot 的程式碼、設定檔或排程。

---

## 架構總覽

```
① 資料層 [既有，不改]
   market_analyst (真實幣安合約 K 線) + signal_engineer (EMA/RSI/ATR/通道位置/回撤/背離等既有因果指標)
        ↓ 只把「算好的事實」餵給 AI 層，AI 層不碰原始 K 線
② 分析層 [新，每輪 4 次 LLM 呼叫；Phase 1 走訂閱 CLI，Phase 2 走 API]
   技術分析角色 → 客觀描述市場結構（不帶方向偏好）
   多方研究員角色 → 讀技術分析 + 歷史記憶 → 最強多方論點
   空方研究員角色 → 讀多方論點 → 直接反駁 + 最強空方論點
   交易員/裁判角色 → 綜合辯論 → 產出 TradeProposal
        ↓ 這是「提案」，不是指令
③ 記憶層 [新，借用 OpenAlice 的持久筆記概念]
   每個 symbol 一份 JSON Lines 檔，記錄過去 N 輪的判斷與後續結果
        ↓ 強迫下一輪面對「上次我說 X，結果發生了 Y」
④ 風控閘門 [既有 core/risk_officer.py，規則不變]
   確定性夾限：倉位上限、強制停損、熔斷 —— 無論 AI 信心多高都適用
        ↓
⑤ 人工核准閘門 [新，借用 OpenAlice「Trading as Git」概念]
   提案進入 pending 狀態 → 人工核准/拒絕 → 只有 approved 才能往下走
        ↓
⑥ 執行層 [既有 core/futures_execution_engineer.py，Testnet only]
        ↓
⑦ 驗證層 [既有 core/trade_journal.py + bootstrap 信賴下界]
   每筆決策連同完整辯論全文一起記錄 → 只做前瞻樣本累積，不做歷史回測宣稱
```

---

## 目錄與模組（新套件 `ai_desk/`）

放在與 `core/`、`backtest/`、`webapp/` 同層的新頂層目錄，不塞進 `core/`——因為這是概念上完全不同的子系統（LLM 推理 vs. 確定性規則），混在一起會破壞現有 `core/` 的「純規則引擎」邊界。

| 檔案 | 職責 | 依賴 |
|---|---|---|
| `ai_desk/briefing.py` | `build_market_briefing(df, symbol, interval) -> MarketBriefing` —— 把 `signal_engineer` 算好的欄位轉成給 LLM 讀的結構化摘要（純函式，不呼叫 LLM，不呼叫網路） | `core.signal_engineer` |
| `ai_desk/roles.py` | 四個角色函式，每個都是 `run_role(prompt_ctx, llm_call) -> ParsedRoleOutput`，`llm_call: Callable[[str], str]` 由外部注入 | 無（純邏輯 + 注入的呼叫器） |
| `ai_desk/llm_client.py` | LLM 呼叫器兩後端，同一 `__call__(prompt) -> str` 介面：`ClaudeCliClient`（訂閱 CLI，Phase 1 預設）+ `AnthropicLLMClient`（API，Phase 2） | `subprocess`（CLI）／`anthropic`（API，Phase 2 才需要） |
| `ai_desk/memory.py` | `ThesisMemory` —— 讀寫 `ai_desk/memory/{symbol}_{interval}.jsonl`，`load(n=5)` / `append(entry)` | 無 |
| `ai_desk/proposal.py` | `TradeProposal` dataclass + `clamp_with_risk_officer(proposal, risk_officer) -> RiskDecision` | `core.risk_officer.RiskOfficer` |
| `ai_desk/approval.py` | `ApprovalStore` —— SQLite 狀態機（pending/approved/rejected/executed），沿用 `trade_journal.py` 的 SQLite-fallback 慣例 | 無 |
| `ai_desk/desk.py` | `run_one_cycle(symbol, interval, deps) -> CycleResult` —— 把①～⑤串起來的編排函式，所有外部依賴（`llm_call`、`risk_officer`、`memory`、`approval_store`）皆可注入 | 以上全部 |
| `run_ai_desk_once.py`（repo 根目錄） | 獨立進入點腳本，仿 `run_once.py` 的「單輪跑完即退出」慣例，供人工手動執行或未來獨立 cron 使用；**完全不碰**現有 `run_once.py` / `run_multi_futures.py` | `ai_desk.desk` |

---

## 各層細節

### ① 資料/簡報層

`MarketBriefing`：一個 dataclass，欄位是已經算好、可讀的事實，例如 `current_price`、`ema_fast/slow`、`rsi`、`atr`、`fib_pos`、`channel_position`、`spread_divergence_state`（純文字如 "背離"/"無背離"），以及最近 N 根的簡短價格走勢描述。刻意**不**把原始 OHLCV 陣列丟給 LLM——避免 LLM 自己重新「算」指標（既不可靠也浪費 token），所有數字都是 `signal_engineer` 已驗證過的因果計算結果。

### ② 分析/辯論層

四個角色皆為「一次 LLM 呼叫 + 一次結構化解析」，呼叫本身透過 `llm_call` 注入以利測試（後端 CLI/API 皆可）：

- **技術分析角色**：輸入 `MarketBriefing`，輸出客觀市場結構描述（禁止在 prompt 層要求方向性結論）。
- **多方研究員**：輸入技術分析結果 + 最近 N 輪記憶，輸出「最強看多論點」（列點式）。
- **空方研究員**：輸入技術分析結果 + 多方論點 + 記憶，輸出「直接反駁多方論點 + 最強看空論點」。
- **交易員/裁判**：輸入以上全部，輸出最終 `TradeProposal`：`{direction: 1|-1|0, confidence: 0~1, entry, stop, take_profit, rationale}`。

**輸出契約**：每個角色的 prompt 要求 LLM 在回覆末尾附一段 fenced JSON block（結構化欄位），`roles.py` 的解析函式只解析這段 JSON，前面的自然語言論述整段原文保留供人工閱讀與記憶層存檔，但不參與程式邏輯。解析失敗（缺 JSON、缺必要欄位）→ 拋出明確錯誤，該輪直接判定為「提案失敗」，不會產生半殘的 proposal 進入下一層。

### ③ 記憶層

格式：`ai_desk/memory/{symbol}_{interval}.jsonl`，每行一筆：

```json
{"ts": "2026-07-17T08:00:00Z", "direction": 1, "confidence": 0.7, "rationale_summary": "...", "price_at_decision": 63500.0}
```

`ThesisMemory.load(n=5)` 讀最近 5 筆格式化成一段文字注入下一輪的多方/空方 prompt，讓模型能看到「上次我判斷 X，之後價格實際走勢」，形成弱形式的「記取教訓」（非模型訓練，純粹是 context 注入，與 OpenAlice 的持久筆記本質相同）。選擇 JSON Lines 而非 SQLite：這一層只需要「寫入＋讀最近 N 筆」兩個操作，JSON Lines 是能滿足需求的最簡單格式（YAGNI），核准/執行狀態機才需要 SQLite 的查詢與更新能力。

### ④ 風控閘門

`ai_desk/proposal.py` 不重新實作任何風控規則,而是把 `TradeProposal` 轉換成現有 `RiskOfficer.decide(...)` 期望的輸入格式，直接呼叫既有、已測試過的 `RiskOfficer`。若 AI 提案沒有停損價、或倉位超過既有上限,回傳的 `RiskDecision.allow=False` 或被夾到上限——這條路徑與現有規則策略走的是**同一個** `RiskOfficer` 實例/邏輯，不做重複實作。

### ⑤ 人工核准閘門

MVP 版本刻意做到最簡單、無需任何新基礎設施：

- `ApprovalStore`（SQLite，沿用 `trade_journal.py` 的連線慣例）存一張表 `ai_desk_proposals(id, symbol, ts, direction, confidence, entry, stop, tp, rationale_full, status, decided_at)`。
- 狀態機：`pending → approved | rejected`；只有 `approved` 才能被 `run_ai_desk_once.py` 的下一輪或一支獨立小腳本挑出來送進執行層，執行後轉 `executed`。
- 第一階段核准介面：一支 CLI 小工具（`python -m ai_desk.approval <id> approve|reject`），列印待核准清單供人工看完整辯論全文後決定。Telegram 介面留待第一階段跑出真實提案、確認 prompt 品質後再設計，明確排除在本次實作範圍。

### ⑥ 執行層

直接呼叫既有 `core/futures_execution_engineer.py`，與現有規則 bot 走同一支執行程式碼，僅限 Testnet 帳戶。不新增任何執行邏輯。

### ⑦ 驗證層

沿用既有 `core/trade_journal.py`，額外把該筆決策的完整辯論全文（四個角色的原始輸出）存進去,供人工事後檢視「AI 當時怎麼想的」。統計方法論延續現有的 bootstrap 信賴下界（`docs/strategy_research_log.md` 已用的方法),但只套用在**這個系統上線後累積的樣本**,明確不做、也不對外宣稱任何歷史回測結果——這點須在儀表板/報告任何呈現此系統成效的地方，用文字明確標註「僅前瞻驗證，非回測」。

---

## LLM 後端與成本

LLM 呼叫抽象成 `llm_call(prompt) -> str` 介面（依賴注入），有兩種可切換後端：

**Phase 1 預設 — 訂閱 CLI（`ClaudeCliClient`）**：仿 OpenAlice 的做法，把使用者本機已登入的 `claude` CLI（`claude -p`）當子程序呼叫，用其 **Claude Max 訂閱額度**，**不額外計費**。限制：只能在有登入的本機跑（不可上 Railway）、會吃訂閱的用量上限（跑多了排擠互動式 Claude Code 額度）、屬訂閱互動用途的灰色地帶（僅適合低頻手動試跑）。此後端不需要 `anthropic` SDK。

**Phase 2 排程 — API（`AnthropicLLMClient`）**：直接呼叫 Anthropic API，需一組獨立 `ANTHROPIC_API_KEY`，**按用量計費，與 Max 訂閱是分開的兩筆帳**；可在 Railway 等無登入伺服器跑。需新增 `anthropic>=0.40`，只進 `requirements-ai.txt`、不進 `requirements.txt`（避免動到 9 台生產 bot 的 Docker 映像）。

**成本**：Phase 1 走訂閱 = 零額外現金支出（只耗訂閱用量）。Phase 2 走 API 的估算（延續 `OpenAlice評估報告.md` 估法）：1 幣種 BTCUSDT、4h、每輪 4 次呼叫 × 每天 6 輪 ≈ 每天 24 次，中等 prompt 長度下每天數十美分至約 1 美元、每月約 20–30 美元；放大幣種／縮短週期／加辯論輪數會線性上升。實際數字待跑起來用真實帳單校準。

---

## 分階段導入（Rollout）

| 階段 | 內容 | 狀態 |
|---|---|---|
| Phase 0 | 本設計文件 | 本次交付 |
| Phase 1 | TDD 實作 `ai_desk/` 全部模組；本機手動執行 `run_ai_desk_once.py`（1 幣種、4h）；人工肉眼檢視辯論全文品質，尚未排程 | 下一步（implementation plan） |
| Phase 2 | Prompt/品質確認後，接上排程（本機 cron 或獨立 Railway service，與現有 9 bot 完全分離）；核准走 CLI 小工具 | 未來 |
| Phase 3 | 累積數週已核准+執行的測試網樣本，並通過 bootstrap 信賴下界後，才考慮针對明確界定的決策子集放寬人工核准 | 未來，明確排除在本次規劃之外 |

---

## 測試策略

延續現有 repo 的 TDD 慣例（每個新函式先寫失敗測試）。關鍵設計：**LLM 呼叫本身透過依賴注入隔離**，測試只驗證「膠水邏輯」，不驗證「LLM 推理品質好不好」（這是人工在 Phase 1 肉眼檢視的範疇，非自動化測試範疇）：

| 模組 | 測試重點 |
|---|---|
| `briefing.py` | 純函式，合成 OHLCV 資料驗證各欄位計算正確、NaN 安全 |
| `roles.py` | 注入假的 `llm_call`（回傳固定文字），驗證：(a) prompt 組裝有帶入正確的 context（記憶、前一角色輸出）；(b) 正常 JSON 區塊能正確解析；(c) 缺 JSON/缺欄位時明確拋錯,不靜默通過 |
| `memory.py` | 讀寫回合測試（round-trip）、`load(n)` 截斷正確、格式化輸出正確 |
| `proposal.py` | 用既有 `RiskOfficer` 測試 fixture，驗證：無停損的 AI 提案被拒絕/夾限、超額倉位被夾到上限 —— 與現有規則策略共用同一風控測試邏輯 |
| `approval.py` | 狀態機測試：pending→approved 才能被執行層挑出；不可重複核准；rejected 不會被執行 |
| `desk.py` | 整合測試，全部依賴注入假物件，驗證一輪完整跑下來會產出正確 gated 的 proposal（不呼叫任何真實網路/API） |

---

## 不在範圍內

- Telegram/儀表板 UI（Phase 2 才設計）
- 自動核准或放寬人工審批（Phase 3，明確排除）
- 多幣種/多週期同時上線（Phase 1 僅 BTCUSDT + 4h 試點）
- 對 `core/` 既有六角色引擎的任何修改
- 針對 LLM 推理品質的自動化測試或評分
- 歷史回測（設計上刻意不支援，見「不可退讓的原則」第 4 條）
