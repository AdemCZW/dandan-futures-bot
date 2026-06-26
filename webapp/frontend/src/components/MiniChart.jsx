import { useEffect, useRef } from 'react'
import { createChart, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'

// 各策略疊的技術線（讓使用者直接看出該技術的買賣參考位）。
//   fib_channel → 費波那契通道（0 原點 / 0.618 黃金 / 1.0 目標）= 均值回歸的頂底參考
//   smc_structure → EMA 快慢線（趨勢方向）
const OVERLAYS = {
  // 全套費波那契通道：0/1.0 錨點與 0.618 黃金線用粗實線，中間比率（0.236/0.382/0.5/0.786）
  // 用細虛線、灰階，既呈現完整 7 條結構又不會在小圖上太雜。
  fib_channel: [
    { key: 'fib_ch_0',   color: '#7f77dd', w: 2, style: 0 },
    { key: 'fib_ch_236', color: '#6e7681', w: 1, style: 2 },
    { key: 'fib_ch_382', color: '#6e7681', w: 1, style: 2 },
    { key: 'fib_ch_5',   color: '#8b949e', w: 1, style: 2 },
    { key: 'fib_ch_618', color: '#ffa657', w: 2, style: 0 },
    { key: 'fib_ch_786', color: '#6e7681', w: 1, style: 2 },
    { key: 'fib_ch_100', color: '#7f77dd', w: 2, style: 0 },
  ],
  smc_structure: [
    { key: 'ema_fast', color: '#ffa657', w: 1, style: 0 },
    { key: 'ema_slow', color: '#58a6ff', w: 1, style: 0 },
  ],
}
const DEFAULT_OVERLAY = [{ key: 'ema_trend', color: '#58a6ff', w: 1, style: 0 }]

/** 卡片內嵌的迷你 K 線圖：蠟燭 + 策略技術線 + 進場/SL/TP 價格線。
 *  靜態（不可拖曳縮放），每 60s 自動刷新一次 K 線。 */
export default function MiniChart({ symbol, interval, strategy, entry, sl, tp, inPosition, trades }) {
  const elRef         = useRef(null)
  const candleRef     = useRef(null)
  const priceLinesRef = useRef([])
  const markersRef    = useRef(null)         // createSeriesMarkers plugin
  const tradesRef     = useRef([])
  tradesRef.current = trades || []           // 最新成交供 load() 使用（避免 stale closure）

  // ── 建圖 + 載 K 線（symbol/interval/strategy 變才重建）─────────────────────
  useEffect(() => {
    if (!elRef.current || !symbol) return
    const chart = createChart(elRef.current, {
      autoSize: true,
      layout: { background: { color: 'transparent' }, textColor: '#8b949e',
                fontSize: 9, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: 'rgba(240,246,252,0.04)' } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: false, secondsVisible: false },
      crosshair: { mode: 0 },
      handleScroll: false, handleScale: false,
    })
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: '#3fb950', downColor: '#f85149',
      wickUpColor: '#3fb950', wickDownColor: '#f85149',
      borderVisible: false, priceLineVisible: false, lastValueVisible: false,
    })
    candleRef.current = candle
    const overlaySeries = []
    let cancelled = false

    const load = async () => {
      try {
        const d = await api.klines(symbol, interval, 60)
        if (cancelled || !d?.candles?.length) return
        candle.setData(d.candles)
        for (const s of overlaySeries.splice(0)) chart.removeSeries(s)
        for (const o of (OVERLAYS[strategy] || DEFAULT_OVERLAY)) {
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
          if (!sec || sec < lo - 60 || sec > hi + 60) continue   // 視窗外略過
          const t = snap(sec)
          const side = tr.side || ''
          if (side === 'entry')
            marks.push({ time: t, position: 'belowBar', color: '#3fb950', shape: 'arrowUp', text: '買' })
          else if (side === 'entry_short')
            marks.push({ time: t, position: 'aboveBar', color: '#f85149', shape: 'arrowDown', text: '空' })
          else if (side.startsWith('exit'))
            marks.push({ time: t, position: (tr.pnl ?? 0) >= 0 ? 'aboveBar' : 'belowBar',
                         color: '#8b949e', shape: 'circle', text: '平' })
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
  }, [symbol, interval, strategy])

  // ── 進場/SL/TP 價格線（持倉或數值變動時即時重畫）──────────────────────────
  useEffect(() => {
    const candle = candleRef.current
    if (!candle) return
    for (const pl of priceLinesRef.current) {
      try { candle.removePriceLine(pl) } catch { /* noop */ }
    }
    priceLinesRef.current = []
    if (!inPosition) return
    const add = (price, color, title) => {
      if (!price) return
      priceLinesRef.current.push(candle.createPriceLine({
        price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title,
      }))
    }
    add(entry, '#c9d1d9', '進')
    add(sl, '#f85149', 'SL')
    add(tp, '#3fb950', 'TP')
  }, [entry, sl, tp, inPosition])

  return <div ref={elRef} style={{ width: '100%', height: 132 }} />
}
