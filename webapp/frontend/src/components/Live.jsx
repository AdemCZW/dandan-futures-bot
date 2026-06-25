import { useEffect, useRef, useState } from 'react'
import { api, cls } from '../api'

const BOT_COLORS   = ['var(--accent)', '#e0397a', '#9b59b6']
const INIT_CAPITAL = 5000   // 每台機器人起始預算（測試網虛擬資金）

// ── 工具函式 ────────────────────────────────────────────────────────────────

function fmt(n, dec = 2) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toFixed(dec)
}
function fmtSign(n, dec = 2) {
  if (n == null || isNaN(n)) return '—'
  return (n >= 0 ? '+' : '') + Number(n).toFixed(dec)
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return '—'
  return (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%'
}

/** 把 recent_trades 配對成「開倉→平倉」回合（最新在前）。 */
function pairTrades(trades = []) {
  const ordered = [...trades].reverse()   // 轉時間正序
  const pairs = []
  let entry = null

  for (const t of ordered) {
    if (t.side === 'entry' || t.side === 'entry_short') {
      entry = t
    } else if (t.side && t.side.startsWith('exit') && entry) {
      pairs.push({
        dir:         entry.side === 'entry' ? 'long' : 'short',
        entry_price: entry.price,
        exit_price:  t.price,
        qty:         t.qty,
        pnl:         t.pnl,
        ts:          t.ts,
        pos_value:   Math.round(entry.qty * entry.price),
      })
      entry = null
    }
  }
  if (entry) {          // 尚未平倉的開倉（目前持倉）
    pairs.push({
      dir:         entry.side === 'entry' ? 'long' : 'short',
      entry_price: entry.price,
      exit_price:  null,
      qty:         entry.qty,
      pnl:         null,
      ts:          entry.ts,
      pos_value:   Math.round(entry.qty * entry.price),
      open:        true,
    })
  }
  return pairs.reverse()   // 最新在前
}

/** 從最新往前算每筆成交後的帳戶餘額。 */
function calcBalances(pairs, realized) {
  // 正向累計（最舊→最新）
  const ordered = [...pairs].reverse()
  let bal = INIT_CAPITAL
  const bals = []
  for (const p of ordered) {
    if (p.pnl != null) bal += p.pnl
    bals.push(p.pnl != null ? bal : null)
  }
  return bals.reverse()
}


// ── BotCard ─────────────────────────────────────────────────────────────────

function BotCard({ data, num, color }) {
  if (!data) return (
    <div className="panel" style={{ borderTop: `2px solid ${color}`, opacity: 0.4 }}>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-display)', color }}>Bot #{num}</div>
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>載入中…</div>
    </div>
  )

  const fresh    = data.age_seconds != null && data.age_seconds < (data.poll ? data.poll * 3 : 180)
  const realized = data.realized_pnl ?? 0
  const unreal   = data.unrealized_pnl ?? 0
  const netVal   = INIT_CAPITAL + realized + unreal
  const netPct   = (realized + unreal) / INIT_CAPITAL * 100
  const winPct   = data.total_trades > 0
    ? Math.round((data.win_trades / data.total_trades) * 100)
    : null

  const dir     = data.direction ?? 0
  const posBase = data.base ?? 0
  const posVal  = data.in_position && data.price ? Math.round(posBase * data.price) : 0

  const pairs = pairTrades(data.recent_trades)
  const bals  = calcBalances(pairs, realized)

  return (
    <div className="panel" style={{
      borderTop: `2px solid ${color}`,
      display: 'flex', flexDirection: 'column', gap: 12,
    }}>

      {/* ── 標頭 ── */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontFamily: 'var(--font-display)', fontSize: 11, fontWeight: 700, color }}>
          Bot #{num}
        </span>
        <span style={{ fontFamily: 'var(--font-display)', fontSize: 12, fontWeight: 600 }}>
          {String(data.strategy || '').toUpperCase()}
        </span>
        <span className="muted" style={{ fontSize: 11 }}>{data.symbol} · {data.interval}</span>
        <span style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 5 }}>
          <span style={{
            width: 6, height: 6, borderRadius: '50%',
            background: fresh ? '#3fb950' : '#484f58', display: 'inline-block',
          }} />
          <span className="muted" style={{ fontSize: 10 }}>
            {fresh ? `${data.age_seconds}s 前` : '離線'}
          </span>
        </span>
      </div>

      {/* ── 預算 / 已實現 / 未實現 ── */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)',
        background: 'var(--surface, #161b22)', borderRadius: 6, padding: '10px 8px', gap: 4,
      }}>
        {[
          { label: '預算',   value: `$${INIT_CAPITAL.toLocaleString()}`, c: '' },
          { label: '已實現', value: fmtSign(realized), c: realized >= 0 ? 'pos' : 'neg' },
          { label: '未實現', value: fmtSign(unreal),   c: unreal   >= 0 ? 'pos' : 'neg' },
        ].map(({ label, value, c }) => (
          <div key={label} style={{ textAlign: 'center' }}>
            <div className="muted" style={{ fontSize: 10, marginBottom: 3 }}>{label}</div>
            <div className={`num ${c}`} style={{ fontSize: 13, fontWeight: 600 }}>{value}</div>
          </div>
        ))}
      </div>

      {/* ── 淨值 ── */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span className="muted" style={{ fontSize: 11 }}>淨值</span>
        <span className="num" style={{ fontSize: 20, fontWeight: 700, color }}>
          ${fmt(netVal)}
        </span>
        <span className={`num ${netPct >= 0 ? 'pos' : 'neg'}`} style={{ fontSize: 13 }}>
          {fmtPct(netPct)}
        </span>
        {data.total_trades > 0 && (
          <span className="muted" style={{ fontSize: 11, marginLeft: 'auto' }}>
            {data.total_trades} 筆 · 勝率 {winPct}%
          </span>
        )}
      </div>

      {/* ── 持倉 ── */}
      {data.in_position ? (
        <div style={{
          background: 'var(--surface, #161b22)', borderRadius: 6, padding: '8px 10px',
          display: 'flex', flexDirection: 'column', gap: 6,
        }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span className={`badge ${dir === 1 ? 'badge-long' : 'badge-short'}`} style={{ fontSize: 11 }}>
              {dir === 1 ? '持多' : '持空'}
            </span>
            <span style={{ fontSize: 12 }}>
              進場 <span className="num">${fmt(data.entry_price)}</span>
            </span>
            <span style={{ fontSize: 12 }}>
              數量 <span className="num">
                {fmt(posBase, 4)} {String(data.symbol || '').replace('USDT', '')}
              </span>
            </span>
          </div>
          <div style={{ display: 'flex', gap: 14, fontSize: 12, flexWrap: 'wrap' }}>
            <span>
              倉位 <span className="num" style={{ color }}>${posVal.toLocaleString()}</span>
            </span>
            <span className="neg">SL <span className="num">${fmt(data.sl)}</span></span>
            <span className="pos">TP <span className="num">${fmt(data.tp)}</span></span>
            {data.price != null && (
              <span className={`${unreal >= 0 ? 'pos' : 'neg'}`} style={{ marginLeft: 'auto' }}>
                現價 ${fmt(data.price)} ({fmtSign(unreal)})
              </span>
            )}
          </div>
        </div>
      ) : (
        <div style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="badge badge-flat">空手</span>
          {data.price != null && (
            <span className="muted">現價 ${fmt(data.price)}</span>
          )}
        </div>
      )}

      {/* ── 交易紀錄 ── */}
      <div>
        <div style={{
          fontSize: 10, fontFamily: 'var(--font-display)',
          color: 'var(--muted)', marginBottom: 6,
        }}>
          交易紀錄
        </div>

        {pairs.length === 0 ? (
          <div className="muted" style={{ fontSize: 12 }}>// 尚無成交</div>
        ) : (
          <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                {[
                  ['時間',   'left'],
                  ['方向',   'left'],
                  ['開倉',   'right'],
                  ['平倉',   'right'],
                  ['倉位',   'right'],
                  ['損益',   'right'],
                  ['餘額',   'right'],
                ].map(([h, align]) => (
                  <th key={h} style={{
                    textAlign: align, paddingBottom: 4,
                    color: 'var(--muted)', fontWeight: 400, fontSize: 10,
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pairs.slice(0, 8).map((p, i) => (
                <tr key={i} style={{ borderTop: '1px solid var(--border, #21262d)' }}>
                  <td style={{ padding: '5px 0', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                    {String(p.ts || '').slice(5, 16)}
                  </td>
                  <td style={{ padding: '5px 4px 5px 0' }}>
                    <span className={`badge ${p.dir === 'long' ? 'badge-long' : 'badge-short'}`}
                          style={{ fontSize: 9 }}>
                      {p.dir === 'long' ? '多' : '空'}
                    </span>
                  </td>
                  <td className="num" style={{ textAlign: 'right', padding: '5px 2px' }}>
                    ${fmt(p.entry_price)}
                  </td>
                  <td className="num" style={{ textAlign: 'right', padding: '5px 2px' }}>
                    {p.exit_price != null
                      ? `$${fmt(p.exit_price)}`
                      : <span className="muted" style={{ fontSize: 10 }}>持有中</span>
                    }
                  </td>
                  <td className="num" style={{ textAlign: 'right', padding: '5px 2px', color: 'var(--muted)' }}>
                    ${(p.pos_value ?? 0).toLocaleString()}
                  </td>
                  <td className={`num ${p.pnl == null ? '' : p.pnl >= 0 ? 'pos' : 'neg'}`}
                      style={{ textAlign: 'right', padding: '5px 2px', fontWeight: 600 }}>
                    {p.pnl != null ? fmtSign(p.pnl) : '—'}
                  </td>
                  <td className="num" style={{ textAlign: 'right', padding: '5px 0', color: 'var(--muted)' }}>
                    {bals[i] != null ? `$${fmt(bals[i])}` : '—'}
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


// ── Live ─────────────────────────────────────────────────────────────────────

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
        <span className="muted" style={{ fontSize: 11, marginLeft: 4 }}>
          初始預算 ${INIT_CAPITAL.toLocaleString()} / 台
        </span>
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
