import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import MiniChart from './MiniChart'
import { pairTrades, calcBalances, lossStreak, roiPct, holdDuration, exitReason } from '../lib/trades'
import Hint, { Plain } from './Hint'

const BOT_COLORS   = ['var(--bot1)', 'var(--bot2)', 'var(--bot3)', 'var(--bot4)']
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

/** UTC 時間字串 → 台灣時間（UTC+8），顯示 MM-DD HH:mm。 */
function fmtTwTime(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T') + 'Z')
  if (isNaN(d.getTime())) return String(ts).slice(5, 16)   // 解析失敗回退原樣
  const tw = new Date(d.getTime() + 8 * 3600 * 1000)
  const p  = (x) => String(x).padStart(2, '0')
  return `${p(tw.getUTCMonth() + 1)}-${p(tw.getUTCDate())} ${p(tw.getUTCHours())}:${p(tw.getUTCMinutes())}`
}

/** 現在時刻 → 後端同格式 UTC 字串（供持倉持有時長計算）。 */
function nowUtcStr() {
  return new Date().toISOString().slice(0, 19).replace('T', ' ')
}

/** 平倉/部分列的類型標籤 + 顏色 + 詳細說明（tooltip）。 */
function typeLabel(row) {
  if (row.kind === 'open') return { txt: '持有中', cls: 'badge-flat', desc: '目前持倉中，尚未平倉' }
  const r = exitReason(row.exit_type, row.pnl)
  const cls = row.kind === 'scale' ? 'badge-system'
            : r.tone === 'pos'     ? 'badge-long'
            : r.tone === 'neg'     ? 'badge-short'
            :                        'badge-flat'
  return { txt: r.label, cls, desc: r.desc }
}

