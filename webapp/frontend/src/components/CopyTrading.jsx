import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api'

// ─── shared helpers ───────────────────────────────────────────────────────────
const clr   = (v) => v == null ? '#8b949e' : v > 0 ? '#3fb950' : v < 0 ? '#f85149' : '#8b949e'
const fmt   = (v, d = 2) => v == null ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
const fmtK  = (v) => v == null ? '—' : v >= 1e6 ? `${fmt(v / 1e6, 1)}M` : v >= 1e3 ? `${fmt(v / 1e3, 1)}K` : fmt(v, 0)
const fmtTs = (ms) => new Date(ms).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', second: '2-digit' })

const cell = { padding: '9px 12px', fontSize: 12, fontFamily: 'var(--font-display)', borderBottom: '1px solid #161b22', verticalAlign: 'middle' }
const hdr  = { ...cell, color: '#484f58', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', background: '#161b22' }

// ─── HL Leaderboard section ────────────────────────────────────────────────────
function HlSection() {
  const [data, setData]       = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr]         = useState(null)

  const load = useCallback(async () => {
    setLoading(true); setErr(null)
    try { setData(await api.hlLeaderboard(20)) }
    catch (e) { setErr(e.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const traders = data?.traders ?? []
  const sum = data?.btc_summary ?? {}

  return (
    <div>
      {/* sub-header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: '#e6edf3', fontFamily: 'var(--font-display)' }}>
          HyperLiquid 頂級交易者
        </div>
        {sum.long != null && (
          <div style={{ display: 'flex', gap: 8, fontSize: 11, fontFamily: 'var(--font-display)' }}>
            <span style={{ color: '#3fb950' }}>▲ 多 {sum.long}</span>
            <span style={{ color: '#f85149' }}>▼ 空 {sum.short}</span>
            <span style={{ color: '#484f58' }}>— 平 {sum.flat}</span>
          </div>
        )}
        <button onClick={load} disabled={loading} style={{
          marginLeft: 'auto', padding: '3px 12px', borderRadius: 20, border: '1px solid #21262d',
          background: 'transparent', color: loading ? '#484f58' : '#8b949e',
          cursor: loading ? 'default' : 'pointer', fontSize: 11, fontFamily: 'var(--font-display)',
        }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      {err && <div style={{ fontSize: 11, color: '#f85149', marginBottom: 8 }}>⚠ {err}</div>}

      <div style={{ borderRadius: 8, overflow: 'hidden', border: '1px solid #21262d' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['#', '交易者', '帳戶', '日 PnL', '日 ROI', '週 PnL', 'BTC'].map(h => (
                <th key={h} style={hdr}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && traders.length === 0
              ? <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: '#484f58' }}>載入中…</td></tr>
              : traders.length === 0
              ? <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: '#484f58' }}>暫無資料</td></tr>
              : traders.map((t, i) => {
                  const dirClr = t.btc_direction === 'long' ? '#3fb950' : t.btc_direction === 'short' ? '#f85149' : '#484f58'
                  const dirLbl = t.btc_direction === 'long' ? '▲ 多' : t.btc_direction === 'short' ? '▼ 空' : '—'
                  return (
                    <tr key={t.address || i} style={{ background: 'transparent' }}>
                      <td style={{ ...cell, color: '#484f58', width: 30 }}>{i + 1}</td>
                      <td style={{ ...cell, color: '#e6edf3', fontWeight: 600, maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</td>
                      <td style={{ ...cell, color: '#8b949e' }}>${fmtK(t.account_value)}</td>
                      <td style={{ ...cell, color: clr(t.day_pnl) }}>{t.day_pnl > 0 ? '+' : ''}{fmtK(t.day_pnl)}</td>
                      <td style={{ ...cell, color: clr(t.day_roi), fontWeight: 700 }}>
                        {t.day_roi == null ? '—' : `${t.day_roi > 0 ? '+' : ''}${fmt(t.day_roi)}%`}
                      </td>
                      <td style={{ ...cell, color: clr(t.week_pnl) }}>{t.week_pnl > 0 ? '+' : ''}{fmtK(t.week_pnl)}</td>
                      <td style={{ ...cell, color: dirClr, fontWeight: 700, whiteSpace: 'nowrap' }}>
                        {dirLbl}{t.btc_size > 0 ? ` ${fmt(t.btc_size, 2)}` : ''}
                      </td>
                    </tr>
                  )
                })
            }
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ─── Binance Large Trades section ─────────────────────────────────────────────
const MIN_OPTS = [
  { label: '1萬U', val: 10_000 },
  { label: '5萬U', val: 50_000 },
  { label: '10萬U', val: 100_000 },
]

function LargeTradesSection() {
  const [trades, setTrades]   = useState([])
  const [loading, setLoading] = useState(false)
  const [minUsdt, setMinUsdt] = useState(10_000)
  const [autoSec, setAutoSec] = useState(10)
  const [countdown, setCd]    = useState(10)
  const newIds = useRef(new Set())

  const load = useCallback(async (threshold = minUsdt) => {
    setLoading(true)
    try {
      const d = await api.largeTrades('BTCUSDT', threshold, 200)
      const incoming = d.trades ?? []
      setTrades(prev => {
        const prevIds = new Set(prev.map(t => t.time + '_' + t.side + '_' + t.qty))
        newIds.current = new Set()
        incoming.forEach(t => {
          const id = t.time + '_' + t.side + '_' + t.qty
          if (!prevIds.has(id)) newIds.current.add(id)
        })
        return incoming
      })
    } catch { /* silent */ }
    finally { setLoading(false) }
  }, [minUsdt])

  // auto-refresh countdown
  useEffect(() => {
    if (autoSec === 0) { setCd(0); return }
    setCd(autoSec)
    load()
    const ivP = setInterval(() => load(), autoSec * 1000)
    let cd = autoSec
    const ivC = setInterval(() => { cd -= 1; if (cd <= 0) cd = autoSec; setCd(cd) }, 1000)
    return () => { clearInterval(ivP); clearInterval(ivC) }
  }, [autoSec, load])  // eslint-disable-line react-hooks/exhaustive-deps

  const chip = (active) => ({
    padding: '2px 9px', borderRadius: 20, border: 'none', cursor: 'pointer',
    fontSize: 10, fontWeight: 600, fontFamily: 'var(--font-display)',
    background: active ? '#58a6ff22' : 'transparent',
    color: active ? '#58a6ff' : '#484f58',
    outline: active ? '1px solid #58a6ff44' : '1px solid #21262d',
  })

  return (
    <div>
      {/* sub-header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: '#e6edf3', fontFamily: 'var(--font-display)' }}>
          OKX BTC 大單監控
        </div>
        {autoSec > 0 && (
          <span style={{ fontSize: 10, color: '#3fb950', fontFamily: 'var(--font-display)', display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 5, height: 5, borderRadius: '50%', background: '#3fb950', display: 'inline-block', animation: 'pulse 1.5s infinite' }} />
            {countdown}s
          </span>
        )}

        {/* 閾值 */}
        <div style={{ display: 'flex', gap: 4, marginLeft: 8 }}>
          {MIN_OPTS.map(o => (
            <button key={o.val} onClick={() => { setMinUsdt(o.val); load(o.val) }} style={chip(minUsdt === o.val)}>
              {o.label}
            </button>
          ))}
        </div>

        {/* 自動刷新 */}
        <div style={{ display: 'flex', gap: 4 }}>
          {[{ l: '關', v: 0 }, { l: '5s', v: 5 }, { l: '10s', v: 10 }, { l: '30s', v: 30 }].map(o => (
            <button key={o.v} onClick={() => setAutoSec(o.v)} style={chip(autoSec === o.v)}>{o.l}</button>
          ))}
        </div>

        <button onClick={() => load()} disabled={loading} style={{
          padding: '2px 8px', borderRadius: 20, border: '1px solid #21262d',
          background: 'transparent', color: loading ? '#484f58' : '#8b949e',
          cursor: loading ? 'default' : 'pointer', fontSize: 11, fontFamily: 'var(--font-display)',
        }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      <div style={{ borderRadius: 8, overflow: 'hidden', border: '1px solid #21262d' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['時間', '方向', '成交均價', '數量 (BTC)', '名義價值'].map(h => (
                <th key={h} style={hdr}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.length === 0
              ? <tr><td colSpan={5} style={{ ...cell, textAlign: 'center', color: '#484f58' }}>
                  {loading ? '載入中…' : '無符合大單'}
                </td></tr>
              : trades.slice(0, 30).map((t, i) => {
                  const isNew = newIds.current.has(t.time + '_' + t.side + '_' + t.qty)
                  const isBuy = t.side === 'buy'
                  return (
                    <tr key={i} style={{
                      background: isNew
                        ? (isBuy ? '#3fb95008' : '#f8514908')
                        : 'transparent',
                      transition: 'background 1s',
                    }}>
                      <td style={{ ...cell, color: '#484f58' }}>{fmtTs(t.time)}</td>
                      <td style={{ ...cell, color: isBuy ? '#3fb950' : '#f85149', fontWeight: 700 }}>
                        {isBuy ? '▲ 主買' : '▼ 主賣'}
                      </td>
                      <td style={{ ...cell, color: '#e6edf3' }}>${fmt(t.price)}</td>
                      <td style={{ ...cell, color: '#8b949e' }}>{fmt(t.qty, 3)}</td>
                      <td style={{ ...cell, color: isBuy ? '#3fb950' : '#f85149', fontWeight: 700 }}>
                        ${fmtK(t.usdt)}
                      </td>
                    </tr>
                  )
                })
            }
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 10, color: '#484f58', fontFamily: 'var(--font-display)', marginTop: 6 }}>
        OKX 永續合約 · 同單聚合 (同毫秒+同方向) · 主動買/賣方 · 顯示最近 30 筆
      </div>
    </div>
  )
}

// ─── Main Tab ─────────────────────────────────────────────────────────────────
export default function CopyTrading() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div style={{ fontSize: 11, color: '#484f58', fontFamily: 'var(--font-display)' }}>
        帶單追蹤 · HyperLiquid 頂級交易者 + OKX BTC 大單流
      </div>
      <HlSection />
      <LargeTradesSection />
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }`}</style>
    </div>
  )
}
