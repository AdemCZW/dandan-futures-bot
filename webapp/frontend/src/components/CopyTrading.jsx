import { useState, useEffect, useCallback, useRef } from 'react'
import { api } from '../api'
import { Plain } from './Hint'

// ─── shared helpers ───────────────────────────────────────────────────────────
const clr   = (v) => v == null ? 'var(--muted)' : v > 0 ? 'var(--pos)' : v < 0 ? 'var(--neg)' : 'var(--muted)'
const fmt   = (v, d = 2) => v == null ? '—' : v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d })
const fmtK  = (v) => v == null ? '—' : v >= 1e6 ? `${fmt(v / 1e6, 1)}M` : v >= 1e3 ? `${fmt(v / 1e3, 1)}K` : fmt(v, 0)
const fmtTs = (ms) => new Date(ms).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', second: '2-digit' })

const cell = { padding: '9px 12px', fontSize: 12, fontFamily: 'var(--font-display)', borderBottom: '1px solid var(--line)', verticalAlign: 'middle' }
const hdr  = { ...cell, color: 'var(--muted)', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', borderBottom: '1px solid var(--line-strong)' }

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
        <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-strong)', fontFamily: 'var(--font-display)' }}>
          HyperLiquid 頂級交易者
        </div>
        {sum.long != null && (
          <div style={{ display: 'flex', gap: 8, fontSize: 11, fontFamily: 'var(--font-display)' }}>
            <span style={{ color: 'var(--pos)' }}>▲ 多 {sum.long}</span>
            <span style={{ color: 'var(--neg)' }}>▼ 空 {sum.short}</span>
            <span style={{ color: 'var(--faint)' }}>— 平 {sum.flat}</span>
          </div>
        )}
        <button onClick={load} disabled={loading} style={{
          marginLeft: 'auto', padding: '3px 12px', borderRadius: 'var(--radius-pill)', border: '1px solid var(--line-strong)',
          background: 'transparent', color: loading ? 'var(--faint)' : 'var(--muted)',
          cursor: loading ? 'default' : 'pointer', fontSize: 11, fontFamily: 'var(--font-display)',
        }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      {err && <div style={{ fontSize: 11, color: 'var(--neg)', marginBottom: 8 }}>⚠ {err}</div>}

      <div style={{ borderRadius: 'var(--radius-sm)', overflow: 'hidden', border: '1px solid var(--line-strong)' }}>
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
              ? <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: 'var(--faint)' }}>載入中…</td></tr>
              : traders.length === 0
              ? <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: 'var(--faint)' }}>暫無資料</td></tr>
              : traders.map((t, i) => {
                  const dirClr = t.btc_direction === 'long' ? 'var(--pos)' : t.btc_direction === 'short' ? 'var(--neg)' : 'var(--faint)'
                  const dirLbl = t.btc_direction === 'long' ? '▲ 多' : t.btc_direction === 'short' ? '▼ 空' : '—'
                  return (
                    <tr key={t.address || i} style={{ background: 'transparent' }}>
                      <td style={{ ...cell, color: 'var(--faint)', width: 30 }}>{i + 1}</td>
                      <td style={{ ...cell, color: 'var(--text)', fontWeight: 600, maxWidth: 120, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.name}</td>
                      <td style={{ ...cell, color: 'var(--muted)' }}>${fmtK(t.account_value)}</td>
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
    padding: '2px 9px', borderRadius: 'var(--radius-pill)', border: 'none', cursor: 'pointer',
    fontSize: 10, fontWeight: 600, fontFamily: 'var(--font-display)',
    background: active ? 'var(--accent-soft)' : 'transparent',
    color: active ? 'var(--accent)' : 'var(--faint)',
    outline: active ? '1px solid var(--accent)' : '1px solid var(--line-strong)',
  })

  return (
    <div>
      {/* sub-header */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-strong)', fontFamily: 'var(--font-display)' }}>
          OKX BTC 大單監控
        </div>
        {autoSec > 0 && (
          <span style={{ fontSize: 10, color: 'var(--pos)', fontFamily: 'var(--font-display)', display: 'flex', alignItems: 'center', gap: 4 }}>
            <span style={{ width: 5, height: 5, borderRadius: '50%', background: 'var(--pos)', display: 'inline-block', animation: 'pulse 1.5s infinite' }} />
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
          padding: '2px 8px', borderRadius: 'var(--radius-pill)', border: '1px solid var(--line-strong)',
          background: 'transparent', color: loading ? 'var(--faint)' : 'var(--muted)',
          cursor: loading ? 'default' : 'pointer', fontSize: 11, fontFamily: 'var(--font-display)',
        }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      <div style={{ borderRadius: 'var(--radius-sm)', overflow: 'hidden', border: '1px solid var(--line-strong)' }}>
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
              ? <tr><td colSpan={5} style={{ ...cell, textAlign: 'center', color: 'var(--faint)' }}>
                  {loading ? '載入中…' : '無符合大單'}
                </td></tr>
              : trades.slice(0, 30).map((t, i) => {
                  const isNew = newIds.current.has(t.time + '_' + t.side + '_' + t.qty)
                  const isBuy = t.side === 'buy'
                  return (
                    <tr key={i} style={{
                      background: isNew
                        ? (isBuy ? 'var(--pos-soft)' : 'var(--neg-soft)')
                        : 'transparent',
                      transition: 'background 1s',
                    }}>
                      <td style={{ ...cell, color: 'var(--faint)' }}>{fmtTs(t.time)}</td>
                      <td style={{ ...cell, color: isBuy ? 'var(--pos)' : 'var(--neg)', fontWeight: 700 }}>
                        {isBuy ? '▲ 主買' : '▼ 主賣'}
                      </td>
                      <td style={{ ...cell, color: 'var(--text)' }}>${fmt(t.price)}</td>
                      <td style={{ ...cell, color: 'var(--muted)' }}>{fmt(t.qty, 3)}</td>
                      <td style={{ ...cell, color: isBuy ? 'var(--pos)' : 'var(--neg)', fontWeight: 700 }}>
                        ${fmtK(t.usdt)}
                      </td>
                    </tr>
                  )
                })
            }
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 10, color: 'var(--faint)', fontFamily: 'var(--font-display)', marginTop: 6 }}>
        OKX 永續合約 · 同單聚合 (同毫秒+同方向) · 主動買/賣方 · 顯示最近 30 筆
      </div>
    </div>
  )
}