/** 迷你權益曲線 SVG sparkline。bals = 每筆成交後餘額（最新在前）。*/
function EquitySpark({ bals, color, height = 48 }) {
  const pts = [...bals].reverse().filter(b => b != null)
  if (pts.length < 2) return null

  const W = 300, H = height
  const minV = Math.min(...pts)
  const maxV = Math.max(...pts)
  const range = maxV - minV || 1

  const toX = i  => (i / (pts.length - 1)) * W
  const toY = v  => H - ((v - minV) / range) * (H - 4) - 2

  const polyline = pts.map((v, i) => `${toX(i)},${toY(v)}`).join(' ')
  const area = [
    `M ${toX(0)},${toY(pts[0])}`,
    ...pts.slice(1).map((v, i) => `L ${toX(i + 1)},${toY(v)}`),
    `L ${W},${H} L 0,${H} Z`,
  ].join(' ')

  const up = pts[pts.length - 1] >= pts[0]
  const lineColor = up ? 'var(--pos)' : 'var(--neg)'

  return (
    <div style={{ marginTop: 4 }}>
      <div className="muted" style={{ fontSize: 10, marginBottom: 4, fontFamily: 'var(--font-display)' }}>
        權益曲線
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
           style={{ display: 'block', overflow: 'visible' }}>
        <defs>
          <linearGradient id={`eq-fill-${color.replace(/\W/g, '')}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={lineColor} stopOpacity="0.3" />
            <stop offset="100%" stopColor={lineColor} stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* baseline */}
        <line x1="0" y1={toY(INIT_CAPITAL)} x2={W} y2={toY(INIT_CAPITAL)}
              stroke="var(--faint)" strokeWidth="0.5" strokeDasharray="3 3" />
        {/* fill */}
        <path d={area} fill={`url(#eq-fill-${color.replace(/\W/g, '')})`} />
        {/* line */}
        <polyline points={polyline} fill="none" stroke={lineColor} strokeWidth="1.5"
                  strokeLinejoin="round" strokeLinecap="round" />
        {/* endpoint dot */}
        <circle cx={toX(pts.length - 1)} cy={toY(pts[pts.length - 1])} r="3"
                fill={lineColor} />
      </svg>
    </div>
  )
}

/** 持倉即時情況條：SL ←─ 進場 · 現價 ─→ TP，標出現價落在停損/停利之間的位置。 */
function PositionBar({ entry, sl, tp, price, dir }) {
  if (!sl || !tp || !price) return null
  const lo = Math.min(sl, tp), hi = Math.max(sl, tp)
  const span = hi - lo || 1
  const frac = (x) => Math.max(0, Math.min(1, (x - lo) / span)) * 100
  const slIsLeft = sl <= tp
  return (
    <div style={{ marginTop: 2 }}>
      <div style={{
        position: 'relative', height: 6, borderRadius: 3,
        background: slIsLeft
          ? 'linear-gradient(90deg, var(--neg-soft), var(--pos-soft))'
          : 'linear-gradient(90deg, var(--pos-soft), var(--neg-soft))',
      }}>
        {/* 進場點 */}
        <div style={{
          position: 'absolute', left: `${frac(entry)}%`, top: -2, width: 2, height: 10,
          background: 'var(--text)', transform: 'translateX(-1px)',
        }} />
        {/* 現價點 */}
        <div style={{
          position: 'absolute', left: `${frac(price)}%`, top: -3, width: 8, height: 12,
          background: 'var(--accent)', borderRadius: 2, transform: 'translateX(-4px)',
        }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, marginTop: 3 }}>
        <span className={slIsLeft ? 'neg' : 'pos'}>{slIsLeft ? `SL ${fmt(sl)}` : `TP ${fmt(tp)}`}</span>
        <span className={slIsLeft ? 'pos' : 'neg'}>{slIsLeft ? `TP ${fmt(tp)}` : `SL ${fmt(sl)}`}</span>
      </div>
    </div>
  )
}

/** 小統計格：標籤 + 數值（進階統計區用）。 */
function MiniStat({ label, children, title }) {
  return (
    <div title={title} style={{
      background: 'var(--surface-2)', borderRadius: 'var(--radius-sm)', padding: '6px 8px',
      cursor: title ? 'help' : 'default',
    }}>
      <div className="muted" style={{ fontSize: 9, marginBottom: 2 }}>{label}</div>
      <div className="num" style={{ fontSize: 12, fontWeight: 600 }}>{children}</div>
    </div>
  )
}

const winPctOf = (trades, wins) => (trades > 0 ? Math.round((wins / trades) * 100) : null)


// ── BotCard ─────────────────────────────────────────────────────────────────

function BotCard({ data, num, color }) {
  // 手動平倉狀態（hooks 必須無條件在最上方，故置於 !data 早退之前）
  const [closing, setClosing] = useState(false)
  const [closeMsg, setCloseMsg] = useState(null)

  async function handleClose() {
    const sym = String(data?.symbol || '').replace('USDT', '')
    if (!window.confirm(
      `確定要手動平倉 Bot #${num}（${sym}）目前的${data.direction === 1 ? '多單' : '空單'}嗎？\n` +
      `這會以市價立即平倉（測試網虛擬倉）。bot 之後仍會繼續自動交易。`)) return
    setClosing(true); setCloseMsg(null)
    try {
      const r = await api.closePosition(num)
      setCloseMsg(r?.ok ? { ok: true, text: r.msg || '已送出平倉' }
                        : { ok: false, text: r?.msg || '平倉失敗' })
    } catch (e) {
      setCloseMsg({ ok: false, text: String(e.message || e) })
    } finally {
      setClosing(false)
    }
  }

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

  const pairs  = pairTrades(data.recent_trades)
  const bals   = calcBalances(pairs, INIT_CAPITAL)
  const streak = lossStreak(pairs)

  // 持倉即時數據
  const openRow   = pairs.find(p => p.open)
  const scaledOut = openRow && openRow.orig_qty != null && openRow.qty < openRow.orig_qty - 1e-6
  const holdStr   = openRow ? holdDuration(openRow.entry_ts, nowUtcStr()) : '—'
  const distSL    = data.in_position && data.sl && data.price
    ? Math.abs(data.price - data.sl) / data.price * 100 : null
  const distTP    = data.in_position && data.tp && data.price
    ? Math.abs(data.tp - data.price) / data.price * 100 : null
  const posRoi    = roiPct(unreal, posVal)

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
            background: fresh ? 'var(--pos)' : 'var(--faint)', display: 'inline-block',
          }} />
          <span className="muted" style={{ fontSize: 10 }}>
            {fresh ? `${data.age_seconds}s 前` : '離線'}
          </span>
        </span>
      </div>

      {/* ── 連續同方向虧損警示（通道方向可能已反轉）── */}
      {streak && streak.count >= 2 && (
        <div style={{
          background: 'var(--neg-soft)', border: '1px solid var(--neg)',
          borderRadius: 'var(--radius-sm)', padding: '7px 10px', fontSize: 11, lineHeight: 1.4,
        }}>
          <span style={{ color: 'var(--neg)', fontWeight: 600 }}>⚠ 連續 {streak.count} 筆
            {streak.dir === 'short' ? '做空' : '做多'}虧損</span>
          <span className="muted">（{fmtSign(streak.totalPnl)}）— 通道方向可能已反轉，宜留意是否續逆勢接刀</span>
        </div>
      )}

      {/* ── 預算 / 已實現 / 未實現 ── */}
      <div style={{
        display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)',
        background: 'var(--surface-2)', borderRadius: 'var(--radius-sm)', padding: '10px 8px', gap: 4,
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

      {/* ── 進階統計（最大回撤 / 每筆夏普 / 多空拆分）── */}
      {data.total_trades > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: 6 }}>
          <MiniStat label="最大回撤"
                    title="以 $5,000 為基底的已實現權益曲線，歷史最大峰谷跌幅%">
            <span className="neg">
              {data.max_drawdown_pct != null ? `-${fmt(data.max_drawdown_pct)}%` : '—'}
            </span>
          </MiniStat>
          <MiniStat label="每筆夏普"
                    title="每筆交易報酬率（pnl/名目）的 平均 ÷ 標準差，衡量穩定度（非年化）">
            <span className={data.sharpe == null ? 'muted' : data.sharpe >= 0 ? 'pos' : 'neg'}>
              {data.sharpe != null ? fmt(data.sharpe, 2) : '—'}
            </span>
          </MiniStat>
          <MiniStat label={`多單 ${data.long_trades || 0} 筆`}
                    title="做多方向：勝率（勝筆/總筆）與累計損益">
            <span>{winPctOf(data.long_trades, data.long_wins) ?? '—'}
              {winPctOf(data.long_trades, data.long_wins) != null && '%'}</span>
            <span className={(data.long_pnl ?? 0) >= 0 ? 'pos' : 'neg'} style={{ marginLeft: 6, fontSize: 11 }}>
              {fmtSign(data.long_pnl)}
            </span>
          </MiniStat>
          <MiniStat label={`空單 ${data.short_trades || 0} 筆`}
                    title="做空方向：勝率（勝筆/總筆）與累計損益">
            <span>{winPctOf(data.short_trades, data.short_wins) ?? '—'}
              {winPctOf(data.short_trades, data.short_wins) != null && '%'}</span>
            <span className={(data.short_pnl ?? 0) >= 0 ? 'pos' : 'neg'} style={{ marginLeft: 6, fontSize: 11 }}>
              {fmtSign(data.short_pnl)}
            </span>
          </MiniStat>
        </div>
      )}

      {/* ── 權益曲線 ── */}
      <EquitySpark bals={bals} color={color} />

      {/* ── K 線 + 技術位（多時間框架切換）── */}
      <div>
        <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>
          K 線 · {String(data.strategy || '').replace('_', ' ')}
          <span style={{ marginLeft: 6, fontSize: 10 }}>{data.symbol}</span>
        </div>
        <MiniChart
          symbol={data.symbol}
          interval={data.interval}
          strategy={data.strategy}
          entry={data.entry_price}
          sl={data.sl}
          tp={data.tp}
          inPosition={!!data.in_position}
          trades={data.recent_trades}
        />
      </div>

      {/* ── 持倉即時情況 ── */}
      {data.in_position ? (
        <div style={{
          background: 'var(--surface-2)', borderRadius: 'var(--radius-sm)', padding: '8px 10px',
          display: 'flex', flexDirection: 'column', gap: 7,
        }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
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
            {scaledOut && (
              <span className="badge badge-system" style={{ fontSize: 9 }}>已部分了結</span>
            )}
            <span className="muted" style={{ fontSize: 11, marginLeft: 'auto' }}>持有 {holdStr}</span>
          </div>

          {/* 即時數據列 */}
          <div style={{ display: 'flex', gap: 14, fontSize: 12, flexWrap: 'wrap' }}>
            <span>
              倉位 <span className="num" style={{ color }}>${posVal.toLocaleString()}</span>
            </span>
            {data.price != null && (
              <span>現價 <span className="num">${fmt(data.price)}</span></span>
            )}
            <span className={`${unreal >= 0 ? 'pos' : 'neg'}`} style={{ marginLeft: 'auto', fontWeight: 600 }}>
              {fmtSign(unreal)} {posRoi != null && `(${fmtPct(posRoi)})`}
            </span>
          </div>

          {/* SL←現價→TP 即時情況條 */}
          <PositionBar entry={data.entry_price} sl={data.sl} tp={data.tp} price={data.price} dir={dir} />
          <div style={{ display: 'flex', gap: 14, fontSize: 11 }}>
            <span className="neg">距 SL {distSL != null ? distSL.toFixed(2) + '%' : '—'}</span>
            <span className="pos">距 TP {distTP != null ? distTP.toFixed(2) + '%' : '—'}</span>
          </div>

          {/* 手動平倉（結算）：市價立即平倉，bot 之後繼續自動交易 */}
          <button
            onClick={handleClose}
            disabled={closing}
            style={{
              marginTop: 2, padding: '6px 10px', fontSize: 12, fontWeight: 600,
              borderRadius: 'var(--radius-sm)', cursor: closing ? 'wait' : 'pointer',
              border: '1px solid var(--neg)', color: 'var(--neg)',
              background: 'var(--neg-soft)',
            }}
            title="以市價立即平掉目前持倉（測試網虛擬倉）。bot 之後仍會繼續自動交易。"
          >
            {closing ? '平倉中…' : '⏹ 手動平倉（結算）'}
          </button>
          {closeMsg && (
            <div className={closeMsg.ok ? 'pos' : 'neg'} style={{ fontSize: 11 }}>
              {closeMsg.ok ? '✓ ' : '⚠ '}{closeMsg.text}
            </div>
          )}
        </div>
      ) : (
        <div style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="badge badge-flat">空手</span>
          {data.price != null && (
            <span className="muted">現價 ${fmt(data.price)}</span>
          )}
        </div>
      )}

      {/* ── 交易紀錄（含部分了結、類型、報酬%、持有時長）── */}
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
          <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse', minWidth: 340 }}>
            <thead>
              <tr>
                {[
                  ['時間',   'left'],
                  ['類型',   'left'],
                  ['方向',   'left'],
                  ['進場',   'right'],
                  ['平倉',   'right'],
                  ['損益',   'right'],
                  ['報酬%',  'right'],
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
              {pairs.slice(0, 12).map((p, i) => {
                const tl  = typeLabel(p)
                const roi = roiPct(p.pnl, p.pos_value)
                const hold = p.open ? holdStr : holdDuration(p.entry_ts, p.ts)
                return (
                  <tr key={i} style={{ borderTop: '1px solid var(--line)' }}>
                    <td style={{ padding: '5px 0', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                      {fmtTwTime(p.ts)}
                      <div style={{ fontSize: 9, opacity: 0.7 }}>{hold !== '—' ? hold : ''}</div>
                    </td>
                    <td style={{ padding: '5px 4px 5px 0' }}>
                      <span className={`badge ${tl.cls}`} style={{ fontSize: 9, cursor: 'help' }}
                            title={tl.desc}>{tl.txt}</span>
                    </td>
                    <td style={{ padding: '5px 4px 5px 0' }}>
                      <span className={`badge ${p.dir === 'long' ? 'badge-long' : 'badge-short'}`}
                            style={{ fontSize: 9 }}>
                        {p.dir === 'long' ? '多' : '空'}
                      </span>
                    </td>
                    <td className="num" style={{ textAlign: 'right', padding: '5px 2px' }}>
                      {p.entry_price != null ? `$${fmt(p.entry_price)}` : '—'}
                    </td>
                    <td className="num" style={{ textAlign: 'right', padding: '5px 2px' }}>
                      {p.exit_price != null
                        ? `$${fmt(p.exit_price)}`
                        : <span className="muted" style={{ fontSize: 10 }}>持有中</span>
                      }
                    </td>
                    <td className={`num ${p.pnl == null ? '' : p.pnl >= 0 ? 'pos' : 'neg'}`}
                        style={{ textAlign: 'right', padding: '5px 2px', fontWeight: 600 }}>
                      {p.pnl != null ? fmtSign(p.pnl) : '—'}
                    </td>
                    <td className={`num ${roi == null ? '' : roi >= 0 ? 'pos' : 'neg'}`}
                        style={{ textAlign: 'right', padding: '5px 2px' }}>
                      {roi != null ? fmtPct(roi) : '—'}
                    </td>
                    <td className="num" style={{ textAlign: 'right', padding: '5px 0', color: 'var(--muted)' }}>
                      {bals[i] != null ? `$${fmt(bals[i])}` : '—'}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          </div>
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
  const [e4,   setE4]   = useState(null)
  const [tick, setTick] = useState(0)
  const timer = useRef(null)

  async function load() {
    try { setD(await api.live()) }   catch { /* ignore */ }
    try { setE2(await api.live2()) } catch { /* ignore */ }
    try { setE3(await api.live3()) } catch { /* ignore */ }
    try { setE4(await api.live4()) } catch { /* ignore */ }
  }

  useEffect(() => {
    load()
    timer.current = setInterval(() => { load(); setTick(t => t + 1) }, 5000)
    return () => clearInterval(timer.current)
  }, [])

  const anyFresh = [d, e2, e3, e4].some(x => x?.age_seconds != null && x.age_seconds < 180)

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

      <Plain>
        四台 bot 同時在<b>幣安合約測試網（虛擬資金、不是真錢）</b>自動交易。每張卡：<b>淨值</b>＝本金 $5,000 加減賺賠的現值；
        <b>已實現</b>＝平倉落袋的賺賠、<b>未實現</b>＝目前持倉的浮動賺賠；下方<b>進階統計</b>看回撤/夏普/多空拆分。
        持倉中會出現<b>「手動平倉」</b>鈕可立即結算該倉。
      </Plain>

      {/* ── 機器人卡片並排（桌面 2 欄、手機 1 欄）── */}
      <div className="bots-grid">
        <BotCard data={d}  num={1} color={BOT_COLORS[0]} />
        <BotCard data={e2} num={2} color={BOT_COLORS[1]} />
        <BotCard data={e3} num={3} color={BOT_COLORS[2]} />
        {e4?.configured && (
          <BotCard data={e4} num={4} color={BOT_COLORS[3]} />
        )}
      </div>
    </>
  )
}
