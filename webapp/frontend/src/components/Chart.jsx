import { useEffect, useRef, useState, useCallback } from 'react'
import { createChart, CrosshairMode, CandlestickSeries, LineSeries, createSeriesMarkers } from 'lightweight-charts'
import { api } from '../api'
import { Plain } from './Hint'
import { getChartColors, useTheme } from '../lib/theme.js'

const SYMBOLS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT']
const TFS = ['1h', '4h', '1d']

// 不同 bot（strategy）配不同顏色；刻意避開紅/綠（與漲跌 K 棒同色會看不出來），
// 用 4 色 categorical 色票（per-bot accents），超過長度則循環取用。
// canvas 標記吃不到 CSS var，故由 getChartColors() 取當前主題的實際色字串。
const botPalette = (c) => [c.bot1, c.bot2, c.bot3, c.bot4]
const colorForBot = (c, names, strat) => {
  const pal = botPalette(c)
  return pal[Math.max(0, names.indexOf(strat)) % pal.length]
}

// mode → 中文標籤（標明回測 vs 真實）
const MODE_LABEL = { backtest: '回測', live_futures_testnet: '測試網', paper: '模擬' }
const modeLabel = (mode) => MODE_LABEL[mode] || mode
// Binance 公開合約 kline REST（不需 API key；瀏覽器直連，比 WS 穩定）
const BINANCE_KLINES = (symbol, tf, limit = 2) =>
  `https://fapi.binance.com/fapi/v1/klines?symbol=${symbol.toUpperCase()}&interval=${tf}&limit=${limit}`
// REST kline 陣列 → lightweight-charts candle 物件（時間用秒）
const mapKline = (k) => ({
  time:  Math.floor(k[0] / 1000),
  open:  parseFloat(k[1]), high: parseFloat(k[2]),
  low:   parseFloat(k[3]), close: parseFloat(k[4]),
})

