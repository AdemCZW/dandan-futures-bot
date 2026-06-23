// 後端 API client。開發時透過 vite proxy 把 /api 導到 uvicorn :8000。
const BASE = import.meta.env.VITE_API ?? ''

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
  live: () => get('/api/live'),
  live2: () => get('/api/live2'),
  whales: (symbol = 'BTCUSDT', period = '5m', limit = 30) =>
    get(`/api/whales?symbol=${symbol}&period=${period}&limit=${limit}`),
  hlLeaderboard: (topN = 30) => get(`/api/hl-leaderboard?top_n=${topN}`),
  klines: (symbol = 'BTCUSDT', tf = '4h', limit = 300) =>
    get(`/api/klines?symbol=${symbol}&interval=${tf}&limit=${limit}`),
  price: (symbol = 'BTCUSDT') => get(`/api/price?symbol=${symbol}`),
  copytraders: (limit = 20) => get(`/api/copytraders?limit=${limit}`),
  copytraderPositions: (uid) => get(`/api/copytrader-positions?uid=${encodeURIComponent(uid)}`),
  largeTrades: (symbol = 'BTCUSDT', minUsdt = 500000, limit = 100) =>
    get(`/api/large-trades?symbol=${symbol}&min_usdt=${minUsdt}&limit=${limit}`),
}

export const pct = (x) => `${(x * 100).toFixed(2)}%`
export const cls = (x) => (x > 0 ? 'pos' : x < 0 ? 'neg' : '')
