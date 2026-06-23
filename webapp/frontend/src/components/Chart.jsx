import { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, CrosshairMode, LineStyle, CandlestickSeries, LineSeries } from 'lightweight-charts'
import { api } from '../api'

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
const TFS = ['1h', '4h', '1d']

const OVERLAYS = [
  { key: 'supertrend_bull', label: 'ST 多頭', color: '#26a69a', width: 2, style: 0 },
  { key: 'supertrend_bear', label: 'ST 空頭', color: '#ef5350', width: 2, style: 0 },
  { key: 'ema_fast',        label: 'EMA 9',   color: '#f0a500', width: 1, style: 0 },
  { key: 'ema_slow',        label: 'EMA 21',  color: '#7b61ff', width: 1, style: 0 },
  { key: 'ema_trend',       label: 'EMA 200', color: '#5d9cec', width: 2, style: 0 },
  { key: 'donchian_upper',  label: 'DC 上軌', color: '#78909c', width: 1, style: 2 },
  { key: 'donchian_lower',  label: 'DC 下軌', color: '#78909c', width: 1, style: 2 },
]

const ALL_KEYS = OVERLAYS.map(o => o.key)
const initVis = Object.fromEntries(ALL_KEYS.map(k => [k, true]))

export default function Chart() {
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({})
  const dataRef = useRef({})

  const [symbol, setSymbol] = useState('BTCUSDT')
  const [tf, setTf] = useState('4h')
  const [loading, setLoading] = useState(false)
  const [errMsg, setErrMsg] = useState(null)
  const [vis, setVis] = useState(initVis)

  // create chart once
  useEffect(() => {
    const el = containerRef.current
    if (!el) return

    const chart = createChart(el, {
      layout: { background: { color: 'transparent' }, textColor: '#b0b8c8' },
      grid: { vertLines: { color: '#1e2535' }, horzLines: { color: '#1e2535' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#2a3045' },
      timeScale: { borderColor: '#2a3045', timeVisible: true, secondsVisible: false },
      width: el.clientWidth,
      height: 480,
    })

    seriesRef.current.candles = chart.addSeries(CandlestickSeries, {
      upColor: '#26a69a', downColor: '#ef5350',
      borderUpColor: '#26a69a', borderDownColor: '#ef5350',
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
    })

    OVERLAYS.forEach(({ key, color, width, style }) => {
      const s = chart.addSeries(LineSeries, {
        color, lineWidth: width, lineStyle: style,
        priceLineVisible: false, lastValueVisible: false,
        crosshairMarkerVisible: false,
      })
      s.setData([])
      seriesRef.current[key] = s
    })

    chartRef.current = chart

    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }))
    ro.observe(el)
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = {} }
  }, [])

  // fetch data when symbol / tf changes
  useEffect(() => {
    if (!chartRef.current) return
    setLoading(true)
    setErrMsg(null)
    api.klines(symbol, tf, 300)
      .then(d => {
        dataRef.current = d
        seriesRef.current.candles?.setData(d.candles ?? [])
        ALL_KEYS.forEach(k => {
          seriesRef.current[k]?.setData(vis[k] ? (d[k] ?? []) : [])
        })
        chartRef.current?.timeScale().fitContent()
        setLoading(false)
      })
      .catch(e => { setErrMsg(e.message); setLoading(false) })
  }, [symbol, tf]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = useCallback(key => {
    setVis(prev => {
      const next = { ...prev, [key]: !prev[key] }
      seriesRef.current[key]?.setData(next[key] ? (dataRef.current[key] ?? []) : [])
      return next
    })
  }, [])

  const btnSym = {
    padding: '4px 10px', borderRadius: 4, border: 'none',
    cursor: 'pointer', fontSize: 12, fontWeight: 600,
    fontFamily: 'var(--font-display)',
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {/* toolbar */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <div style={{ display: 'flex', gap: 4 }}>
          {SYMBOLS.map(s => (
            <button key={s} onClick={() => setSymbol(s)} style={{
              ...btnSym,
              background: symbol === s ? 'var(--accent)' : 'var(--panel2)',
              color: symbol === s ? '#fff' : 'var(--muted)',
            }}>{s.replace('USDT', '')}</button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 4 }}>
          {TFS.map(t => (
            <button key={t} onClick={() => setTf(t)} style={{
              ...btnSym,
              background: tf === t ? 'var(--accent)' : 'var(--panel2)',
              color: tf === t ? '#fff' : 'var(--muted)',
            }}>{t}</button>
          ))}
        </div>
        <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginLeft: 8 }}>
          {OVERLAYS.map(({ key, label, color }) => (
            <button key={key} onClick={() => toggle(key)} style={{
              padding: '3px 8px', borderRadius: 4, cursor: 'pointer',
              fontSize: 11, fontFamily: 'var(--font-display)',
              border: `1px solid ${vis[key] ? color : 'var(--muted)'}`,
              background: vis[key] ? `${color}22` : 'transparent',
              color: vis[key] ? color : 'var(--muted)',
            }}>{label}</button>
          ))}
        </div>
        {loading && <span style={{ color: 'var(--muted)', fontSize: 12, marginLeft: 'auto' }}>載入中…</span>}
      </div>

      {/* chart container */}
      <div ref={containerRef} style={{
        borderRadius: 8, overflow: 'hidden', minHeight: 480,
        border: '1px solid var(--border, #2a3045)',
        background: 'var(--panel, #0d1117)',
      }} />

      {errMsg && <div style={{ color: '#ef5350', fontSize: 12 }}>無法載入：{errMsg}</div>}
    </div>
  )
}