// ─── Binance Copy Trading Leaderboard ────────────────────────────────────────
function BinanceSection() {
  const [data, setData]         = useState(null)
  const [loading, setLoading]   = useState(false)
  const [err, setErr]           = useState(null)
  const [selected, setSelected] = useState(null)   // selected trader uid
  const [positions, setPositions] = useState([])
  const [posLoading, setPosLoading] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setErr(null)
    try { setData(await api.copytraders(20)) }
    catch (e) { setErr(e.message) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const selectTrader = useCallback(async (uid, positionShared) => {
    if (selected === uid) { setSelected(null); setPositions([]); return }
    setSelected(uid)
    setPositions([])
    if (!positionShared) return
    setPosLoading(true)
    try {
      const d = await api.copytraderPositions(uid)
      setPositions(d.positions ?? [])
    } catch { /* silent */ }
    finally { setPosLoading(false) }
  }, [selected])

  const traders = data?.traders ?? []

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10, flexWrap: 'wrap' }}>
        <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text-strong)', fontFamily: 'var(--font-display)' }}>
          幣安帶單排行榜
        </div>
        <span style={{ fontSize: 10, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>
          7日 ROI 降序 · 點擊查看持倉
        </span>
        <button onClick={load} disabled={loading} style={{
          marginLeft: 'auto', padding: '3px 12px', borderRadius: 'var(--radius-pill)', border: '1px solid var(--line-strong)',
          background: 'transparent', color: loading ? 'var(--faint)' : 'var(--muted)',
          cursor: loading ? 'default' : 'pointer', fontSize: 11, fontFamily: 'var(--font-display)',
        }}>
          {loading ? '…' : '↻'}
        </button>
      </div>

      {err && <div style={{ fontSize: 11, color: 'var(--neg)', marginBottom: 8 }}>⚠ {err}</div>}

      <Plain>
        這是<b>幣安官方的「帶單交易員」排行榜</b>（真人實盤），不是我們的 bot。
        <b>ROI</b>＝報酬率%（本金賺賠的比例）、<b>PnL</b>＝實際賺賠金額（USDT）、<b>勝率</b>＝賺錢單佔比。
        看高手怎麼布局、現在偏多偏空當參考；點一列可展開他的持倉。<b>過去績效不保證未來</b>。
      </Plain>

      <div style={{ borderRadius: 'var(--radius-sm)', overflow: 'hidden', border: '1px solid var(--line-strong)' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              {['#', '交易者', '粉絲', '7日 ROI', '7日 PnL', '勝率', '持倉'].map(h => (
                <th key={h} style={hdr}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading && traders.length === 0
              ? <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: 'var(--faint)' }}>載入中…</td></tr>
              : traders.length === 0
              ? <tr><td colSpan={7} style={{ ...cell, textAlign: 'center', color: 'var(--faint)' }}>暫無資料（幣安 API 可能不通）</td></tr>
              : traders.map((t, i) => {
                  const isSelected = selected === t.uid
                  return (
                    <tr
                      key={t.uid || i}
                      onClick={() => selectTrader(t.uid, t.position_shared)}
                      style={{
                        background: isSelected ? 'var(--accent-soft)' : 'transparent',
                        cursor: 'pointer',
                        outline: isSelected ? '1px solid var(--accent)' : 'none',
                      }}
                    >
                      <td style={{ ...cell, color: 'var(--faint)', width: 28 }}>{i + 1}</td>
                      <td style={{ ...cell, color: 'var(--text)', fontWeight: 600, maxWidth: 130, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {t.nickname}
                      </td>
                      <td style={{ ...cell, color: 'var(--muted)' }}>{fmtK(t.followers)}</td>
                      <td style={{ ...cell, color: clr(t.roi_7d), fontWeight: 700 }}>
                        {t.roi_7d == null ? '—' : `${t.roi_7d > 0 ? '+' : ''}${fmt(t.roi_7d)}%`}
                      </td>
                      <td style={{ ...cell, color: clr(t.pnl_7d) }}>
                        {t.pnl_7d == null ? '—' : `${t.pnl_7d > 0 ? '+' : ''}${fmtK(t.pnl_7d)}`}
                      </td>
                      <td style={{ ...cell, color: 'var(--muted)' }}>
                        {t.win_rate == null ? '—' : `${fmt(t.win_rate, 1)}%`}
                      </td>
                      <td style={{ ...cell, color: t.position_shared ? 'var(--pos)' : 'var(--faint)', fontSize: 10 }}>
                        {t.position_shared ? '公開' : '未分享'}
                      </td>
                    </tr>
                  )
                })
            }
          </tbody>
        </table>
      </div>

      {/* 持倉展開區 */}
      {selected && (
        <div style={{ marginTop: 8, borderRadius: 'var(--radius-sm)', border: '1px solid var(--line-strong)', overflow: 'hidden' }}>
          <div style={{ ...hdr, padding: '8px 12px', display: 'flex', alignItems: 'center', gap: 8 }}>
            <span>持倉明細</span>
            {posLoading && <span style={{ color: 'var(--faint)' }}>載入中…</span>}
          </div>
          {!posLoading && positions.length === 0
            ? <div style={{ ...cell, textAlign: 'center', color: 'var(--faint)' }}>無公開持倉</div>
            : positions.map((p, i) => {
                const isLong = p.direction === 'long'
                return (
                  <div key={i} style={{
                    display: 'grid', gridTemplateColumns: '1fr 60px 90px 90px 80px 70px',
                    gap: 8, padding: '8px 12px', borderBottom: '1px solid var(--line)',
                    fontSize: 12, fontFamily: 'var(--font-display)',
                  }}>
                    <span style={{ color: 'var(--text)', fontWeight: 600 }}>{p.symbol}</span>
                    <span style={{ color: isLong ? 'var(--pos)' : 'var(--neg)', fontWeight: 700 }}>
                      {isLong ? '▲ 多' : '▼ 空'}
                    </span>
                    <span style={{ color: 'var(--muted)' }}>入 ${fmt(p.entry_price, 2)}</span>
                    <span style={{ color: 'var(--muted)' }}>標 ${fmt(p.mark_price, 2)}</span>
                    <span style={{ color: clr(p.upnl) }}>{p.upnl > 0 ? '+' : ''}{fmt(p.upnl)}</span>
                    <span style={{ color: clr(p.roe) }}>{p.roe > 0 ? '+' : ''}{fmt(p.roe, 1)}% ROE</span>
                  </div>
                )
              })
          }
        </div>
      )}
    </div>
  )
}

// ─── Main Tab ─────────────────────────────────────────────────────────────────
export default function CopyTrading() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 24 }}>
      <div style={{ fontSize: 11, color: 'var(--faint)', fontFamily: 'var(--font-display)' }}>
        帶單追蹤 · 幣安帶單排行榜 + HyperLiquid 頂級交易者 + OKX BTC 大單流
      </div>
      <BinanceSection />
      <HlSection />
      <LargeTradesSection />
      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.3} }`}</style>
    </div>
  )
}
