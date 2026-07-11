// 後端 API client。開發時透過 vite proxy 把 /api 導到 uvicorn :8000。
const BASE = import.meta.env.VITE_API ?? ''

// 合併 bot 容器（多台命名空間端點）：前端直連省 dashboard 代理流量/CPU，
// GitHub Pages 部署時甚至不需要 dashboard 常駐。bot 端 GET 全開 CORS *。
export const BOT_BASE = import.meta.env.VITE_BOT_URL
  ?? 'https://dandan-futures-bot-production.up.railway.app'

// 本機開發（未設 VITE_BOT_URL）：bot 容器可能未開/已關，圖表資料改走本機後端
// /api/*（同一份 core.chart_data，vite proxy 到 :8000）。GitHub Pages 建置（有設
// VITE_BOT_URL）維持直連 bot 容器 /ma6 等，行為不變。
const HAS_BOT = !!import.meta.env.VITE_BOT_URL

// 平倉直連 token（僅 GitHub Pages 建置時由 GH Actions secret 注入）。
// 空字串 → closeBot 退回走 dashboard 代理（本機開發模式，dashboard 持 token 轉發）。
// 使用者已確認接受風險：testnet 虛擬倉，token 可隨時在 Railway 換掉作廢。
const CLOSE_TOKEN = import.meta.env.VITE_CLOSE_TOKEN ?? ''

async function getAbs(url) {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json()
}

async function postAbs(url, headers = {}) {
  const r = await fetch(url, { method: 'POST', headers })
  if (!r.ok) {
    const e = await r.json().catch(() => ({}))
    throw new Error(e.detail || e.msg || `HTTP ${r.status}`)
  }
  return r.json()
}

async function get(path) {
  const r = await fetch(`${BASE}${path}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return r.json()
}

async function post(path, body) {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!r.ok) {
    const e = await r.json().catch(() => ({}))
    throw new Error(e.detail || `HTTP ${r.status}`)
  }
  return r.json()
}

export const api = {
  strategies: () => get('/api/strategies'),
  backtest: (body) => post('/api/backtest', body),
  explain: (body) => post('/api/explain', body),
  optimize: (body) => post('/api/optimize', body),
  trades: (limit = 50, mode) =>
    get(`/api/trades?limit=${limit}${mode ? `&mode=${encodeURIComponent(mode)}` : ''}`),
  // N 台籃子：bot 容器直連（/bots 清單 + /{id}/live 各台 enrich 狀態）
  // 2026-07-05 清理：移除 live/live2/3/4/liveAll 舊四端點 client——目標服務已關閉合併
  bots: () => getAbs(`${BOT_BASE}/bots`),
  botLive: (id) => getAbs(`${BOT_BASE}/${id}/live`),
  // 該台近期成交（bot 直連，strategy+symbol 已在後端過濾好）
  botTrades: (id, limit = 100) => getAbs(`${BOT_BASE}/${id}/trades?limit=${limit}`),
  // 手動平倉（結算）：有 CLOSE_TOKEN（GH Pages 建置注入）→ 直連 bot；
  // 否則退回 dashboard 代理 /api/close/{botId}（本機開發，dashboard 持 token 轉發）。
  closeBot: (id) => CLOSE_TOKEN
    ? postAbs(`${BOT_BASE}/${id}/close`, { 'X-Close-Token': CLOSE_TOKEN })
    : post(`/api/close/${id}`, {}),
  // K 線 + 費波那契通道 + 交易標記：有 bot（GH Pages）直連容器；本機開發走後端 /api/*
  klines: (symbol = 'BTCUSDT', tf = '4h', limit = 300) => HAS_BOT
    ? getAbs(`${BOT_BASE}/klines?symbol=${symbol}&interval=${tf}&limit=${limit}`)
    : get(`/api/klines?symbol=${symbol}&interval=${tf}&limit=${limit}`),
  // 六線密集/發散（雙均線系統版面）：MA20/60/120 + EMA20/60/120 + 首次回踩訊號
  ma6: (symbol = 'BTCUSDT', tf = '4h', limit = 300) => HAS_BOT
    ? getAbs(`${BOT_BASE}/ma6?symbol=${symbol}&interval=${tf}&limit=${limit}`)
    : get(`/api/ma6?symbol=${symbol}&interval=${tf}&limit=${limit}`),
  tradeMarkers: (symbol = 'BTCUSDT', bucketHours = 6) => HAS_BOT
    ? getAbs(`${BOT_BASE}/markers?symbol=${symbol}&bucket_hours=${bucketHours}`)
    : get(`/api/trade-markers?symbol=${symbol}&bucket_hours=${bucketHours}`),
  price: (symbol = 'BTCUSDT') => get(`/api/price?symbol=${symbol}`),
}

export const pct = (x) => `${(x * 100).toFixed(2)}%`
export const cls = (x) => (x > 0 ? 'pos' : x < 0 ? 'neg' : '')