// indicator catalogue — only ST + EMA200 on by default。
// `ckey` 指向 getChartColors() 的欄位（canvas 線色 + chip 標籤色都用它），主題切換時即時取色。
const OVERLAYS = [
  { key: 'supertrend_bull', label: 'Supertrend ↑', ckey: 'up',     width: 2, style: 0, defOn: true,  group: '趨勢' },
  { key: 'supertrend_bear', label: 'Supertrend ↓', ckey: 'down',   width: 2, style: 0, defOn: true,  group: '趨勢' },
  { key: 'ema_trend',       label: 'EMA 200',       ckey: 'accent', width: 2, style: 0, defOn: true,  group: '趨勢' },
  { key: 'ema_slow',        label: 'EMA 21',        ckey: 'bot3',   width: 1, style: 0, defOn: false, group: '均線' },
  { key: 'ema_fast',        label: 'EMA 9',         ckey: 'bot4',   width: 1, style: 0, defOn: false, group: '均線' },
  { key: 'donchian_upper',  label: 'DC 上軌',        ckey: 'faint',  width: 1, style: 2, defOn: false, group: '通道' },
  { key: 'donchian_lower',  label: 'DC 下軌',        ckey: 'faint',  width: 1, style: 2, defOn: false, group: '通道' },
  // 費波那契通道：0 與 1.0 為錨點（實線），中間比率與延伸線（虛線）。漲跌雙向自動切換。
  // 預設只開 3 條關鍵線（0 原點 / 0.618 黃金 / 1.0 目標），中間比率預設關閉避免雜訊，需要時手動開。
  { key: 'fib_ch_0',        label: 'FC 0',           ckey: 'bot3',   width: 2, style: 0, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_236',      label: 'FC 0.236',       ckey: 'muted',  width: 1, style: 2, defOn: false, group: 'Fib 通道' },
  { key: 'fib_ch_382',      label: 'FC 0.382',       ckey: 'muted',  width: 1, style: 2, defOn: false, group: 'Fib 通道' },
  { key: 'fib_ch_5',        label: 'FC 0.5',         ckey: 'muted',  width: 1, style: 2, defOn: false, group: 'Fib 通道' },
  { key: 'fib_ch_618',      label: 'FC 0.618',       ckey: 'bot4',   width: 2, style: 0, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_786',      label: 'FC 0.786',       ckey: 'muted',  width: 1, style: 2, defOn: false, group: 'Fib 通道' },
  { key: 'fib_ch_100',      label: 'FC 1.0',         ckey: 'bot3',   width: 2, style: 0, defOn: true,  group: 'Fib 通道' },
  { key: 'fib_ch_1272',     label: 'FC 1.272',       ckey: 'neg',    width: 1, style: 1, defOn: false, group: 'Fib 延伸' },
  { key: 'fib_ch_1618',     label: 'FC 1.618',       ckey: 'neg',    width: 1, style: 1, defOn: false, group: 'Fib 延伸' },
  { key: 'fib_ch_200',      label: 'FC 2.0',         ckey: 'neg',    width: 1, style: 1, defOn: false, group: 'Fib 延伸' },
]

const ALL_KEYS = OVERLAYS.map(o => o.key)
const initVis = Object.fromEntries(OVERLAYS.map(o => [o.key, o.defOn]))

export default function Chart() {
  const klinePollMs = Number(import.meta.env.VITE_CHART_KLINE_POLL_MS || 3000)
  const theme = useTheme()                   // 'dark'|'light'；切換時重繪本元件（canvas 重新取色）
  const containerRef = useRef(null)
  const chartRef     = useRef(null)
  const seriesRef    = useRef({})
  const dataRef      = useRef({})
  const markersRef   = useRef(null)          // createSeriesMarkers plugin api
  const markerDataRef = useRef([])           // 原始標記（後端回傳）

  const [bots,        setBots]        = useState([])    // 出現過的 strategy 清單（圖例）
  const [showMarkers, setShowMarkers] = useState(true)  // 交易點總開關
  const [hiddenBots,  setHiddenBots]  = useState(new Set())  // 個別隱藏的 bot

  const [symbol,    setSymbol]    = useState('BTCUSDT')
  const [tf,        setTf]        = useState('4h')
  const [loading,   setLoading]   = useState(false)
  const [errMsg,    setErrMsg]    = useState(null)
  const [vis,       setVis]       = useState(initVis)
  const [lastTs,    setLastTs]    = useState(null)
  const [livePrice, setLivePrice] = useState(null)
  const [prevPrice, setPrevPrice] = useState(null)
  const [wsReady,   setWsReady]   = useState(false)
  const [refresh,   setRefresh]   = useState(0)

  // ── chart init ──────────────────────────────────────────────────────
  // 依賴 theme：主題切換時整張圖重建，canvas 才會吃到新色（CSS var 不會自動套到 canvas）。
  // getChartColors() 必須在 effect 內呼叫，才會讀到當前主題的實際色字串。
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const c = getChartColors()
    const chart = createChart(el, {
      layout: { background: { color: c.bg }, textColor: c.text },
      grid:   { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: c.border, textColor: c.text },
      timeScale: {
        borderColor: c.border, timeVisible: true, secondsVisible: false,
        rightOffset: 12, barSpacing: 12, minBarSpacing: 4,
      },
      width: el.clientWidth, height: 460,
    })

    seriesRef.current.candles = chart.addSeries(CandlestickSeries, {
      upColor:        c.up, downColor:       c.down,
      borderUpColor:  c.up, borderDownColor: c.down,
      wickUpColor:    c.up, wickDownColor:   c.down,
    })
    // 交易標記層（v5 plugin），附在 K 線 series 上
    markersRef.current = createSeriesMarkers(seriesRef.current.candles, [])

    OVERLAYS.forEach(({ key, ckey, width, style }) => {
      const s = chart.addSeries(LineSeries, {
        color: c[ckey], lineWidth: width, lineStyle: style,
        priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
      })
      s.setData([])
      seriesRef.current[key] = s
    })

    chartRef.current = chart

    // 主題切換重建後，從 dataRef 還原既有 K 線/疊圖（避免空圖，也不必重打 API）。
    const d = dataRef.current
    if (d && d.candles?.length) {
      seriesRef.current.candles.setData(d.candles)
      ALL_KEYS.forEach(k => seriesRef.current[k]?.setData(vis[k] ? (d[k] ?? []) : []))
      const n = d.candles.length
      requestAnimationFrame(() => {
        if (chartRef.current) {
          chartRef.current.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90), to: n + 12 })
        }
      })
    }

    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }))
    ro.observe(el)
    return () => { ro.disconnect(); chart.remove(); chartRef.current = null; seriesRef.current = {} }
  }, [theme]) // eslint-disable-line react-hooks/exhaustive-deps

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
        // 預設聚焦最近 ~90 根，讓 K 棒放大、交易點散開（避免 300 根全擠成一團）。
        // setData 在新資料上會排程一次 auto-fit，故用 rAF 延後設定可見範圍以覆蓋 auto-fit。
        const n = d.candles?.length ?? 0
        requestAnimationFrame(() => {
          if (!chartRef.current) return
          if (n > 0) chartRef.current.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 90), to: n + 12 })
          else chartRef.current.timeScale().scrollToRealTime()
        })
        const last = d.candles?.[d.candles.length - 1]
        if (last) { setLastTs(last.time); setLivePrice(last.close) }
        setLoading(false)
      })
      .catch(e => { setErrMsg(e.message); setLoading(false) })
  }, [symbol, tf, refresh]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── Binance REST kline 輪詢（每 1.5 秒更新最後一根 K 棒；WS 在部分環境收不到訊息）──
  useEffect(() => {
    setWsReady(false)
    let alive = true

    async function poll() {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
      try {
        const res = await fetch(BINANCE_KLINES(symbol, tf, 2))
        const arr = await res.json()
        if (!alive || !Array.isArray(arr) || !arr.length) return
        const series = seriesRef.current.candles
        if (!series) return
        const cache = dataRef.current?.candles
        // 只更新 >= 現有最後一根的 K 棒，避免 lightweight-charts update() 對舊資料報錯
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
    const t = setInterval(poll, klinePollMs)
    return () => { alive = false; clearInterval(t) }
  }, [symbol, tf, klinePollMs]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── 交易標記：依 bot 配色 + 進出場形狀 + 聚合筆數，套到 K 線 ──────────────
  const applyMarkers = useCallback((raw, botNames, show, hidden) => {
    if (!markersRef.current) return
    if (!show || !raw?.length) { markersRef.current.setMarkers([]); return }
    const c = getChartColors()   // canvas 標記需實際色字串（CSS var 不適用），即時取當前主題色
    const ms = raw
      .filter(m => !hidden?.has(m.strategy))   // 過濾個別隱藏的 bot
      .map(m => {
        const color = colorForBot(c, botNames, m.strategy)
        const text  = m.count > 1 ? `×${m.count}` : undefined  // 聚合多筆 → ×N
        if (m.side === 'entry') {
          const short = m.dir < 0
          return { time: m.time, position: short ? 'aboveBar' : 'belowBar',
                   color, shape: short ? 'arrowDown' : 'arrowUp', text }
        }
        // 出場：嵌在 K 線內，不跟進場點搶位置
        return { time: m.time, position: 'inBar', color, shape: 'circle', text }
      })
    markersRef.current.setMarkers(ms)
  }, [theme])  // 主題切換 → 重算標記色（圖重建後由 re-apply effect 重新套用）

  // ── fetch 交易標記（換標的/重整時；每 6 小時聚合一點）───────────────────
  useEffect(() => {
    api.tradeMarkers(symbol, 6)
      .then(d => {
        markerDataRef.current = d.markers ?? []
        const bl = d.bots ?? []
        setBots(bl)
        applyMarkers(d.markers ?? [], bl.map(b => b.strategy), showMarkers, hiddenBots)
      })
      .catch(() => { markerDataRef.current = []; setBots([]); applyMarkers([], [], false, hiddenBots) })
  }, [symbol, refresh, applyMarkers]) // eslint-disable-line react-hooks/exhaustive-deps

  // ── 開關切換 / 個別隱藏 → 重套標記 ──────────────────────────────────
  useEffect(() => {
    applyMarkers(markerDataRef.current, bots.map(b => b.strategy), showMarkers, hiddenBots)
  }, [showMarkers, hiddenBots, bots, applyMarkers])

  const toggle = useCallback(key => {
    setVis(prev => {
      const next = { ...prev, [key]: !prev[key] }
      seriesRef.current[key]?.setData(next[key] ? (dataRef.current[key] ?? []) : [])
      return next
    })
  }, [])

  const priceUp  = livePrice != null && prevPrice != null && livePrice >= prevPrice
  const priceClr = livePrice == null ? 'var(--muted)' : priceUp ? 'var(--pos)' : 'var(--neg)'

  // active = 扁平 accent-soft 底 + 1px 同色框（無 neon glow）；color 用來標示文字/框色
  const chip = (active, color = 'var(--accent)') => ({
    padding: '3px 10px', borderRadius: 'var(--radius-pill)', border: 'none', cursor: 'pointer',
    fontSize: 11, fontWeight: 600, fontFamily: 'var(--font-display)',
    background: active ? 'var(--accent-soft)' : 'transparent',
    color:      active ? color : 'var(--faint)',
    outline:    active ? `1px solid ${color}` : '1px solid var(--line)',
    transition: `all var(--dur-fast) var(--ease)`,
  })

  // DOM 用的圖表色（chip 標籤色 / 圖例點需與 canvas 線/標記同色）。
  // 隨 theme 重算（useTheme 已使本元件在切換時 re-render）。
  const cDom = getChartColors()
  void theme   // 明示依賴 theme 重算

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>

      <Plain>
        放大版 K 線圖（蠟燭：綠漲紅跌）。上面疊的線是策略用的技術線（如費波那契通道、均線），
        圖上的 <b>▲買 / ▼空 / ●平</b> 標記是<b>四台 bot 真實的進出場點</b>（不同顏色代表不同 bot），
        可對照「它在什麼價位進出、現在賺賠如何」。換交易對 / 週期看不同市場。
      </Plain>

      {/* ── price header ── */}
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 10, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--faint)', fontFamily: 'var(--font-display)', marginBottom: 2 }}>
            {symbol} · {tf} · Binance Futures
          </div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
            <span style={{ fontSize: 32, fontWeight: 700, fontFamily: 'var(--font-display)', color: priceClr, letterSpacing: '-0.5px' }}>
              {livePrice != null
                ? livePrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                : '—'}
            </span>
            <span style={{ fontSize: 13, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>USDT</span>
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4, marginBottom: 4, marginLeft: 8 }}>
          <span style={{ fontSize: 10, color: wsReady ? 'var(--pos)' : 'var(--faint)', fontFamily: 'var(--font-display)', display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: wsReady ? 'var(--pos)' : 'var(--faint)', display: 'inline-block', animation: wsReady ? 'pulse 1.5s infinite' : 'none' }} />
            {wsReady ? 'LIVE' : '連線中…'}
          </span>
          {lastTs && (
            <span style={{ fontSize: 10, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>
              K棒：{new Date(lastTs * 1000).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
            </span>
          )}
        </div>
      </div>

      {/* ── controls row ── */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        {/* symbol */}
        {SYMBOLS.map(s => (
          <button key={s} onClick={() => setSymbol(s)} style={chip(symbol === s, 'var(--accent)')}>
            {s.replace('USDT', '')}
          </button>
        ))}
        <div style={{ width: 1, height: 16, background: 'var(--line-strong)', margin: '0 2px' }} />
        {/* timeframe */}
        {TFS.map(t => (
          <button key={t} onClick={() => setTf(t)} style={chip(tf === t, 'var(--accent)')}>
            {t}
          </button>
        ))}
        {/* manual refresh */}
        <button onClick={() => setRefresh(r => r + 1)} disabled={loading}
          style={{ ...chip(false), color: loading ? 'var(--faint)' : 'var(--muted)', padding: '3px 8px' }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      {/* ── indicator toggles ── */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {OVERLAYS.map(({ key, label, ckey }) => (
          <button key={key} onClick={() => toggle(key)} style={chip(vis[key], cDom[ckey])}>
            {label}
          </button>
        ))}
      </div>

      {/* ── 交易點：總開關 + bot 圖例（可逐一點擊隱藏）── */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <button onClick={() => setShowMarkers(s => !s)} style={chip(showMarkers, 'var(--warn)')}>
          ⚲ 交易點
        </button>
        {showMarkers && bots.length > 0 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
            {bots.map(b => {
              const names   = bots.map(x => x.strategy)
              const isBT    = b.mode === 'backtest'
              const hidden  = hiddenBots.has(b.strategy)
              const dotClr  = colorForBot(cDom, names, b.strategy)   // 與 canvas 標記同色
              const toggleBot = () => setHiddenBots(prev => {
                const next = new Set(prev)
                hidden ? next.delete(b.strategy) : next.add(b.strategy)
                return next
              })
              return (
                <button key={b.strategy} onClick={toggleBot}
                  title={hidden ? '點擊顯示' : '點擊隱藏'}
                  style={{ display: 'flex', alignItems: 'center', gap: 4,
                    fontSize: 11, fontFamily: 'var(--font-display)',
                    background: 'none', border: 'none', cursor: 'pointer', padding: '2px 6px',
                    borderRadius: 'var(--radius-sm)', outline: `1px solid ${hidden ? 'var(--line)' : dotClr}`,
                    opacity: hidden ? 0.35 : 1, transition: `opacity var(--dur-fast) var(--ease),outline var(--dur-fast) var(--ease)` }}>
                  <span style={{ width: 8, height: 8, borderRadius: '50%',
                    background: hidden ? 'var(--faint)' : dotClr, display: 'inline-block',
                    transition: `background var(--dur-fast) var(--ease)` }} />
                  <span style={{ color: hidden ? 'var(--faint)' : 'var(--text)' }}>{b.strategy}</span>
                  <span style={{ fontSize: 10, padding: '1px 4px', borderRadius: 4,
                    background: isBT ? 'var(--neg-soft)' : 'var(--pos-soft)',
                    color: isBT ? 'var(--neg)' : 'var(--muted)' }}>
                    {modeLabel(b.mode)}{isBT ? ' ⚠' : ''}
                  </span>
                  <span style={{ fontSize: 10, color: 'var(--faint)' }}>{b.count}筆</span>
                </button>
              )
            })}
            <span style={{ fontSize: 10, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>
              ↑多進 ↓空進 ●出場
            </span>
          </div>
        )}
        {showMarkers && bots.length === 0 && (
          <span style={{ fontSize: 11, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>
            此標的目前無交易紀錄
          </span>
        )}
      </div>
      {showMarkers && bots.some(b => b.mode === 'backtest') && (
        <div style={{ fontSize: 10, color: 'var(--neg)', fontFamily: 'var(--font-display)', marginTop: -6 }}>
          ⚠ 標「回測」者為回測產出資料（每筆只記出場、無進場點），非實盤下單
        </div>
      )}

      {/* ── chart ── */}
      <div ref={containerRef} style={{
        borderRadius: 'var(--radius)', overflow: 'hidden',
        border: '1px solid var(--line)',
        background: 'var(--surface)',
        minHeight: 460,
      }} />

      {errMsg && <div style={{ fontSize: 11, color: 'var(--neg)' }}>⚠ {errMsg}</div>}

      <style>{`
        @keyframes pulse {
          0%, 100% { opacity: 1 }
          50%       { opacity: 0.3 }
        }
      `}</style>
    </div>
  )
}
