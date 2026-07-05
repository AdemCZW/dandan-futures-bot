import { describe, it, expect, vi } from 'vitest'
import { createBackoffState, isBlocked, recordRateLimit, fetchBinancePublic } from './binancePoll'

// 幣安公開 API 429(超量警告)/418(IP被封) 的退避邏輯：前端有 3 處（Live.jsx 現價輪詢、
// DualMa.jsx / Chart.jsx K線輪詢）直接每 2-3 秒打 fapi.binance.com，原本完全沒有退避，
// 收到 429/418 還是照樣打，會讓封鎖持續延長（幣安文件：418 期間持續違規會拉長禁令）。

describe('createBackoffState / isBlocked — 初始狀態不封鎖', () => {
  it('剛建立時不封鎖', () => {
    const state = createBackoffState()
    expect(isBlocked(state, 1000)).toBe(false)
  })
})

describe('recordRateLimit — 讀 Retry-After 標頭決定退避多久', () => {
  it('有 Retry-After 標頭時，依標頭秒數封鎖，到期後解除', () => {
    const state = createBackoffState()
    const res = { status: 429, headers: { get: (k) => (k === 'Retry-After' ? '30' : null) } }
    recordRateLimit(state, res, 1000)
    expect(isBlocked(state, 1000)).toBe(true)
    expect(isBlocked(state, 1000 + 29_999)).toBe(true)
    expect(isBlocked(state, 1000 + 30_000)).toBe(false)
  })

  it('沒有 Retry-After 標頭時，429 用預設退避時間', () => {
    const state = createBackoffState()
    const res = { status: 429, headers: { get: () => null } }
    recordRateLimit(state, res, 1000)
    expect(isBlocked(state, 1000)).toBe(true)
    expect(isBlocked(state, 1000 + 60_000)).toBe(false)
  })

  it('418（IP已被封）沒有標頭時，退避時間比 429 長（避免延長封鎖）', () => {
    const state429 = createBackoffState()
    recordRateLimit(state429, { status: 429, headers: { get: () => null } }, 1000)
    const state418 = createBackoffState()
    recordRateLimit(state418, { status: 418, headers: { get: () => null } }, 1000)
    // 429 已經解除的時間點，418 應該還在封鎖中
    const t = 1000 + 60_000
    expect(isBlocked(state429, t)).toBe(false)
    expect(isBlocked(state418, t)).toBe(true)
  })
})

describe('fetchBinancePublic — 封鎖期間完全不發請求', () => {
  it('封鎖中：不呼叫 fetchImpl，直接回 null', async () => {
    const state = createBackoffState()
    recordRateLimit(state, { status: 418, headers: { get: () => '999' } }, 1000)
    const fetchImpl = vi.fn()
    const result = await fetchBinancePublic('https://fapi.binance.com/x', state, { fetchImpl, now: () => 1500 })
    expect(fetchImpl).not.toHaveBeenCalled()
    expect(result).toBeNull()
  })

  it('未封鎖且回應 200：回傳解析後的 json', async () => {
    const state = createBackoffState()
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: { get: () => null }, json: async () => ({ price: '1.23' }),
    })
    const result = await fetchBinancePublic('https://fapi.binance.com/x', state, { fetchImpl, now: () => 1000 })
    expect(result).toEqual({ price: '1.23' })
  })

  it('回應 429：記錄退避並回 null（下一次呼叫不再打）', async () => {
    const state = createBackoffState()
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false, status: 429, headers: { get: (k) => (k === 'Retry-After' ? '10' : null) },
    })
    const result = await fetchBinancePublic('https://fapi.binance.com/x', state, { fetchImpl, now: () => 1000 })
    expect(result).toBeNull()
    expect(isBlocked(state, 1000)).toBe(true)

    const result2 = await fetchBinancePublic('https://fapi.binance.com/x', state, { fetchImpl, now: () => 1005 })
    expect(result2).toBeNull()
    expect(fetchImpl).toHaveBeenCalledTimes(1)   // 第二次因為封鎖中，沒有再打
  })
})
