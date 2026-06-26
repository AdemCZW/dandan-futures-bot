import { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, CrosshairMode, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
const TFS = ['1h', '4h', '1d']

// 不同 bot（strategy）配不同顏色；超過長度則循環取用
const BOT_PALETTE = ['#58a6ff', '#3fb950', '#ffa657', '#d2a8ff', '#39d3c3', '#f778ba', '#e3b341', '#ff7b72']
const colorForBot = (names, strat) => BOT_PALETTE[Math.max(0, names.indexOf(strat)) % BOT_PALETTE.length]

// mode → 中文標籤（標明回測 vs 真實）
const MODE_LABEL = { backtest: '回測', live_futures_testnet: '測試網', paper: '模擬' }
const modeLabel = (mode) => MODE_LABEL[mode] || mode
const AUTO_OPTS = [{ label: '關閉', sec: 0 }, { label: '3s', sec: 3 }, { label: '10s', sec: 10 }, { label: '30s', sec: 30 }]

// indicator catalogue — only ST + EMA200 on by default
const OVERLAYS = [
  { key: 'supertrend_bull', label: 'Supertrend ↑', color: '#3fb950', width: 2, style: 0, defOn: true,  group: '趨勢' },
  { key: 'supertrend_bear', label: 'Supertrend ↓', color: '#f85149', width: 2, style: 0, defOn: true,  group: '趨勢' },
  { key: 'ema_trend',       label: 'EMA 200',       color: '#58a6ff', width: 2, style: 0, defOn: true,  group: '趨勢' },
  { key: 'ema_slow',        label: 'EMA 21',        color: '#d2a8ff', width: 1, style: 0, defOn: false, group: '均線' },
  { key: 'ema_fast',        label: 'EMA 9',         color: '#ffa657', width: 1, style: 0, defOn: false, group: '均線' },
  { key: 'donchian_upper',  label: 'DC 上軌',        color: '#3d4451', width: 1, style: 2, defOn: false, group: '通道' },
  { key: 'donchian_lower',  label: 'DC 下軌',        color: '#3d4451', width: 1, style: 2, defOn: false, group: '通道' },
  // 費波那契通道：0 與 1.0 為錨點（實線），中間比率與延伸線（虛線）。漲跌雙向自動切換。
  { key: 'fib_ch_0',        label: 'FC 0',           color: '#7f77dd', width: 2, style: 0, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_236',      label: 'FC 0.236',       color: '#6e7681', width: 1, style: 2, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_382',      label: 'FC 0.382',       color: '#6e7681', width: 1, style: 2, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_5',        label: 'FC 0.5',         color: '#8b949e', width: 1, style: 2, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_618',      label: 'FC 0.618',       color: '#ffa657', width: 2, style: 0, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_786',      label: 'FC 0.786',       color: '#6e7681', width: 1, style: 2, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_100',      label: 'FC 1.0',         color: '#7f77dd', width: 2, style: 0, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_1272',     label: 'FC 1.272',       color: '#f85149', width: 1, style: 1, defOn: false, group: 'Fib 延伸' },
  { key: 'fib_ch_1618',     label: 'FC 1.618',       color: '#f85149', width: 1, style: 1, defOn: false, group: 'Fib 延伸' },
  { key: 'fib_ch_200',      label: 'FC 2.0',         color: '#f85149', width: 1, style: 1, defOn: false, group: 'Fib 延伸' },
]

const ALL_KEYS = OVERLAYS.map(o => o.key)
const initVis = Object.fromEntries(OVERLAYS.map(o => [o.key, o.defOn]))

export default function Chart() {
  const containerRef = useRef(null)
  const chartRef     = useRef(null)
  const seriesRef    = useRef({})
  const dataRef      = useRef({})
  const markersRef   = useRef(null)          // createSeriesMarkers plugin api
  const markerDataRef = useRef([])           // 原始標記（後端回傳）

  const [bots,        setBots]        = useState([])    // 出現過的 strategy 清單（圖例）
  const [showMarkers, setShowMarkers] = useState(true)  // 交易點開關

  const [symbol,    setSymbol]    = useState('BTCUSDT')
  const [tf,        setTf]        = useState('4h')
  const [loading,   setLoading]   = useState(false)
  const [errMsg,    setErrMsg]    = useState(null)
  const [vis,       setVis]       = useState(initVis)
  const [lastTs,    setLastTs]    = useState(null)
  const [livePrice, setLivePrice] = useState(null)
  const [prevPrice, setPrevPrice] = useState(null)
  const [autoSec,   setAutoSec]   = useState(3)
  const [countdown, setCountdown] = useState(0)
  const [refresh,   setRefresh]   = useState(0)

  // ── chart init ──────────────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      layout: { background: { color: 'transparent' }, textColor: '#8b949e' },
      grid:   { vertLines: { color: '#161b22' }, horzLines: { color: '#161b22' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#21262d', textColor: '#8b949e' },
      timeScale: {
        borderColor: '#21262d', timeVisible: true, secondsVisible: false,
        rightOffset: 8, barSpacing: 8,
      },
      width: el.clientWidth, height: 460,
    })

    seriesRef.current.candles = chart.addSeries(CandlestickSeries, {
      upColor:        '#3fb950', downColor:       '#f85149',
      borderUpColor:  '#3fb950', borderDownColor: '#f85149',
      wickUpColor:    '#3fb950', wickDownColor:   '#f85149',
    })
    // 交易標記層（v5 plugin），附在 K 線 series 上
    markersRef.current = createSeriesMarkers(seriesRef.current.candles, [])

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

  // ── full klines load ─────────────────────────────────────────────────
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

  // ── live price poll ──────────────────────────────────────────────────
  useEffect(() => {
    if (autoSec === 0) { setCountdown(0); return }
    setCountdown(autoSec)
    const tick = () => {
      api.price(symbol).then(d => {
        if (!d.price) return
        setPrevPrice(p => { return p ?? d.price })
        setLivePrice(prev => { setPrevPrice(prev); return d.price })
        const candles = dataRef.current.candles
        if (!candles?.length) return
        const last = candles[candles.length - 1]
        const upd = { time: last.time, open: last.open,
          high: Math.max(last.high, d.price), low: Math.min(last.low, d.price), close: d.price }
        seriesRef.current.candles?.update(upd)
        dataRef.current.candles[candles.length - 1] = upd
        setLastTs(d.ts)
      }).catch(() => {})
    }
    const ivP = setInterval(tick, autoSec * 1000)
    let cd = autoSec
    const ivC = setInterval(() => { cd -= 1; if (cd <= 0) cd = autoSec; setCountdown(cd) }, 1000)
    return () => { clearInterval(ivP); clearInterval(ivC) }
  }, [autoSec, symbol]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── 交易標記：依 bot 配色 + 進出場形狀 + 聚合筆數，套到 K 線 ──────────────
  const applyMarkers = useCallback((raw, botNames, show) => {
    if (!markersRef.current) return
    if (!show || !raw?.length) { markersRef.current.setMarkers([]); return }
    const ms = raw.map(m => {
      const color = colorForBot(botNames, m.strategy)
      const text  = m.count > 1 ? String(m.count) : undefined   // 聚合多筆 → 顯示筆數
      if (m.side === 'entry') {
        const short = m.dir < 0
        return { time: m.time, position: short ? 'aboveBar' : 'belowBar',
                 color, shape: short ? 'arrowDown' : 'arrowUp', text }
      }
      return { time: m.time, position: 'aboveBar', color, shape: 'circle', text }
    })
    markersRef.current.setMarkers(ms)
  }, [])

  // ── fetch 交易標記（換標的/重整時；每 6 小時聚合一點）───────────────────
  useEffect(() => {
    api.tradeMarkers(symbol, 6)
      .then(d => {
        markerDataRef.current = d.markers ?? []
        const bl = d.bots ?? []
        setBots(bl)
        applyMarkers(d.markers ?? [], bl.map(b => b.strategy), showMarkers)
      })
      .catch(() => { markerDataRef.current = []; setBots([]); applyMarkers([], [], false) })
  }, [symbol, refresh, applyMarkers]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── 開關切換 → 重套標記 ──────────────────────────────────────────────
  useEffect(() => {
    applyMarkers(markerDataRef.current, bots.map(b => b.strategy), showMarkers)
  }, [showMarkers, bots, applyMarkers])

  const toggle = useCallback(key => {
    setVis(prev => {
      const next = { ...prev, [key]: !prev[key] }
      seriesRef.current[key]?.setData(next[key] ? (dataRef.current[key] ?? []) : [])
      return next
    })
  }, [])

  const priceUp  = livePrice != null && prevPrice != null && livePrice >= prevPrice
  const priceClr = livePrice == null ? '#8b949e' : priceUp ? '#3fb950' : '#f85149'

  const chip = (active, color = 'var(--accent)') => ({
    padding: '3px 10px', borderRadius: 20, border: 'none', cursor: 'pointer',
    fontSize: 11, fontWeight: 600, fontFamily: 'var(--font-display)',
    background: active ? `${color}25` : 'transparent',
    color:      active ? color : '#484f58',
    outline:    active ? `1px solid ${color}55` : '1px solid #21262d',
    transition: 'all 0.15s',
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

      {/* ── price header ── */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 11, color: '#484f58', fontFamily: 'var(--font-display)', marginBottom: 2 }}>
            {symbol} · {tf} · Binance Futures
          </div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontSize: 32, fontWeight: 700, fontFamily: 'var(--font-display)', color: priceClr, letterSpacing: '-0.5px' }}>
              {livePrice != null
                ? livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                : '—'}
            </span>
            <span style={{ fontSize: 13, color: '#484f58', fontFamily: 'var(--font-display)' }}>USDT</span>
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 4, marginLeft: 8 }}>
          {autoSec > 0 && (
            <span style={{ fontSize: 10, color: '#3fb950', fontFamily: 'var(--font-display)', display: 'flex', alignItems: 'center', gap: 4 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: '#3fb950', display: 'inline-block', animation: 'pulse 1.5s infinite' }} />
              LIVE · {countdown}s
            </span>
          )}
          {lastTs && (
            <span style={{ fontSize: 10, color: '#484f58', fontFamily: 'var(--font-display)' }}>
              K棒：{new Date(lastTs * 1000).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>
      </div>

      {/* ── controls row ── */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {/* symbol */}
        {SYMBOLS.map(s => (
          <button key={s} onClick={() => setSymbol(s)} style={chip(symbol === s, '#58a6ff')}>
            {s.replace('USDT', '')}
          </button>
        ))}
        <div style={{ width: 1, height: 16, background: '#21262d', margin: '0 2px' }} />
        {/* timeframe */}
        {TFS.map(t => (
          <button key={t} onClick={() => setTf(t)} style={chip(tf === t, '#58a6ff')}>
            {t}
          </button>
        ))}
        <div style={{ width: 1, height: 16, background: '#21262d', margin: '0 2px' }} />
        {/* auto-refresh */}
        {AUTO_OPTS.map(o => (
          <button key={o.sec} onClick={() => setAutoSec(o.sec)} style={chip(autoSec === o.sec, '#3fb950')}>
            {o.label}
          </button>
        ))}
        {/* manual refresh */}
        <button onClick={() => setRefresh(r => r + 1)} disabled={loading}
          style={{ ...chip(false), color: loading ? '#484f58' : '#8b949e', padding: '3px 8px' }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      {/* ── indicator toggles ── */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {OVERLAYS.map(({ key, label, color }) => (
          <button key={key} onClick={() => toggle(key)} style={chip(vis[key], color)}>
            {label}
          </button>
        ))}
      </div>

      {/* ── 交易點：開關 + bot 圖例（每 6h 聚合一點，標明回測）── */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <button onClick={() => setShowMarkers(s => !s)} style={chip(showMarkers, '#e3b341')}>
          ⚲ 交易點 / 6h
        </button>
        {showMarkers && bots.length > 0 && (
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
            {bots.map(b => {
              const names = bots.map(x => x.strategy)
              const isBT = b.mode === 'backtest'
              return (
                <span key={b.strategy} style={{ display: 'flex', alignItems: 'center', gap: 4,
                  fontSize: 11, color: '#8b949e', fontFamily: 'var(--font-display)' }}>
                  <span style={{ width: 9, height: 9, borderRadius: '50%',
                    background: colorForBot(names, b.strategy), display: 'inline-block' }} />
                  {b.strategy}
                  <span style={{ fontSize: 10, padding: '1px 5px', borderRadius: 6,
                    background: isBT ? '#f8514922' : '#3fb95018',
                    color: isBT ? '#f85149' : '#6e7681',
                    outline: isBT ? '1px solid #f8514955' : 'none' }}>
                    {modeLabel(b.mode)}{isBT ? '⚠' : ''}
                  </span>
                  <span style={{ fontSize: 10, color: '#484f58' }}>{b.count}筆</span>
                </span>
              )
            })}
            <span style={{ fontSize: 10, color: '#484f58', fontFamily: 'var(--font-display)' }}>
              ↑進多 ↓進空 ●出場
            </span>
          </div>
        )}
        {showMarkers && bots.length === 0 && (
          <span style={{ fontSize: 11, color: '#484f58', fontFamily: 'var(--font-display)' }}>
            此標的目前無交易紀錄
          </span>
        )}
      </div>
      {showMarkers && bots.some(b => b.mode === 'backtest') && (
        <div style={{ fontSize: 10, color: '#f85149', fontFamily: 'var(--font-display)', marginTop: -6 }}>
          ⚠ 標「回測」者為回測產出資料（每筆只記出場、無進場點），非實盤下單
        </div>
      )}

      {/* ── chart ── */}
      <div ref={containerRef} style={{
        borderRadius: 10, overflow: 'hidden',
        border: '1px solid #21262d',
        background: '#0d1117',
        minHeight: 460,
      }} />

      {errMsg && <div style={{ fontSize: 11, color: '#f85149' }}>⚠ {errMsg}</div>}

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1 }
          50%       { opacity: 0.3 }
        }
      `}</style>
    </div>
  )
}
