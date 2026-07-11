import { useEffect, useRef, useState } from 'react'
import { createChart, CrosshairMode, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'
import { Plain } from './Hint'
import { getChartColors, useTheme } from '../lib/theme.js'
import { createBackoffState, fetchBinancePublic } from '../lib/binancePoll'

// 雙均線系統版面（2026-07-05，還原 YouTube 分析的六線密集/發散系統）。
// 2026-07-12 整頁重設計：四個獨立圖層（均線／斜通道／通道內層／水平回撤），
// 每條線在圖上直接標名＋價位，預設只開關鍵線，解決「線太多很亂」的問題。

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

// ── 圖層一：均線六線（MA 實線藍紫系 / EMA 虛線暖色系；20 期最粗＝回踩判斷線）──
const LINES = [
  { key: 'ma20',  label: 'MA20',  ckey: 'accent', width: 2, style: 0 },
  { key: 'ma60',  label: 'MA60',  ckey: 'bot3',   width: 1, style: 0 },
  { key: 'ma120', label: 'MA120', ckey: 'muted',  width: 1, style: 0 },
  { key: 'ema20', label: 'EMA20', ckey: 'bot4',   width: 2, style: 2 },
  { key: 'ema60', label: 'EMA60', ckey: 'bot2',   width: 1, style: 2 },
  { key: 'ema120', label: 'EMA120', ckey: 'faint', width: 1, style: 2 },
]

// ── 圖層二：斜通道關鍵線（迴歸60根）。零軸/一軸標名在價格軸上（title），
//    金色粗實線；內部比率獨立成圖層三（預設關，要看結構層級再打開）──
const FIB_KEY_LINES = [
  { key: 'fib_ch_0',   label: '通道零軸', ckey: 'warn', width: 2, style: 0, title: '通道零軸' },
  { key: 'fib_ch_100', label: '通道一軸', ckey: 'warn', width: 2, style: 0, title: '通道一軸' },
]
const FIB_INNER_LINES = [
  { key: 'fib_ch_236', label: '0.236', ckey: 'faint', width: 1, style: 3 },
  { key: 'fib_ch_382', label: '0.382', ckey: 'faint', width: 1, style: 3 },
  { key: 'fib_ch_5',   label: '0.5',   ckey: 'faint', width: 1, style: 3 },
  { key: 'fib_ch_618', label: '0.618', ckey: 'faint', width: 1, style: 3 },
  { key: 'fib_ch_786', label: '0.786', ckey: 'faint', width: 1, style: 3 },
]

// ── 圖層四：水平回撤層（擺動高低點錨定，畫成 priceLine，每條線自帶文字標籤）──
// 0/1（行情起點/終點）實線較粗，中間比率虛線；顏色用綠色系跟金色通道區隔。
const RETR_KEY = new Set([0, 1])
const retrLabel = (ratio, dir) => {
  if (ratio === 0) return dir === -1 ? '回撤0＝波段高點' : '回撤0＝波段低點'
  if (ratio === 1) return dir === -1 ? '回撤1＝波段低點' : '回撤1＝波段高點'
  return `回撤 ${ratio}`
}

export default function DualMa() {
  const theme = useTheme()
  const containerRef = useRef(null)
  const chartRef = useRef(null)
  const seriesRef = useRef({})
  const markersRef = useRef(null)
  const dataRef = useRef(null)
  const retrLinesRef = useRef([])

  const [symbol, setSymbol] = useState('LINKUSDT')
  const [tf, setTf] = useState('4h')
  const [loading, setLoading] = useState(false)
  const [errMsg, setErrMsg] = useState(null)
  const [counts, setCounts] = useState({ breakout: BLANK_DIR(), pullback1: BLANK_DIR(), pullback2: BLANK_DIR() })
  const [livePrice, setLivePrice] = useState(null)
  const [prevPrice, setPrevPrice] = useState(null)
  const [lastTs, setLastTs] = useState(null)
  const [wsReady, setWsReady] = useState(false)
  // 四個獨立圖層開關：預設開均線＋通道關鍵線＋回撤；通道內層預設關（減亂）。
  const [showMa, setShowMa] = useState(true)
  const [showFib, setShowFib] = useState(true)
  const [showFibInner, setShowFibInner] = useState(false)
  const [showRetr, setShowRetr] = useState(true)
  const [fibDir, setFibDir] = useState(0)
  const [retrDir, setRetrDir] = useState(0)

  // 依開關餵資料（不重抓 API）：均線 / 通道關鍵線 / 通道內層。
  const applyLineLayers = (d, { ma = showMa, fib = showFib, inner = showFibInner } = {}) => {
    LINES.forEach(({ key }) => seriesRef.current[key]?.setData(ma ? (d[key] ?? []) : []))
    FIB_KEY_LINES.forEach(({ key }) => seriesRef.current[key]?.setData(fib ? (d.fib_channel?.[key] ?? []) : []))
    FIB_INNER_LINES.forEach(({ key }) => seriesRef.current[key]?.setData((fib && inner) ? (d.fib_channel?.[key] ?? []) : []))
  }

  // 水平回撤層：畫成 candle series 上的 priceLine（線上自帶文字標籤＋價格軸標籤）。
  const applyRetrLayer = (d, visible = showRetr) => {
    const candles = seriesRef.current.candles
    if (!candles) return
    retrLinesRef.current.forEach(pl => { try { candles.removePriceLine(pl) } catch { /* series 已重建 */ } })
    retrLinesRef.current = []
    const retr = d?.fib_retracement
    if (!visible || !retr?.levels?.length) return
    const c = getChartColors()
    retr.levels.forEach(({ ratio, price }) => {
      const isKey = RETR_KEY.has(ratio)
      retrLinesRef.current.push(candles.createPriceLine({
        price,
        color: isKey ? c.pos : `${c.pos}88`,
        lineWidth: isKey ? 2 : 1,
        lineStyle: isKey ? 0 : 2,
        axisLabelVisible: true,
        title: retrLabel(ratio, retr.dir),
      }))
    })
  }

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
    // 通道關鍵線：價格軸上顯示線名＋當前價（title + lastValueVisible）
    FIB_KEY_LINES.forEach(({ key, ckey, width, style, title }) => {
      const s = chart.addSeries(LineSeries, {
        color: c[ckey], lineWidth: width, lineStyle: style, title,
        priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
      })
      s.setData([])
      seriesRef.current[key] = s
    })
    FIB_INNER_LINES.forEach(({ key, ckey, width, style }) => {
      const s = chart.addSeries(LineSeries, {
        color: c[ckey], lineWidth: width, lineStyle: style,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      })
      s.setData([])
      seriesRef.current[key] = s
    })
    chartRef.current = chart
    retrLinesRef.current = []          // candle series 重建 → 舊 priceLine 已消滅

    const d = dataRef.current
    if (d?.candles?.length) {
      seriesRef.current.candles.setData(d.candles)
      applyLineLayers(d)
      applyRetrLayer(d)
      markersRef.current.setMarkers(buildAllMarkers(d, c))
      const n = d.candles.length
      requestAnimationFrame(() => {
        chartRef.current?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90), to: n + 12 })
      })
    }

    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }))
    ro.observe(el)
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = {}; retrLinesRef.current = [] }
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
        applyLineLayers(d)
        applyRetrLayer(d)
        setFibDir(d.fib_dir ?? 0)
        setRetrDir(d.fib_retracement?.dir ?? 0)
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
  }, [symbol, tf]) // eslint-disable-line react-hooks/exhaustive-deps

  // 圖層開關：開→餵資料、關→清空，不重抓 API。
  useEffect(() => { if (dataRef.current) applyLineLayers(dataRef.current) }, [showMa, showFib, showFibInner]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (dataRef.current) applyRetrLayer(dataRef.current) }, [showRetr]) // eslint-disable-line react-hooks/exhaustive-deps

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

  const chip = (active, color = 'var(--accent)') => ({
    padding: '3px 10px', borderRadius: 'var(--radius-pill)', border: 'none', cursor: 'pointer',
    fontSize: 11, fontWeight: 600, fontFamily: 'var(--font-display)',
    background: active ? 'color-mix(in srgb, ' + color + ' 14%, transparent)' : 'transparent',
    color: active ? color : 'var(--faint)',
    outline: active ? `1px solid ${color}` : '1px solid var(--line)',
  })

  const cDom = getChartColors()
  void theme
  const priceUp = livePrice != null && prevPrice != null && livePrice >= prevPrice
  const priceClr = livePrice == null ? 'var(--muted)' : priceUp ? 'var(--pos)' : 'var(--neg)'

  const legendSection = { display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center', fontSize: 11, fontFamily: 'var(--font-display)', color: 'var(--muted)' }
  const legendTag = (txt, color) => (
    <span style={{ color, fontWeight: 700, minWidth: 64 }}>{txt}</span>
  )
  const swatch = (color, dashed = false, thick = false) => (
    <span style={{
      width: 16, display: 'inline-block',
      height: dashed ? 0 : (thick ? 3 : 2),
      borderTop: dashed ? `2px dashed ${color}` : 'none',
      background: dashed ? 'transparent' : color,
    }} />
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <Plain>
        雙均線系統版面：K 線＋四個可獨立開關的圖層。<b>均線</b>＝六線密集/發散系統
        (MA 實線、EMA 虛線；六線糾結＝密集/盤整、依序展開＝發散/趨勢確立)。
        <b style={{ color: 'var(--warn)' }}>通道(斜)</b>＝迴歸擬合最近60根的斐波那契通道，
        零軸＝行情原點側(下降通道在上緣=壓力／上升在下緣=支撐)、一軸＝對側目標；「內層」開關可加顯 0.236~0.786 結構層級。
        <b style={{ color: 'var(--pos)' }}>回撤(水平)</b>＝最近180根擺動高低點錨定的斐波那契回撤，
        0 錨在行情起點(下跌段=高點/上漲段=低點)，跟分析師 TradingView 畫法一致，每條線都直接標名。
        圖上三種進場訊號：<b style={{ color: 'var(--accent)' }}>藍◆密集突破</b>、
        <b style={{ color: 'var(--warn)' }}>黃▲首踩</b>(b9 實際下單依據)、
        <b style={{ color: 'var(--bot3, #b58ce0)' }}>紫▲二踩</b>；▲＝做多、▼＝做空。
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

      {/* ── 控制列：幣種 / 週期 ── */}
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

      {/* ── 圖層開關列（顏色對應圖上線色）── */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        <span style={{ fontSize: 11, color: 'var(--faint)', fontFamily: 'var(--font-display)', marginRight: 2 }}>圖層：</span>
        <button onClick={() => setShowMa(v => !v)} style={chip(showMa, 'var(--accent)')}>均線6線</button>
        <button onClick={() => setShowFib(v => !v)} style={chip(showFib, 'var(--warn)')}>
          通道(斜){fibDir === -1 ? '↓' : fibDir === 1 ? '↑' : ''}
        </button>
        <button onClick={() => setShowFibInner(v => !v)} disabled={!showFib}
                style={{ ...chip(showFib && showFibInner, 'var(--warn)'), opacity: showFib ? 1 : 0.4 }}>
          ＋內層0.236~0.786
        </button>
        <button onClick={() => setShowRetr(v => !v)} style={chip(showRetr, 'var(--pos)')}>
          回撤(水平){retrDir === -1 ? '↓' : retrDir === 1 ? '↑' : ''}
        </button>
      </div>

      {/* ── 分組圖例：每條線的顏色/線型/含義 ── */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: '8px 10px',
                    border: '1px solid var(--line)', borderRadius: 'var(--radius)', background: 'var(--surface)' }}>
        {showMa && (
          <div style={legendSection}>
            {legendTag('均線', 'var(--accent)')}
            {LINES.map(({ key, label, ckey }) => (
              <span key={key} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                {swatch(cDom[ckey], key.startsWith('ema'))}{label}
              </span>
            ))}
          </div>
        )}
        {showFib && (
          <div style={legendSection}>
            {legendTag(`通道(斜)${fibDir === -1 ? '·下降' : fibDir === 1 ? '·上升' : ''}`, 'var(--warn)')}
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              {swatch(cDom.warn, false, true)}零軸（{fibDir === -1 ? '上緣壓力' : '下緣支撐'}，價格軸有標名）
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              {swatch(cDom.warn, false, true)}一軸（對側目標）
            </span>
            {showFibInner && (
              <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <span style={{ width: 16, height: 0, borderTop: `1px dotted ${cDom.faint}`, display: 'inline-block' }} />
                內層 0.236／0.382／0.5／0.618／0.786（結構層級）
              </span>
            )}
          </div>
        )}
        {showRetr && (
          <div style={legendSection}>
            {legendTag(`回撤(水平)${retrDir === -1 ? '·下跌段' : retrDir === 1 ? '·上漲段' : ''}`, 'var(--pos)')}
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              {swatch(cDom.pos, false, true)}0／1＝波段{retrDir === -1 ? '高點／低點' : '低點／高點'}（錨點）
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
              {swatch(`${cDom.pos}88`, true)}0.236~0.786（回撤層級，每條線上直接標名）
            </span>
          </div>
        )}
        <div style={{ ...legendSection }}>
          {legendTag('訊號', 'var(--muted)')}
          <span style={{ color: 'var(--accent)' }}>◆ 密集突破 {counts.breakout.total}
            <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(▲{counts.breakout.long}/▼{counts.breakout.short})</span>
          </span>
          <span style={{ color: 'var(--warn)' }}>▲ 首踩·b9實單 {counts.pullback1.total}
            <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(▲{counts.pullback1.long}/▼{counts.pullback1.short})</span>
          </span>
          <span style={{ color: cDom.bot3 }}>▲ 二踩 {counts.pullback2.total}
            <span style={{ color: 'var(--muted)', fontWeight: 400 }}>(▲{counts.pullback2.long}/▼{counts.pullback2.short})</span>
          </span>
          <span style={{ color: 'var(--faint)' }}>● 密集區起點</span>
        </div>
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

// 依訊號型別 + 多空方向統計筆數（供圖例統計列顯示，不用擠在圖上小標籤裡辨認方向）。
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
