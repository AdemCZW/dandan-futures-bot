import { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, CrosshairMode, CandlestickSeries, LineSeries } from 'lightweight-charts'
import { api } from '../api'

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
const TFS = ['1h', '4h', '1d']
const AUTO_OPTS = [
  { label: '關閉', sec: 0 },
  { label: '3s',   sec: 3 },
  { label: '10s',  sec: 10 },
  { label: '30s',  sec: 30 },
]

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
  const [lastTs, setLastTs] = useState(null)
  const [livePrice, setLivePrice] = useState(null)
  const [autoSec, setAutoSec] = useState(3)
  const [countdown, setCountdown] = useState(0)
  const [refresh, setRefresh] = useState(0)

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
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      })
      s.setData([])
      seriesRef.current[key] = s
    })
    chartRef.current = chart
    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }))
    ro.observe(el)
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = {} }
  }, [])

  // full reload when symbol / tf / manual refresh changes
  useEffect(() => {
    if (!chartRef.current) return
    setLoading(true)
    setErrMsg(null)
    api.klines(symbol, tf, 300)
      .then(d => {
        dataRef.current = d
        seriesRef.current.candles?.setData(d.candles ?? [])
        ALL_KEYS.forEach(k => seriesRef.current[k]?.setData(vis[k] ? (d[k] ?? []) : []))
        chartRef.current?.timeScale().scrollToRealTime()
        const last = d.candles?.[d.candles.length - 1]
        if (last) { setLastTs(last.time); setLivePrice(last.close) }
        setLoading(false)
      })
      .catch(e => { setErrMsg(e.message); setLoading(false) })
  }, [symbol, tf, refresh]) // eslint-disable-line react-hooks/exhaustive-deps

  // auto-update: poll mark price every autoSec seconds
  useEffect(() => {
    if (autoSec === 0) { setCountdown(0); return }
    setCountdown(autoSec)
    const tick = () => {
      api.price(symbol)
        .then(d => {
          if (!d.price) return
          setLivePrice(d.price)
          const candles = dataRef.current.candles
          if (!candles?.length) return
          const last = candles[candles.length - 1]
          const updated = {
            time: last.time,
            open: last.open,
            high: Math.max(last.high, d.price),
            low: Math.min(last.low, d.price),
            close: d.price,
          }
          seriesRef.current.candles?.update(updated)
          // update in-memory so next tick uses latest high/low
          dataRef.current.candles[candles.length - 1] = updated
          setLastTs(d.ts)
        })
        .catch(() => {})
    }

    const ivPrice = setInterval(tick, autoSec * 1000)

    // countdown display (1-second tick)
    let cd = autoSec
    const ivCount = setInterval(() => {
      cd -= 1
      if (cd <= 0) cd = autoSec
      setCountdown(cd)
    }, 1000)

    return () => { clearInterval(ivPrice); clearInterval(ivCount) }
  }, [autoSec, symbol]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = useCallback(key => {
    setVis(prev => {
      const next = { ...prev, [key]: !prev[key] }
      seriesRef.current[key]?.setData(next[key] ? (dataRef.current[key] ?? []) : [])
      return next
    })
  }, [])

  const btn = {
    padding: '4px 10px', borderRadius: 4, border: 'none',
    cursor: 'pointer', fontSize: 12, fontWeight: 600, fontFamily: 'var(--font-display)',
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>

      {/* live price bar */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 26, fontWeight: 700, fontFamily: 'var(--font-display)', color: '#e8eaf6' }}>
          {livePrice != null ? livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—'}
        </span>
        <span style={{ fontSize: 12, color: 'var(--muted)', fontFamily: 'var(--font-display)' }}>USDT</span>
        {autoSec > 0 && (
          <span style={{ fontSize: 11, color: '#26a69a', fontFamily: 'var(--font-display)' }}>
            ● 即時  {countdown}s 後更新
          </span>
        )}
        {lastTs && (
          <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 'auto', fontFamily: 'var(--font-display)' }}>
            K棒：{new Date(lastTs * 1000).toLocaleString('zh-TW', { month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' })}
          </span>
        )}
      </div>

      {/* toolbar */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        {/* symbol */}
        <div style={{ display: 'flex', gap: 4 }}>
          {SYMBOLS.map(s => (
            <button key={s} onClick={() => setSymbol(s)} style={{
              ...btn, background: symbol === s ? 'var(--accent)' : 'var(--panel2)',
              color: symbol === s ? '#fff' : 'var(--muted)',
            }}>{s.replace('USDT', '')}</button>
          ))}
        </div>
        {/* timeframe */}
        <div style={{ display: 'flex', gap: 4 }}>
          {TFS.map(t => (
            <button key={t} onClick={() => setTf(t)} style={{
              ...btn, background: tf === t ? 'var(--accent)' : 'var(--panel2)',
              color: tf === t ? '#fff' : 'var(--muted)',
            }}>{t}</button>
          ))}
        </div>
        {/* auto-refresh */}
        <div style={{ display: 'flex', gap: 4, marginLeft: 4 }}>
          {AUTO_OPTS.map(o => (
            <button key={o.sec} onClick={() => setAutoSec(o.sec)} style={{
              ...btn, fontSize: 11,
              background: autoSec === o.sec ? '#26a69a33' : 'var(--panel2)',
              color: autoSec === o.sec ? '#26a69a' : 'var(--muted)',
              border: `1px solid ${autoSec === o.sec ? '#26a69a' : 'transparent'}`,
            }}>{o.label}</button>
          ))}
        </div>
        {/* manual refresh */}
        <button onClick={() => setRefresh(r => r + 1)} disabled={loading} style={{
          ...btn, background: 'var(--panel2)', color: 'var(--accent)',
          border: '1px solid var(--accent)', opacity: loading ? 0.5 : 1,
        }}>{loading ? '…' : '↻'}</button>
      </div>

      {/* indicator toggles */}
      <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap' }}>
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

      {/* chart */}
      <div ref={containerRef} style={{
        borderRadius: 8, overflow: 'hidden', minHeight: 480,
        border: '1px solid var(--border, #2a3045)',
        background: 'var(--panel, #0d1117)',
      }} />

      {errMsg && <div style={{ color: '#ef5350', fontSize: 12 }}>無法載入：{errMsg}</div>}
    </div>
  )
}
