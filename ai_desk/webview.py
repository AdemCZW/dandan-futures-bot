"""ai_desk 本地觀察後台 — 用瀏覽器看四角色辯論一步步跑、看/核准待辦提案。

只在本機跑（localhost），走訂閱 CLI，不部署、不碰現有 dashboard 與 bot。
啟動：
    .venv/bin/python -m ai_desk.webview          # 預設 http://127.0.0.1:8765
編排邏輯完全複用 ai_desk.desk.run_one_cycle（單一真相來源），本檔只做「顯示 + 觸發 + 核准」。
"""
from __future__ import annotations

import threading
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ai_desk.approval import ApprovalStore
from ai_desk.outcome import evaluate_outcome, summarize

DEFAULT_DB = "ai_desk_proposals.db"
ROLE_ORDER = ["analyst", "bull", "bear", "judge"]


def _thread_spawn(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _real_cycle(symbol: str, interval: str, on_progress, store_path: str):
    """真實一輪：抓幣安合約 K 線 → run_one_cycle（走訂閱 CLI）。延遲 import 避免測試碰網路。"""
    from binance.client import Client

    from config import Config
    from core.market_analyst import fetch_klines
    from core.risk_officer import RiskOfficer

    from ai_desk.desk import run_one_cycle
    from ai_desk.llm_client import ClaudeCliClient
    from ai_desk.memory import ThesisMemory
    from run_ai_desk_once import EQUITY_FOR_SIZING, MEMORY_DIR, prepare_df

    raw = fetch_klines(Client(), symbol, interval, limit=400, futures=True)
    df = prepare_df(raw)
    return run_one_cycle(
        df, symbol, interval,
        llm_call=ClaudeCliClient(),
        risk_officer=RiskOfficer(Config()),
        equity=EQUITY_FOR_SIZING,
        memory=ThesisMemory(MEMORY_DIR, symbol, interval),
        approval_store=ApprovalStore(store_path),
        on_progress=on_progress,
    )


def _real_klines(symbol: str, since: str):
    """抓提案成立後的 1h K 線，供結果結算用。延遲 import 避免測試碰網路。"""
    import pandas as pd
    from binance.client import Client

    from core.market_analyst import fetch_klines

    raw = fetch_klines(Client(), symbol, "1h", limit=1000, futures=True)
    t0 = pd.to_datetime(since).tz_localize(None)
    return raw[raw.index >= t0]


def create_app(*, store_path: str = DEFAULT_DB, cycle_fn=None, spawn=None,
               klines_fn=None) -> FastAPI:
    if cycle_fn is None:
        def cycle_fn(symbol, interval, on_progress):
            return _real_cycle(symbol, interval, on_progress, store_path)
    if spawn is None:
        spawn = _thread_spawn
    if klines_fn is None:
        klines_fn = _real_klines

    app = FastAPI(title="ai_desk 本地觀察後台")
    runs: dict[str, dict] = {}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE_HTML

    @app.post("/api/run")
    def start_run(body: dict | None = None) -> dict:
        body = body or {}
        symbol = (body.get("symbol") or "BTCUSDT").upper()
        interval = body.get("interval") or "4h"
        run_id = uuid.uuid4().hex[:8]
        runs[run_id] = {"status": "running", "symbol": symbol, "interval": interval,
                        "current": "analyst", "roles": {}, "proposal": None,
                        "risk": None, "proposal_id": None, "error": None}

        def work() -> None:
            def on_progress(role: str, text: str) -> None:
                st = runs[run_id]
                st["roles"][role] = text
                i = ROLE_ORDER.index(role)
                st["current"] = ROLE_ORDER[i + 1] if i + 1 < len(ROLE_ORDER) else None
            try:
                result = cycle_fn(symbol, interval, on_progress)
                p, rk = result.proposal, result.risk
                st = runs[run_id]
                st["proposal"] = {"direction": p.direction, "confidence": p.confidence,
                                  "entry": p.entry, "stop": p.stop,
                                  "take_profit": p.take_profit, "rationale": p.rationale}
                st["risk"] = {"allow": rk.allow, "quantity": rk.quantity, "reason": rk.reason}
                st["proposal_id"] = result.proposal_id
                st["status"] = "done"
                st["current"] = None
            except Exception as e:  # noqa: BLE001 — 顯示給使用者看，不吞
                st = runs[run_id]
                st["status"] = "error"
                st["error"] = f"{type(e).__name__}: {e}"
                st["current"] = None

        spawn(work)
        return {"run_id": run_id}

    @app.get("/api/run/{run_id}")
    def get_run(run_id: str) -> dict:
        st = runs.get(run_id)
        if st is None:
            raise HTTPException(status_code=404, detail="找不到這個 run")
        return st

    @app.get("/api/pending")
    def pending() -> list:
        return ApprovalStore(store_path).pending()

    @app.get("/api/outcomes")
    def outcomes() -> dict:
        """每筆提案的紙上結算 + 彙總。注意：ai_desk 從不執行，這是推演不是真實損益。"""
        rows = []
        for r in ApprovalStore(store_path).all():
            res = evaluate_outcome(direction=r["direction"], entry=r["entry"],
                                   stop=r["stop"], take_profit=r["take_profit"],
                                   qty=r["qty"],
                                   klines=klines_fn(r["symbol"], r["created_at"]))
            rows.append({**{k: r[k] for k in
                            ("id", "symbol", "direction", "confidence", "entry",
                             "stop", "take_profit", "qty", "status", "created_at",
                             "rationale")},
                         **res})
        return {"rows": rows, "summary": summarize(rows)}

    @app.post("/api/approve/{pid}")
    def approve(pid: int) -> dict:
        try:
            ApprovalStore(store_path).approve(pid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True}

    @app.post("/api/reject/{pid}")
    def reject(pid: int) -> dict:
        try:
            ApprovalStore(store_path).reject(pid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True}

    return app


PAGE_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ai_desk 觀察後台</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
         margin: 0; padding: 18px 24px;
         background: #0f1115; color: #e6e8eb; }
  h1 { font-size: 20px; margin: 4px 0 12px; }
  .muted { color: #8b93a1; font-size: 13px; }
  .bar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }
  input, button, select { font-size: 15px; padding: 8px 12px; border-radius: 8px;
         border: 1px solid #2a2f3a; background: #171a21; color: #e6e8eb; }
  input { width: 130px; }
  button { cursor: pointer; background: #2b6cff; border-color: #2b6cff; color: #fff; font-weight: 600; }
  button:disabled { opacity: .5; cursor: default; }
  button.ghost { background: transparent; color: #e6e8eb; }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; align-items: start; }
  @media (max-width: 1200px) { .grid { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 680px) { .grid { grid-template-columns: 1fr; } input { flex: 1; } }
  .card { background: #171a21; border: 1px solid #2a2f3a; border-radius: 12px; padding: 14px; }
  .card h3 { margin: 0 0 8px; font-size: 15px; display: flex; align-items: center; gap: 8px; }
  .role-body { white-space: pre-wrap; font-size: 13.5px; line-height: 1.55; color: #d3d7de;
         max-height: 62vh; overflow-y: auto; }
  .pending { color: #f2b04a; } .running { color: #4ac0f2; } .done { color: #4ad07a; }
  .dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .dot.wait { background: #3a4150; } .dot.run { background: #4ac0f2; animation: pulse 1s infinite; }
  .dot.ok { background: #4ad07a; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
  .proposal { margin-top: 14px; border-left: 3px solid #2b6cff; }
  .proposal.short { border-color: #f2555a; } .proposal.long { border-color: #4ad07a; }
  .proposal.flat { border-color: #8b93a1; }
  .kv { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 6px 0; font-size: 14px; }
  .kv b { color: #fff; }
  .queue-item { border: 1px solid #2a2f3a; border-radius: 10px; padding: 12px; margin-bottom: 10px; }
  .queue-item .acts { display: flex; gap: 8px; margin-top: 8px; }
  .approve { background: #2f9e57; border-color: #2f9e57; }
  .reject { background: #b3403f; border-color: #b3403f; }
  details summary { cursor: pointer; color: #8b93a1; font-size: 13px; margin-top: 6px; }
  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0 14px; }
  .stat { background: #171a21; border: 1px solid #2a2f3a; border-radius: 10px;
          padding: 10px 14px; min-width: 120px; }
  .stat .lab { font-size: 12px; color: #8b93a1; }
  .stat .val { font-size: 19px; font-weight: 700; margin-top: 2px; }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #2a2f3a; white-space: nowrap; }
  th { color: #8b93a1; font-weight: 600; font-size: 12.5px; }
  td.wrap { white-space: normal; color: #b9bfc9; font-size: 12.5px; }
  .tbl-wrap { overflow-x: auto; border: 1px solid #2a2f3a; border-radius: 12px; background: #171a21; }
  .st { padding: 2px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .st.stopped { background: #3a1c1e; color: #f2555a; }
  .st.target  { background: #17331f; color: #4ad07a; }
  .st.open    { background: #16303d; color: #4ac0f2; }
  .st.unfilled, .st.no_data { background: #24282f; color: #8b93a1; }
  .pos { color: #4ad07a; } .neg { color: #f2555a; }
  .warn-band { background: #2a2113; border: 1px solid #4a3a18; color: #e8c07a;
               border-radius: 10px; padding: 10px 14px; font-size: 13px; margin-bottom: 12px; }
  .err { color: #f2555a; }
  .note { font-size: 12px; color: #8b93a1; margin-top: 4px; }
</style>
</head>
<body>
  <h1>ai_desk 觀察後台 <span class="muted">四角色辯論 · 只提案不下單 · 走訂閱</span></h1>

  <div class="bar">
    <input id="symbol" value="BTCUSDT" placeholder="幣種">
    <select id="interval">
      <option>4h</option><option>1h</option><option>15m</option><option>1d</option>
    </select>
    <button id="runBtn" onclick="startRun()">跑一輪辯論</button>
    <span id="status" class="muted"></span>
  </div>
  <div class="note">按下後會叫本機 claude（訂閱額度）跑 4 次，約需 1–4 分鐘。過程一步步顯示於下方。</div>

  <div class="grid" id="roles" style="margin-top:14px">
    <div class="card"><h3><span class="dot wait" id="dot-analyst"></span>技術分析</h3><div class="role-body muted" id="body-analyst">尚未開始</div></div>
    <div class="card"><h3><span class="dot wait" id="dot-bull"></span>多方研究員</h3><div class="role-body muted" id="body-bull">尚未開始</div></div>
    <div class="card"><h3><span class="dot wait" id="dot-bear"></span>空方研究員</h3><div class="role-body muted" id="body-bear">尚未開始</div></div>
    <div class="card"><h3><span class="dot wait" id="dot-judge"></span>裁判</h3><div class="role-body muted" id="body-judge">尚未開始</div></div>
  </div>

  <div id="proposalBox"></div>

  <h1 style="margin-top:26px">待核准清單 <span class="muted">（核准/打槍目前只留紀錄，不會真的下單）</span></h1>
  <div id="queue"><div class="muted">載入中…</div></div>

  <h1 style="margin-top:26px">提案結果追蹤 <span class="muted">（前瞻樣本累積）</span></h1>
  <div class="warn-band">⚠️ 以下全部是<b>紙上推演</b>：ai_desk 只提案、從未真實下單。這是用真實 K 線回推「若當初照提案掛單會怎樣」，不代表帳戶真有這些損益。同一根同時觸及停損停利時保守算停損。</div>
  <div class="stats" id="stats"></div>
  <div class="tbl-wrap"><table id="outcomes">
    <thead><tr><th>#</th><th>標的</th><th>方向</th><th>狀態</th><th>紙上損益</th>
      <th>進場</th><th>停損</th><th>停利</th><th>信心</th><th>提案時間</th><th>審批</th></tr></thead>
    <tbody><tr><td colspan="11" class="muted">載入中…</td></tr></tbody>
  </table></div>
  <div class="note" id="outcomeNote"></div>

<script>
const ROLE_LABEL = {analyst:"技術分析", bull:"多方研究員", bear:"空方研究員", judge:"裁判"};
const ROLES = ["analyst","bull","bear","judge"];
let polling = null;

function stripJson(t){ return (t||"").replace(/```json[\\s\\S]*?```/g, "").trim(); }

async function startRun(){
  const btn = document.getElementById("runBtn");
  btn.disabled = true;
  document.getElementById("proposalBox").innerHTML = "";
  ROLES.forEach(r => {
    document.getElementById("body-"+r).textContent = "等待中…";
    document.getElementById("body-"+r).className = "role-body muted";
    document.getElementById("dot-"+r).className = "dot wait";
  });
  const symbol = document.getElementById("symbol").value.trim() || "BTCUSDT";
  const interval = document.getElementById("interval").value;
  setStatus("啟動中…", "running");
  const res = await fetch("/api/run", {method:"POST", headers:{"Content-Type":"application/json"},
                 body: JSON.stringify({symbol, interval})});
  const {run_id} = await res.json();
  if (polling) clearInterval(polling);
  polling = setInterval(() => poll(run_id), 1500);
  poll(run_id);
}

async function poll(run_id){
  const st = await (await fetch("/api/run/"+run_id)).json();
  ROLES.forEach(r => {
    const dot = document.getElementById("dot-"+r);
    const body = document.getElementById("body-"+r);
    if (st.roles[r] !== undefined){
      dot.className = "dot ok";
      body.textContent = stripJson(st.roles[r]);
      body.className = "role-body";
    } else if (st.current === r){
      dot.className = "dot run";
      body.textContent = "分析中…";
    }
  });
  if (st.status === "running"){
    setStatus("進行中：" + (ROLE_LABEL[st.current] || "…"), "running");
  } else if (st.status === "done"){
    clearInterval(polling); polling = null;
    setStatus("完成", "done");
    document.getElementById("runBtn").disabled = false;
    renderProposal(st);
    loadQueue();
    loadOutcomes();
  } else if (st.status === "error"){
    clearInterval(polling); polling = null;
    setStatus("出錯", "");
    document.getElementById("runBtn").disabled = false;
    document.getElementById("proposalBox").innerHTML =
      '<div class="card proposal"><b class="err">跑辯論失敗</b><div class="role-body err">'+
      escapeHtml(st.error||"")+'</div></div>';
  }
}

function renderProposal(st){
  const p = st.proposal, rk = st.risk;
  if (!p){ document.getElementById("proposalBox").innerHTML = ""; return; }
  const dirTxt = {"1":"做多","-1":"做空","0":"觀望"}[String(p.direction)] || "?";
  const cls = p.direction===1?"long":(p.direction===-1?"short":"flat");
  let riskLine = "";
  if (rk){
    riskLine = rk.allow
      ? '風控：<b style="color:#4ad07a">放行</b>　數量 '+rk.quantity.toFixed(6)+'　'+escapeHtml(rk.reason)
      : '風控：<b style="color:#f2b04a">未放行</b>　'+escapeHtml(rk.reason);
  }
  const priceLine = p.direction!==0
    ? '<div class="kv"><span>進場 <b>'+p.entry+'</b></span><span>停損 <b>'+p.stop+'</b></span><span>停利 <b>'+p.take_profit+'</b></span></div>'
    : "";
  document.getElementById("proposalBox").innerHTML =
    '<div class="card proposal '+cls+'">'+
      '<h3>裁判提案：'+dirTxt+'　<span class="muted">信心 '+p.confidence+'</span></h3>'+
      priceLine +
      '<div class="kv">依據：'+escapeHtml(p.rationale)+'</div>'+
      '<div class="kv muted">'+riskLine+'</div>'+
      (st.proposal_id ? '<div class="note">已進待核准佇列 #'+st.proposal_id+'</div>' : '')+
    '</div>';
}

async function loadQueue(){
  const rows = await (await fetch("/api/pending")).json();
  const box = document.getElementById("queue");
  if (!rows.length){ box.innerHTML = '<div class="muted">目前沒有待核准的提案。</div>'; return; }
  box.innerHTML = rows.map(r => {
    const dirTxt = {"1":"做多","-1":"做空"}[String(r.direction)] || "?";
    return '<div class="queue-item">'+
      '<div class="kv"><b>#'+r.id+'　'+r.symbol+'　'+dirTxt+'</b>'+
        '<span class="muted">信心 '+r.confidence+'　數量 '+Number(r.qty).toFixed(6)+'</span></div>'+
      '<div class="kv"><span>進場 <b>'+r.entry+'</b></span><span>停損 <b>'+r.stop+'</b></span><span>停利 <b>'+r.take_profit+'</b></span></div>'+
      '<div class="kv">依據：'+escapeHtml(r.rationale)+'</div>'+
      '<details><summary>看完整四角色辯論</summary><div class="role-body">'+escapeHtml(r.debate_full_text)+'</div></details>'+
      '<div class="acts"><button class="approve" onclick="decide('+r.id+',\\'approve\\')">核准</button>'+
        '<button class="reject" onclick="decide('+r.id+',\\'reject\\')">打槍</button></div>'+
    '</div>';
  }).join("");
}

async function decide(pid, action){
  const res = await fetch("/api/"+action+"/"+pid, {method:"POST"});
  if (!res.ok){ const e = await res.json(); alert("失敗：" + (e.detail||res.status)); }
  loadQueue(); loadOutcomes();
}

const STATE_TXT = {stopped:"停損", target:"停利", open:"持有中",
                   unfilled:"未成交", no_data:"尚無資料"};
const STATUS_TXT = {pending:"待核准", approved:"已核准", rejected:"已打槍", executed:"已執行"};

async function loadOutcomes(){
  const tb = document.querySelector("#outcomes tbody");
  const data = await (await fetch("/api/outcomes")).json();
  const s = data.summary;
  const money = v => (v>=0?"+":"") + v.toFixed(2);
  const cls = v => v>=0 ? "pos" : "neg";
  document.getElementById("stats").innerHTML = [
    ["總提案", s.total],
    ["已結算", s.closed],
    ["勝 / 敗", s.wins + " / " + s.losses],
    ["勝率", s.closed ? (s.win_rate*100).toFixed(0)+"%" : "—"],
    ["已實現(紙上)", '<span class="'+cls(s.realized_pnl)+'">'+money(s.realized_pnl)+'</span>'],
    ["未平倉(紙上)", '<span class="'+cls(s.open_pnl)+'">'+money(s.open_pnl)+'</span>'],
  ].map(([l,v]) => '<div class="stat"><div class="lab">'+l+'</div><div class="val">'+v+'</div></div>').join("");

  if (!data.rows.length){
    tb.innerHTML = '<tr><td colspan="11" class="muted">還沒有任何提案。</td></tr>';
    document.getElementById("outcomeNote").textContent = "";
    return;
  }
  const dirTxt = {"1":"做多","-1":"做空","0":"觀望"};
  tb.innerHTML = data.rows.map(r =>
    '<tr><td>#'+r.id+'</td><td>'+r.symbol+'</td><td>'+(dirTxt[String(r.direction)]||"?")+'</td>'+
    '<td><span class="st '+r.state+'">'+(STATE_TXT[r.state]||r.state)+'</span></td>'+
    '<td class="'+cls(r.pnl)+'">'+(r.state==="unfilled"||r.state==="no_data" ? "—" : money(r.pnl))+'</td>'+
    '<td>'+r.entry+'</td><td>'+r.stop+'</td><td>'+r.take_profit+'</td>'+
    '<td>'+r.confidence+'</td><td class="muted">'+(r.created_at||"").slice(0,16).replace("T"," ")+'</td>'+
    '<td class="muted">'+(STATUS_TXT[r.status]||r.status)+'</td></tr>'+
    '<tr><td></td><td colspan="10" class="wrap">依據：'+escapeHtml(r.rationale)+'</td></tr>'
  ).join("");
  document.getElementById("outcomeNote").textContent =
    "樣本數 " + s.closed + " 筆已結算 —— 依你系統一貫標準，要判斷有無 edge 需累積數十筆並算 bootstrap 信賴下界，目前遠遠不足。";
}

function setStatus(t, cls){ const s=document.getElementById("status"); s.textContent=t; s.className = cls||"muted"; }
function escapeHtml(s){ return (s||"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

loadQueue();
loadOutcomes();
</script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8765)
