import { useEffect, useRef, useState } from 'react'
import { createChart, CrosshairMode, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'
import { Plain } from './Hint'
import { getChartColors, useTheme } from '../lib/theme.js'

// 雙均線系統版面（2026-07-05，還原 YouTube 分析的六線密集/發散系統）。
// MA20/60/120 + EMA20/60/120 六線同框；六線緊密糾結＝密集（盤整），
// 排列一致地向外展開＝發散（趨勢確立）。訊號標記＝b9 觀察倉實際依據的
// 「發散確立後首次回踩20均線不破」進場點（core.chart_data.ma6_overlay_data，
// 與 MaConvergencePullbackStrategy 同一份邏輯，圖表跟真實下單依據不會兜不起來）。

const SYMBOLS = ['LINKUSDT', 'BTCUSDT', 'ETHUSDT']
const TFS = ['15m', '1h', '4h']

// MA 系（實線，藍紫色系）+ EMA 系（虛線，暖色系）；20 期最粗（回踩訊號的判斷線）。
const LINES = [
  { key: 'ma20',  label: 'MA20',  ckey: 'accent', width: 2, style: 0 },
  { key: 'ma60',  label: 'MA60',  ckey: 'bot3',   width: 1, style: 0 },
  { key: 'ma120', label: 'MA120', ckey: 'muted',  width: 1, style: 0 },
  { key: 'ema20', label: 'EMA20', ckey: 'bot4',   width: 2, style: 2 },
  { key: 'ema60', label: 'EMA60', ckey: 'bot2',   width: 1, style: 2 },
  { key: 'ema120', label: 'EMA120', ckey: 'faint', width: 1, style: 2 },
]

export default function DualMa() {
  const theme = useTheme()
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({})
  const markersRef = useRef(null)
  const dataRef = useRef(null)

  const [symbol, setSymbol] = useState('LINKUSDT')
  const [tf, setTf] = useState('4h')
  const [loading, setLoading] = useState(false)
  const [errMsg, setErrMsg] = useState(null)
  const [signalCount, setSignalCount] = useState(0)

  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const c = getChartColors()
    const chart = createChart(el, {
      layout: { background: { color: c.bg }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: c.border, textColor: c.text },
      timeScale: { borderColor: c.border, timeVisible: true, secondsVisible: false, rightOffset: 12, barSpacing: 10, minBarSpacing: 3 },
      width: el.clientWidth, height: 460,
    })
    seriesRef.current.candles = chart.addSeries(CandlestickSeries, {
      upColor: c.up, downColor: c.down, borderUpColor: c.up, borderDownColor: c.down,
      wickUpColor: c.up, wickDownColor: c.down,
    })
    markersRef.current = createSeriesMarkers(seriesRef.current.candles, [])
    LINES.forEach(({ key, ckey, width, style }) => {
      const s = chart.addSeries(LineSeries, {
        color: c[ckey], lineWidth: width, lineStyle: style,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      })
      s.setData([])
      seriesRef.current[key] = s
    })
    chartRef.current = chart

    const d = dataRef.current
    if (d?.candles?.length) {
      seriesRef.current.candles.setData(d.candles)
      LINES.forEach(({ key }) => seriesRef.current[key]?.setData(d[key] ?? []))
      markersRef.current.setMarkers(buildMarkers(d.ma6_signals ?? [], c))
      const n = d.candles.length
      requestAnimationFrame(() => {
        chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90), to: n + 12 })
      })
    }

    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }))
    ro.observe(el)
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = {} }
  }, [theme]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!chartRef.current) return
    setLoading(true)
    setErrMsg(null)
    api.ma6(symbol, tf, 300)
      .then(d => {
        if (d.error) throw new Error(d.error)
        dataRef.current = d
        seriesRef.current.candles?.setData(d.candles ?? [])
        LINES.forEach(({ key }) => seriesRef.current[key]?.setData(d[key] ?? []))
        const c = getChartColors()
        markersRef.current?.setMarkers(buildMarkers(d.ma6_signals ?? [], c))
        setSignalCount((d.ma6_signals ?? []).length)
        const n = d.candles?.length ?? 0
        requestAnimationFrame(() => {
          if (!chartRef.current) return
          if (n > 0) chartRef.current.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90), to: n + 12 })
        })
        setLoading(false)
      })
      .catch(e => { setErrMsg(e.message); setLoading(false) })
  }, [symbol, tf])

  const chip = (active) => ({
    padding: '3px 10px', borderRadius: 'var(--radius-pill)', border: 'none', cursor: 'pointer',
    fontSize: 11, fontWeight: 600, fontFamily: 'var(--font-display)',
    background: active ? 'var(--accent-soft)' : 'transparent',
    color: active ? 'var(--accent)' : 'var(--faint)',
    outline: active ? '1px solid var(--accent)' : '1px solid var(--line)',
  })

  const cDom = getChartColors()
  void theme

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Plain>
        雙均線系統版面(還原 YouTube 分析的六線密集/發散系統)：MA20/60/120(實線)+
        EMA20/60/120(虛線)。六線糾結在一起＝<b>密集</b>(盤整、方向未明)；
        六線依序展開＝<b>發散</b>(趨勢確立)。黃色<b>▲/▼標記</b>是發散確立後「第一次回踩20均線不破」
        的進場訊號——跟 b9(LINKUSDT 觀察倉)實際下單依據完全同一套邏輯。
      </Plain>

      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {SYMBOLS.map(s => (
          <button key={s} onClick={() => setSymbol(s)} style={chip(symbol === s)}>{s.replace('USDT', '')}</button>
        ))}
        <div style={{ width: 1, height: 16, background: 'var(--line-strong)', margin: '0 2px' }} />
        {TFS.map(t => (
          <button key={t} onClick={() => setTf(t)} style={chip(tf === t)}>{t}</button>
        ))}
        {loading && <span style={{ fontSize: 11, color: 'var(--faint)' }}>載入中…</span>}
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
        {LINES.map(({ key, label, ckey }) => (
          <span key={key} style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11, fontFamily: 'var(--font-display)', color: 'var(--muted)' }}>
            <span style={{
              width: 14, display: 'inline-block',
              height: key.startsWith('ema') ? 0 : 2,
              borderTop: key.startsWith('ema') ? `2px dashed ${cDom[ckey]}` : 'none',
              background: key.startsWith('ema') ? 'transparent' : cDom[ckey],
            }} />
            {label}
          </span>
        ))}
        <span style={{ fontSize: 11, color: 'var(--warn)', fontFamily: 'var(--font-display)', marginLeft: 8 }}>
          ▲▼ 回踩進場訊號 · 本頁共 {signalCount} 個
        </span>
      </div>

      <div ref={containerRef} style={{
        borderRadius: 'var(--radius)', overflow: 'hidden', border: '1px solid var(--line)',
        background: 'var(--surface)', minHeight: 460,
      }} />

      {errMsg && <div style={{ fontSize: 11, color: 'var(--neg)' }}>⚠ {errMsg}</div>}
    </div>
  )
}

function buildMarkers(signals, c) {
  return signals.map(s => ({
    time: s.time,
    position: s.dir > 0 ? 'belowBar' : 'aboveBar',
    color: c.warn ?? '#d4a24e',
    shape: s.dir > 0 ? 'arrowUp' : 'arrowDown',
    text: s.dir > 0 ? '回踩多' : '回踩空',
  }))
}
