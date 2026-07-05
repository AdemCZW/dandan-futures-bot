import { useEffect, useRef, useState } from 'react'
import { createChart, CrosshairMode, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'
import { Plain } from './Hint'
import { getChartColors, useTheme } from '../lib/theme.js'
import { createBackoffState, fetchBinancePublic } from '../lib/binancePoll'

// 雙均線系統版面（2026-07-05，還原 YouTube 分析的六線密集/發散系統）。
// MA20/60/120 + EMA20/60/120 六線同框；六線緊密糾結＝密集（盤整），
// 排列一致地向外展開＝發散（趨勢確立）。訊號標記＝b9 觀察倉實際依據的
// 「發散確立後首次回踩20均線不破」進場點（core.chart_data.ma6_overlay_data，
// 與 MaConvergencePullbackStrategy 同一份邏輯，圖表跟真實下單依據不會兜不起來）。

const SYMBOLS = ['LINKUSDT', 'BTCUSDT', 'ETHUSDT']
const TFS = ['15m', '1h', '4h']

// 幣安公開合約 kline REST（不需 API key）：輪詢更新最後一根 K 棒，跟 Chart.jsx 同機制。
const BINANCE_KLINES = (symbol, tf, limit = 2) =>
  `https://fapi.binance.com/fapi/v1/klines?symbol=${symbol.toUpperCase()}&interval=${tf}&limit=${limit}`
const mapKline = (k) => ({
  time: Math.floor(k[0] / 1000),
  open: parseFloat(k[1]), high: parseFloat(k[2]),
  low: parseFloat(k[3]), close: parseFloat(k[4]),
})

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
  const [counts, setCounts] = useState({ breakout: BLANK_DIR(), pullback1: BLANK_DIR(), pullback2: BLANK_DIR() })
  const [livePrice, setLivePrice] = useState(null)
  const [prevPrice, setPrevPrice] = useState(null)
  const [lastTs, setLastTs] = useState(null)
  const [wsReady, setWsReady] = useState(false)

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
      markersRef.current.setMarkers(buildAllMarkers(d, c))
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
        markersRef.current?.setMarkers(buildAllMarkers(d, c))
        setCounts(countByTypeAndDir(d.ma6_signals ?? []))
        const n = d.candles?.length ?? 0
        requestAnimationFrame(() => {
          if (!chartRef.current) return
          if (n > 0) chartRef.current.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90), to: n + 12 })
        })
        const last = d.candles?.[d.candles.length - 1]
        if (last) { setLastTs(last.time); setLivePrice(last.close) }
        setLoading(false)
      })
      .catch(e => { setErrMsg(e.message); setLoading(false) })
  }, [symbol, tf])

  // ── 即時報價：輪詢幣安公開合約 K 線，更新最後一根 K 棒（跟 Chart.jsx 同機制）──
  useEffect(() => {
    setWsReady(false)
    let alive = true
    const backoff = createBackoffState()

    async function poll() {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
      try {
        const arr = await fetchBinancePublic(BINANCE_KLINES(symbol, tf, 2), backoff)
        if (!alive || !Array.isArray(arr) || !arr.length) return
        const series = seriesRef.current.candles
        if (!series) return
        const cache = dataRef.current?.candles
        const lastTime = cache?.length ? cache[cache.length - 1].time : 0
        let latest = null
        for (const k of arr) {
          const candle = mapKline(k)
          if (candle.time < lastTime) continue
          series.update(candle)
          latest = candle
          if (cache?.length) {
            const last = cache[cache.length - 1]
            if (last.time === candle.time) cache[cache.length - 1] = candle
            else if (candle.time > last.time) cache.push(candle)
          }
        }
        if (latest) {
          setLivePrice(prev => { setPrevPrice(prev); return latest.close })
          setLastTs(Math.floor(Date.now() / 1000))
          setWsReady(true)
        }
      } catch { /* 網路異常靜默，下一輪再試 */ }
    }

    poll()
    const t = setInterval(poll, 3000)
    return () => { alive = false; clearInterval(t) }
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
  const priceUp = livePrice != null && prevPrice != null && livePrice >= prevPrice
  const priceClr = livePrice == null ? 'var(--muted)' : priceUp ? 'var(--pos)' : 'var(--neg)'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Plain>
        雙均線系統版面(還原 YouTube 分析的六線密集/發散系統)：MA20/60/120(實線)+
        EMA20/60/120(虛線)。六線糾結＝<b>密集</b>(盤整)、灰點標示；六線依序展開＝<b>發散</b>(趨勢確立)。
        圖上三種進場訊號：<b style={{ color: 'var(--accent)' }}>藍◆密集突破</b>(方法一，密集區一表態就進)、
        <b style={{ color: 'var(--warn)' }}>黃▲首踩</b>(方法二，發散後第一次回踩20均線不破)、
        <b style={{ color: 'var(--bot3, #b58ce0)' }}>紫▲二踩</b>(第二次回踩)。
        b9 觀察倉<b>實際只下單「首踩」</b>；突破與二踩為圖上顯示供你評估，尚未接進實盤。
        箭頭<b>向上(▲)＝做多、向下(▼)＝做空</b>，圖上標記文字也會標「多／空」。
      </Plain>

      {/* ── 即時報價（幣安公開合約，跟 K 線圖表分頁同資料源）── */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--faint)', fontFamily: 'var(--font-display)', marginBottom: 2 }}>
            {symbol} · {tf} · Binance Futures
          </div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontSize: 32, fontWeight: 700, fontFamily: 'var(--font-display)', color: priceClr, letterSpacing: '-0.5px' }}>
              {livePrice != null
                ? livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
                : '—'}
            </span>
            <span style={{ fontSize: 13, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>USDT</span>
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 4, marginLeft: 8 }}>
          <span style={{ fontSize: 10, color: wsReady ? 'var(--pos)' : 'var(--faint)', fontFamily: 'var(--font-display)', display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: wsReady ? 'var(--pos)' : 'var(--faint)', display: 'inline-block' }} />
            {wsReady ? 'LIVE' : '連線中…'}
          </span>
          {lastTs && (
            <span style={{ fontSize: 10, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>
              K棒：{new Date(lastTs * 1000).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>
      </div>

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
      </div>

      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'center', fontSize: 11, fontFamily: 'var(--font-display)' }}>
        <span style={{ color: 'var(--accent)' }}>◆ 密集突破 {counts.breakout.total}
          <span style={{ color: 'var(--muted)', fontWeight: 400 }}> (▲多{counts.breakout.long}／▼空{counts.breakout.short})</span>
        </span>
        <span style={{ color: 'var(--warn)' }}>▲ 首踩(b9實單) {counts.pullback1.total}
          <span style={{ color: 'var(--muted)', fontWeight: 400 }}> (▲多{counts.pullback1.long}／▼空{counts.pullback1.short})</span>
        </span>
        <span style={{ color: cDom.bot3 }}>▲ 二踩 {counts.pullback2.total}
          <span style={{ color: 'var(--muted)', fontWeight: 400 }}> (▲多{counts.pullback2.long}／▼空{counts.pullback2.short})</span>
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

// 三型進場訊號的顏色/文字（方法一密集突破、方法二首踩=b9實單、二踩）。
const SIG_STYLE = {
  breakout:  { ckey: 'accent', label: '突破' },
  pullback1: { ckey: 'warn',   label: '首踩' },
  pullback2: { ckey: 'bot3',   label: '二踩' },
}

const BLANK_DIR = () => ({ total: 0, long: 0, short: 0 })

// 依訊號型別 + 多空方向統計筆數（供上方統計列顯示，不用擠在圖上小標籤裡辨認方向）。
function countByTypeAndDir(signals) {
  const out = { breakout: BLANK_DIR(), pullback1: BLANK_DIR(), pullback2: BLANK_DIR() }
  signals.forEach(s => {
    const bucket = out[s.type]
    if (!bucket) return
    bucket.total += 1
    if (s.dir > 0) bucket.long += 1
    else bucket.short += 1
  })
  return out
}

// 組合密集區起點標記 + 三型進場訊號，依時間排序（lightweight-charts 要求遞增）。
function buildAllMarkers(d, c) {
  const markers = []

  // 密集區：只在每段連續密集的「起點」標一個灰點（避免逐根堆疊成柱）。
  const density = d.density ?? []
  const candles = d.candles ?? []
  const barSec = candles.length > 1 ? candles[1].time - candles[0].time : 0
  const densTimes = new Set(density.map(x => x.time))
  density.forEach(x => {
    const isRunStart = barSec > 0 && !densTimes.has(x.time - barSec)
    if (isRunStart) {
      markers.push({ time: x.time, position: 'belowBar', color: c.faint ?? '#6a6862',
                     shape: 'circle', text: '密集' })
    }
  })

  ;(d.ma6_signals ?? []).forEach(s => {
    const st = SIG_STYLE[s.type] ?? SIG_STYLE.pullback1
    const long = s.dir > 0
    markers.push({
      time: s.time,
      position: long ? 'belowBar' : 'aboveBar',
      color: c[st.ckey] ?? '#d4a24e',
      shape: long ? 'arrowUp' : 'arrowDown',
      text: st.label + (long ? '多' : '空'),
    })
  })

  return markers.sort((a, b) => a.time - b.time)
}
