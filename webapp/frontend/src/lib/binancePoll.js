// 幣安公開 API 客戶端輪詢的退避邏輯（2026-07-06）。
//
// Live.jsx / DualMa.jsx / Chart.jsx 都直接從瀏覽器每 2-3 秒打 fapi.binance.com，
// 原本收到 429(超量警告)/418(IP已被封) 還是照樣打下一輪——幣安文件：418 期間
// 持續違規會拉長禁令。退避規則：優先讀 Retry-After 標頭；沒有標頭時 429 用
// 60 秒、418 用 5 分鐘的保守預設（418 代表已經觸發封鎖，預設值故意比 429 長）。

const DEFAULT_BACKOFF_MS = { 429: 60_000, 418: 5 * 60_000 }

export function createBackoffState() {
  return { blockedUntil: 0 }
}

export function isBlocked(state, now) {
  return now < state.blockedUntil
}

export function recordRateLimit(state, response, now) {
  const header = response.headers?.get?.('Retry-After')
  const retryAfterMs = header != null ? Number(header) * 1000 : null
  const fallbackMs = DEFAULT_BACKOFF_MS[response.status] ?? DEFAULT_BACKOFF_MS[429]
  const backoffMs = Number.isFinite(retryAfterMs) && retryAfterMs > 0 ? retryAfterMs : fallbackMs
  state.blockedUntil = now + backoffMs
}

// 統一入口：封鎖中完全不發請求；收到 429/418 記錄退避、回 null；其餘照常回傳 json。
export async function fetchBinancePublic(url, state, { fetchImpl = fetch, now = Date.now } = {}) {
  const t = now()
  if (isBlocked(state, t)) return null
  const res = await fetchImpl(url)
  if (res.status === 429 || res.status === 418) {
    recordRateLimit(state, res, t)
    return null
  }
  return res.json()
}
