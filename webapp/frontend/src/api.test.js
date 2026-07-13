import { describe, it, expect } from 'vitest'
import { chartApiBase } from './api'

// 圖表資料(klines/ma6)不該依賴交易 bot 容器死活——bot 純粹是交易引擎，使用者停 bot
// 省 Railway 費用時，圖表(K線/均線/訊號)應該還能看。2026-07-13 實測發現：GitHub Pages
// 公開頁面原本直連 bot 容器拿圖表資料，bot 一停，圖表整條資料鏈斷掉(Failed to fetch)。
// 修法：圖表資料改固定打 dandan-dashboard(純讀取、sleepApplication 自動休眠，不含任何
// 交易邏輯)，只有 bot 專屬的即時持倉/手動平倉才維持直連 bot。
describe('chartApiBase — 圖表資料 base：公開建置固定打 dashboard，本機開發用相對路徑', () => {
  it('公開建置(GitHub Pages)：回傳 dashboard 絕對網址', () => {
    expect(chartApiBase(true, 'https://dandan-dashboard-production.up.railway.app'))
      .toBe('https://dandan-dashboard-production.up.railway.app')
  })

  it('本機開發：回傳空字串（走 vite proxy 到本地 /api/*，不管 dashboard 網址是什麼）', () => {
    expect(chartApiBase(false, 'https://dandan-dashboard-production.up.railway.app')).toBe('')
  })
})
