// 主題管理 + 圖表取色。
// lightweight-charts 畫在 canvas 上，無法吃 CSS var()，必須給實際色字串，
// 且主題切換時要能重新取色重畫 —— 這支檔集中處理。
import { useEffect, useState } from 'react'

const KEY = 'dd-theme'
const EVT = 'dd-theme-change'

export function getTheme() {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'
}

export function setTheme(t) {
  const next = t === 'light' ? 'light' : 'dark'
  document.documentElement.setAttribute('data-theme', next)
  try { localStorage.setItem(KEY, next) } catch { /* ignore */ }
  window.dispatchEvent(new CustomEvent(EVT, { detail: next }))
}

export function toggleTheme() {
  setTheme(getTheme() === 'light' ? 'dark' : 'light')
}

// 讀目前主題下的圖表用色（從 CSS 變數即時取，確保與 styles.css 一致）。
export function getChartColors() {
  const s = getComputedStyle(document.documentElement)
  const v = (name, fallback) => (s.getPropertyValue(name).trim() || fallback)
  return {
    bg:        v('--chart-bg', 'transparent'),
    grid:      v('--chart-grid', 'rgba(245,243,238,0.06)'),
    line:      v('--chart-line', '#5b8cff'),
    text:      v('--chart-text', '#9a988f'),
    border:    v('--chart-border', 'rgba(245,243,238,0.10)'),
    up:        v('--candle-up', '#4fb286'),
    down:      v('--candle-down', '#d8685f'),
    accent:    v('--accent', '#5b8cff'),
    pos:       v('--pos', '#4fb286'),
    neg:       v('--neg', '#d8685f'),
    warn:      v('--warn', '#d4a24e'),
    muted:     v('--muted', '#9a988f'),
    faint:     v('--faint', '#6a6862'),
    text2:     v('--text', '#f5f3ee'),
    bot1:      v('--bot1', '#5b8cff'),
    bot2:      v('--bot2', '#e07a9e'),
    bot3:      v('--bot3', '#b58ce0'),
    bot4:      v('--bot4', '#e0a458'),
  }
}

// React hook：回傳目前主題（'dark'|'light'），主題切換時自動重繪元件。
export function useTheme() {
  const [theme, setT] = useState(getTheme)
  useEffect(() => {
    const onChange = () => setT(getTheme())
    window.addEventListener(EVT, onChange)
    return () => window.removeEventListener(EVT, onChange)
  }, [])
  return theme
}
