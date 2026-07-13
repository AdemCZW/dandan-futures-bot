import { describe, it, expect } from 'vitest'
import { shouldLabelRetrLevel, retrLabel } from './DualMa.jsx'

// 2026-07-13：手機窄螢幕(375px)實測——雙均線頁面回撤層 7 條線全部掛價格軸標籤，
// 加上通道零軸/一軸+現價，擠成一團互相蓋住看不清楚。修法：窄螢幕只留 0/1(錨點)
// 兩條標籤，中間 0.236~0.786 這 5 條細節線仍畫出來（保留視覺結構），但不掛標籤；
// 有掛標籤的也縮短文字（說明已經在上面圖例欄，不用每條線重複長句）。
describe('shouldLabelRetrLevel — 窄螢幕只留錨點(0/1)標籤，內層不掛標籤', () => {
  it('寬螢幕：任何比率都掛標籤', () => {
    expect(shouldLabelRetrLevel(0, false)).toBe(true)
    expect(shouldLabelRetrLevel(0.382, false)).toBe(true)
    expect(shouldLabelRetrLevel(1, false)).toBe(true)
  })

  it('窄螢幕：只有錨點(0/1)掛標籤', () => {
    expect(shouldLabelRetrLevel(0, true)).toBe(true)
    expect(shouldLabelRetrLevel(1, true)).toBe(true)
  })

  it('窄螢幕：內層比率(0.236~0.786)不掛標籤', () => {
    for (const r of [0.236, 0.382, 0.5, 0.618, 0.786]) {
      expect(shouldLabelRetrLevel(r, true)).toBe(false)
    }
  })
})

describe('retrLabel — 窄螢幕縮短文字（說明已在圖例欄，不用重複）', () => {
  it('寬螢幕：維持完整說明文字', () => {
    expect(retrLabel(0, -1, false)).toBe('回撤0＝波段高點')
    expect(retrLabel(1, -1, false)).toBe('回撤1＝波段低點')
    expect(retrLabel(0.382, -1, false)).toBe('回撤 0.382')
  })

  it('窄螢幕：只留「回撤N」，不重複完整說明', () => {
    expect(retrLabel(0, -1, true)).toBe('回撤0')
    expect(retrLabel(1, -1, true)).toBe('回撤1')
    expect(retrLabel(0.382, -1, true)).toBe('回撤0.382')
  })
})
