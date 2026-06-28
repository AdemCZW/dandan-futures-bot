import { useEffect, useRef, useState } from 'react'
import { createChart, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'
import { getChartColors, useTheme } from '../lib/theme.js'

// 各策略疊的技術線（讓使用者直接看出該技術的買賣參考位）。
//   fib_channel → 費波那契通道（0 原點 / 0.618 黃金 / 1.0 目標）= 均值回歸的頂底參考
//   smc_structure → EMA 快慢線（趨勢方向）
// 顏色須在 effect 內由 getChartColors() 即時取（canvas 無法吃 CSS var），
// 故以函式接收主題色物件 c 再回傳 overlay 設定，主題切換時重畫即換色。
const buildOverlays = (c) => ({
  // 全套費波那契通道：0/1.0 錨點與 0.618 黃金線用粗實線，中間比率（0.236/0.382/0.5/0.786）
  // 用細虛線、灰階，既呈現完整 7 條結構又不會在小圖上太雜。
  fib_channel: [
    { key: 'fib_ch_0',   color: c.bot3,  w: 2, style: 0 },
    { key: 'fib_ch_236', color: c.faint, w: 1, style: 2 },
    { key: 'fib_ch_382', color: c.faint, w: 1, style: 2 },
    { key: 'fib_ch_5',   color: c.muted, w: 1, style: 2 },
    { key: 'fib_ch_618', color: c.bot4,  w: 2, style: 0 },
    { key: 'fib_ch_786', color: c.faint, w: 1, style: 2 },
    { key: 'fib_ch_100', color: c.bot3,  w: 2, style: 0 },
  ],
  smc_structure: [
    { key: 'ema_fast', color: c.bot4, w: 1, style: 0 },
    { key: 'ema_slow', color: c.bot1, w: 1, style: 0 },
  ],
  // trend_pullback：EMA200 主方向（粗藍）+ 快/慢線（圖表用 9/21 當短線動能 proxy）
  trend_pullback: [
    { key: 'ema_trend', color: c.bot1, w: 2, style: 0 },
    { key: 'ema_slow',  color: c.bot3, w: 1, style: 0 },
    { key: 'ema_fast',  color: c.bot4, w: 1, style: 0 },
  ],
})
const buildDefaultOverlay = (c) => [{ key: 'ema_trend', color: c.bot1, w: 1, style: 0 }]

const TIMEFRAMES = ['5m', '15m', '1h', '4h']

/** 卡片內嵌的迷你 K 線圖：蠟燭 + 策略技術線 + 進場/SL/TP 價格線。
 *  可切換時間框架（5m/15m/1h/4h），預設 = 機器人本身的週期。
 *  靜態（不可拖曳縮放），每 60s 自動刷新一次 K 線。 */
export default function MiniChart({ symbol, interval, strategy, entry, sl, tp, inPosition, trades }) {
  const elRef         = useRef(null)
  const candleRef     = useRef(null)
  const priceLinesRef = useRef([])
  const markersRef    = useRef(null)         // createSeriesMarkers plugin
  const tradesRef     = useRef([])
  tradesRef.current = trades || []           // 最新成交供 load() 使用（避免 stale closure）

  const theme = useTheme()                    // 'dark'|'light'；切換主題時觸發重繪

  // 顯示用時間框架（預設跟隨機器人週期；機器人週期變動時重設）。
  const [tf, setTf] = useState(interval || '15m')
  useEffect(() => { setTf(interval || '15m') }, [interval])

  // 拖曳調整高度（130–600px）。
  const [chartH, setChartH] = useState(220)
  const onDragStart = (e) => {
    e.preventDefault()
    const startY = e.clientY
    const startH = chartH
    const onMove = (ev) => {
      setChartH(Math.max(130, Math.min(600, startH + ev.clientY - startY)))
    }
    const onUp = () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
  }

  // ── 建圖 + 載 K 線（symbol/tf/strategy 變才重建）─────────────────────────────
  useEffect(() => {
    if (!elRef.current || !symbol) return
    const c = getChartColors()                 // 即時取目前主題色（canvas 不吃 CSS var）
    const chart = createChart(elRef.current, {
      autoSize: true,
      layout: { background: { color: c.bg }, textColor: c.text,
                fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: c.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
      handleScroll: false, handleScale: false,
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: c.up, downColor: c.down,
      wickUpColor: c.up, wickDownColor: c.down,
      borderVisible: false, priceLineVisible: false, lastValueVisible: false,
    })
    candleRef.current = candle
    const overlays = buildOverlays(c)
    const defaultOverlay = buildDefaultOverlay(c)
    const overlaySeries = []
    let cancelled = false

    const load = async () => {
      try {
        const d = await api.klines(symbol, tf, 60)
        if (cancelled || !d?.candles?.length) return
        candle.setData(d.candles)
        for (const s of overlaySeries.splice(0)) chart.removeSeries(s)
        for (const o of (overlays[strategy] || defaultOverlay)) {
          const arr = (d[o.key] || []).filter(p => p && p.value != null)
          if (!arr.length) continue
          const ls = chart.addSeries(LineSeries, {
            color: o.color, lineWidth: o.w, lineStyle: o.style,
            priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
          })
          ls.setData(arr)
          overlaySeries.push(ls)
        }
        // 進出場標記：把成交時間吸附到最近的 K 棒，標在蠟燭上
        const ctimes = d.candles.map(c => c.time)
        const lo = ctimes[0], hi = ctimes[ctimes.length - 1]
        const snap = (sec) => {
          let best = lo, bd = Infinity
          for (const t of ctimes) { const dd = Math.abs(t - sec); if (dd < bd) { bd = dd; best = t } }
          return best
        }
        const marks = []
        for (const tr of tradesRef.current) {
          const sec = Math.floor(new Date(String(tr.ts).replace(' ', 'T') + 'Z').getTime() / 1000)
          if (!sec || sec < lo - 60 || sec > hi + 60) continue
          const t = snap(sec)
          const side = tr.side || ''
          const px = tr.price != null ? Number(tr.price).toFixed(2) : ''
          const pnl = tr.pnl != null ? Number(tr.pnl) : null
          if (side === 'entry')
            marks.push({ time: t, position: 'belowBar', color: c.pos, shape: 'arrowUp',
                         text: px ? `買 ${px}` : '買', size: 2 })
          else if (side === 'entry_short')
            marks.push({ time: t, position: 'aboveBar', color: c.neg, shape: 'arrowDown',
                         text: px ? `空 ${px}` : '空', size: 2 })
          else if (side === 'scale_out') {
            const ps = pnl != null ? (pnl >= 0 ? ` +${pnl.toFixed(2)}` : ` ${pnl.toFixed(2)}`) : ''
            marks.push({ time: t, position: 'aboveBar', color: c.accent, shape: 'square',
                         text: `部分${ps}`, size: 1.5 })
          } else if (side.startsWith('exit')) {
            const profit = pnl != null && pnl > 0.005
            const loss   = pnl != null && pnl < -0.005
            const label  = pnl != null
              ? (profit ? `+${pnl.toFixed(2)}` : loss ? `${pnl.toFixed(2)}` : '平')
              : '平'
            marks.push({ time: t,
                         position: loss ? 'belowBar' : 'aboveBar',
                         color: profit ? c.pos : loss ? c.neg : c.muted,
                         shape: 'circle', text: label, size: 1.5 })
          }
        }
        marks.sort((a, b) => a.time - b.time)
        if (markersRef.current) markersRef.current.setMarkers(marks)
        else markersRef.current = createSeriesMarkers(candle, marks)
        chart.timeScale().fitContent()
      } catch { /* 網路/資料異常時靜默，維持空圖 */ }
    }
    load()
    const timer = setInterval(load, 60000)
    return () => {
      cancelled = true
      clearInterval(timer)
      candleRef.current = null
      priceLinesRef.current = []
      markersRef.current = null
      chart.remove()
    }
  }, [symbol, tf, strategy, theme])         // theme 變動時整圖重建以套用新色

  // ── 進場/SL/TP 價格線（持倉或數值變動時即時重畫）──────────────────────────
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    for (const pl of priceLinesRef.current) {
      try { candle.removePriceLine(pl) } catch { /* noop */ }
    }
    priceLinesRef.current = []
    if (!inPosition) return
    const c = getChartColors()                 // 即時取主題色（canvas 不吃 CSS var）
    const add = (price, color, label, lw = 1) => {
      if (!price) return
      const title = `${label} ${Number(price).toFixed(2)}`
      priceLinesRef.current.push(candle.createPriceLine({
        price, color, lineWidth: lw, lineStyle: 2, axisLabelVisible: true, title,
      }))
    }
    add(entry, c.muted, '進', 1)
    add(sl,    c.neg,   'SL',  2)
    add(tp,    c.pos,   'TP',  2)
  }, [entry, sl, tp, inPosition, tf, theme])  // theme 變動時重畫價格線換色

  return (
    <div>
      {/* 時間框架切換 */}
      <div style={{ display: 'flex', gap: 4, marginBottom: 4 }}>
        {TIMEFRAMES.map((t) => {
          const on = t === tf
          return (
            <button key={t} onClick={() => setTf(t)}
              style={{
                fontSize: 10, padding: '2px 8px', borderRadius: 4, cursor: 'pointer',
                fontFamily: 'var(--font-display)', lineHeight: 1.4,
                border: on ? '1px solid var(--accent)' : '1px solid var(--line-strong)',
                background: on ? 'var(--accent-soft)' : 'transparent',
                color: on ? 'var(--accent)' : 'var(--muted)',
              }}>
              {t}{t === interval ? '·本' : ''}
            </button>
          )
        })}
      </div>
      <div ref={elRef} style={{ width: '100%', height: chartH }} />
      {/* 拖曳調整高度的把手 */}
      <div
        onMouseDown={onDragStart}
        style={{
          height: 8, cursor: 'ns-resize', userSelect: 'none',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          borderRadius: '0 0 4px 4px',
          background: 'var(--surface-2)',
          marginTop: 2,
        }}
        title="拖曳調整圖表高度"
      >
        <span style={{ color: 'var(--muted)', fontSize: 10, letterSpacing: 2 }}>⠿</span>
      </div>
    </div>
  )
}
