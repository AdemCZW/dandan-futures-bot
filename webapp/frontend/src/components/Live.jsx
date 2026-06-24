import { useEffect, useRef, useState } from 'react'
import { api, cls } from '../api'

const SIDE_CLS = { buy: 'badge-long', sell: 'badge-short', long: 'badge-long', short: 'badge-short' }
function sideCls(s) {
  const k = String(s || '').toLowerCase()
  return SIDE_CLS[k] || (/多|long|buy|買/i.test(k) ? 'badge-long' : /空|short|sell|賣/i.test(k) ? 'badge-short' : 'badge-flat')
}

const BOT_COLORS = ['var(--accent)', '#e0397a', '#9b59b6']
const BOT_LABELS = ['FIB_RETRACEMENT · SOL', 'FIB_CHANNEL · SOL', 'SMC_STRUCTURE · ETH']

function BotCard({ data, num, color }) {
  if (!data) return (
    <div className="panel" style={{ borderTop: `2px solid ${color}`, opacity: 0.4 }}>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-display)', color }}>Bot #{num}</div>
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>載入中…</div>
    </div>
  )

  const posLabel = data.in_position ? (data.direction === -1 ? '持空' : '持多') : '空手'
  const posCls   = data.in_position ? (data.direction === -1 ? 'badge-short' : 'badge-long') : 'badge-flat'
  const pnl      = data.unrealized_pnl
  const trades   = data.recent_trades || []
  const fresh    = data.age_seconds != null && data.age_seconds < (data.poll ? data.poll * 3 : 180)

  return (
    <div className="panel" style={{ borderTop: `2px solid ${color}`, display: 'flex', flexDirection: 'column', gap: 10 }}>

      {/* ── 標頭 ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{
          fontFamily: 'var(--font-display)', fontSize: 11, fontWeight: 700,
          letterSpacing: '0.1em', color,
        }}>Bot #{num}</span>
        <span style={{ fontFamily: 'var(--font-display)', fontSize: 12, fontWeight: 600 }}>
          {String(data.strategy || '').toUpperCase()}
        </span>
        <span className="muted" style={{ fontSize: 11 }}>{data.symbol} {data.interval}</span>
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%',
            background: fresh ? '#3fb950' : '#484f58',
            display: 'inline-block',
          }} />
          <span className="muted" style={{ fontSize: 10 }}>
            {fresh ? `${data.age_seconds}s 前` : '離線'}
          </span>
        </span>
      </div>

      {/* ── 狀態列 ── */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <span className={`badge ${posCls}`}>{posLabel}</span>
        <span style={{ fontSize: 13 }}>
          權益 <span className="num" style={{ color }}>{data.equity != null ? data.equity.toFixed(2) : '—'}</span>
        </span>
        {pnl != null && (
          <span style={{ fontSize: 13 }}>
            未實現 <span className={`num ${cls(pnl)}`}>{pnl >= 0 ? '+' : ''}{Number(pnl).toFixed(2)}</span>
          </span>
        )}
        {data.price != null && (
          <span className="muted" style={{ fontSize: 12, marginLeft: 'auto' }}>
            現價 <span className="num">{Number(data.price).toFixed(2)}</span>
          </span>
        )}
      </div>

      {/* ── 持倉細節 ── */}
      {data.in_position && (
        <div style={{ display: 'flex', gap: 12, fontSize: 12, flexWrap: 'wrap' }}>
          <span>進場 <span className="num">{Number(data.entry_price).toFixed(2)}</span></span>
          <span className="neg">SL <span className="num">{data.sl != null ? Number(data.sl).toFixed(2) : '—'}</span></span>
          <span className="pos">TP <span className="num">{data.tp != null ? Number(data.tp).toFixed(2) : '—'}</span></span>
        </div>
      )}

      {/* ── 交易紀錄 ── */}
      <div>
        <div style={{ fontSize: 11, fontFamily: 'var(--font-display)', color: 'var(--muted)', marginBottom: 6 }}>
          交易紀錄
        </div>
        {trades.length === 0 ? (
          <div className="muted" style={{ fontSize: 12 }}>// 尚無成交</div>
        ) : (
          <table style={{ width: '100%', fontSize: 12 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left', paddingBottom: 4, color: 'var(--muted)', fontWeight: 400 }}>時間</th>
                <th style={{ textAlign: 'left', paddingBottom: 4, color: 'var(--muted)', fontWeight: 400 }}>動作</th>
                <th style={{ textAlign: 'right', paddingBottom: 4, color: 'var(--muted)', fontWeight: 400 }}>價格</th>
                <th style={{ textAlign: 'right', paddingBottom: 4, color: 'var(--muted)', fontWeight: 400 }}>損益</th>
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 8).map((t, i) => (
                <tr key={i} style={{ borderTop: '1px solid var(--border, #21262d)' }}>
                  <td style={{ padding: '4px 0', color: 'var(--muted)', fontSize: 11 }}>
                    {String(t.ts || '').slice(5, 16)}
                  </td>
                  <td style={{ padding: '4px 6px 4px 0' }}>
                    <span className={`badge ${sideCls(t.side)}`} style={{ fontSize: 10 }}>{t.side}</span>
                  </td>
                  <td className="num" style={{ textAlign: 'right', padding: '4px 0' }}>
                    {Number(t.price).toFixed(2)}
                  </td>
                  <td className={`num ${cls(t.pnl)}`} style={{ textAlign: 'right', padding: '4px 0' }}>
                    {t.pnl != null ? `${t.pnl >= 0 ? '+' : ''}${Number(t.pnl).toFixed(2)}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

export default function Live() {
  const [d,    setD]    = useState(null)
  const [e2,   setE2]   = useState(null)
  const [e3,   setE3]   = useState(null)
  const [tick, setTick] = useState(0)
  const timer = useRef(null)

  async function load() {
    try { setD(await api.live()) }   catch { /* ignore */ }
    try { setE2(await api.live2()) } catch { /* ignore */ }
    try { setE3(await api.live3()) } catch { /* ignore */ }
  }

  useEffect(() => {
    load()
    timer.current = setInterval(() => { load(); setTick(t => t + 1) }, 5000)
    return () => clearInterval(timer.current)
  }, [])

  const anyFresh = [d, e2, e3].some(x => x?.age_seconds != null && x.age_seconds < 180)

  return (
    <>
      {/* ── 頂部狀態列 ── */}
      <div className={`panel${anyFresh ? ' is-active hud-neon-top' : ''}`}
           style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <span className={`status-pulse${anyFresh ? '' : ' is-offline'}`} />
        <h3 style={{ margin: 0, paddingLeft: 10 }}>
          即時監控 {anyFresh ? '· 運行中' : '· 待命'}
        </h3>
        <span className="badge badge-system">合約測試網</span>
        <span className="badge badge-system" style={{ marginLeft: 'auto' }}>
          每 5 秒自動刷新（#<span className="num">{tick}</span>）
        </span>
      </div>

      {/* ── 三台並排 ── */}
      <div style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(3, 1fr)',
        gap: 16,
        alignItems: 'start',
      }}>
        <BotCard data={d}  num={1} color={BOT_COLORS[0]} />
        <BotCard data={e2} num={2} color={BOT_COLORS[1]} />
        <BotCard data={e3} num={3} color={BOT_COLORS[2]} />
      </div>
    </>
  )
}
