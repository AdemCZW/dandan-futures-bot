import { useEffect, useRef, useState } from 'react'
import { api, cls } from '../api'
import TechPanel from './TechPanel.jsx'

const TLBL = { 1: '做多 (+1)', 0: '空手 (0)', '-1': '做空 (-1)' }
const ALBL = {
  entry: '進場做多', entry_short: '進場做空', exit_signal: '信號平倉', exit_sltp: '停損/停利',
  hold: '續抱', flat: '觀望', rejected: '風控否決', skip_anomaly: '暴量跳過',
}
function actT(a) {
  let s = ALBL[a.act] || a.act
  if (a.price != null) s += ` @ ${a.price}`
  if (a.qty != null) s += ` ×${a.qty}`
  if (a.sl != null) s += ` [SL ${a.sl} / TP ${a.tp}]`
  return s
}
function sideBadge(side) {
  const s = String(side || '')
  if (/空|short|sell|賣/i.test(s)) return 'badge-short'
  if (/多|long|buy|買/i.test(s)) return 'badge-long'
  return 'badge-flat'
}
function Stg({ n, role, children }) {
  return (
    <div style={{
      position: 'relative',
      background: 'var(--panel2)',
      borderRadius: 'var(--radius)',
      padding: '8px 12px',
      flex: 1,
      minWidth: 150,
      boxShadow: 'inset 2px 0 0 0 var(--accent)',
    }}>
      <div style={{
        fontFamily: 'var(--font-display)',
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        color: 'var(--accent)',
      }}>
        <span className="num">{n}</span> · {role}
      </div>
      <div style={{ fontSize: 13, marginTop: 4, color: 'var(--text)' }}>{children}</div>
    </div>
  )
}

function ExperimentStrip({ e }) {
  if (!e || !e.configured || !e.active) return null
  const posLabel = e.in_position ? (e.direction === -1 ? '持空' : '持多') : '空手'
  const pnl = e.unrealized_pnl
  return (
    <div className="panel" style={{ borderLeft: '3px solid var(--accent2, #e0397a)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span className="badge badge-flat" style={{ borderColor: 'var(--accent2,#e0397a)', color: 'var(--accent2,#e0397a)' }}>
          對照實驗
        </span>
        <h3 style={{ margin: 0 }}>短線對照組</h3>
        <span className="muted" style={{ fontSize: 12 }}>
          {e.strategy} · {e.symbol} {e.interval} · <span className="num">{posLabel}</span>
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 12 }} className="muted">
          權益 <span className="num">{e.equity != null ? e.equity.toFixed(2) : '—'}</span>
          {pnl != null && <> · 未實現 <span className={`num ${cls(pnl)}`}>{pnl >= 0 ? '+' : ''}{pnl}</span></>}
        </span>
      </div>
      <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
        ⚠ 已驗證【無 OOS edge】的短線策略，純為與上方長線 supertrend 4h 並行對照觀察（預期偏弱/虧損）。
      </div>
    </div>
  )
}

export default function Live() {
  const [d, setD] = useState(null)
  const [e2, setE2] = useState(null)
  const [err, setErr] = useState('')
  const [tick, setTick] = useState(0)
  const timer = useRef(null)

  async function load() {
    try { setD(await api.live()); setErr('') }
    catch (e) { setErr(String(e.message || e)) }
    try { setE2(await api.live2()) } catch { /* 對照組未設定時忽略 */ }
  }

  useEffect(() => {
    load()
    timer.current = setInterval(() => { load(); setTick((t) => t + 1) }, 5000)
    return () => clearInterval(timer.current)
  }, [])

  const fresh = d && d.age_seconds != null && d.age_seconds < (d.poll ? d.poll * 3 : 60)
  const posLabel = !d ? '' : (d.in_position ? (d.direction === -1 ? '持空' : '持多') : '空手')

  return (
    <>
      <div className={`panel${fresh ? ' is-active hud-neon-top' : ''}`}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span className={`status-pulse${fresh ? '' : ' is-offline'}`} />
          <h3 style={{ margin: 0, paddingLeft: 10 }}>即時監控 {fresh ? '· 運行中' : '· 待命/已停'}</h3>
          {d && (
            <span className={d.mode === 'futures' ? 'badge badge-system' : 'badge badge-flat'}>
              {d.mode === 'futures' ? '合約測試網' : 'Paper 模擬'}
            </span>
          )}
          <span className="badge badge-system" style={{ marginLeft: 'auto' }}>
            每 5 秒自動刷新（#<span className="num">{tick}</span>）
          </span>
        </div>
        {err && <div className="err">⚠ {err}</div>}
        {d && (
          <div className="muted" style={{ marginTop: 8 }}>
            {d.strategy} · {d.symbol} {d.interval} · 輪詢 <span className="num">{d.poll}</span>s ·
            最後更新 {d.age_seconds != null ? <><span className="num">{d.age_seconds}</span>s 前</> : '—'}
            {!d.active && ' · 尚未啟動（請先跑 run_paper.py 或 run_live_futures.py）'}
          </div>
        )}
      </div>

      <ExperimentStrip e={e2} />

      {d && d.active && (
        <>
          <div className="cards">
            <div className={`card${d.in_position ? (d.direction === -1 ? ' is-short' : ' is-long') : ''}`}>
              <div className="v">{posLabel}</div><div className="k">目前部位</div>
            </div>
            <div className="card glow-update" data-fresh={fresh ? '1' : '0'}>
              <div className="v num signal-glow" key={`price-${d.price}`}>{d.price != null ? d.price.toFixed(2) : '—'}</div>
              <div className="k">現價（即時）</div>
            </div>
            <div className="card glow-update" data-fresh={fresh ? '1' : '0'}>
              <div className="v num" key={`equity-${d.equity}`}>{d.equity != null ? d.equity.toFixed(2) : '—'}</div>
              <div className="k">權益（USDT）</div>
            </div>
            <div className="card">
              <div className={`v num signal-glow ${cls(d.unrealized_pnl)}`} key={`pnl-${d.unrealized_pnl}`}>{d.unrealized_pnl >= 0 ? '+' : ''}{d.unrealized_pnl}</div>
              <div className="k">未實現損益</div>
            </div>
          </div>

          {d.last_decision && (
            <div className={`panel${fresh ? ' is-active' : ''}`}>
              <h3 style={{ marginTop: 0 }}>本根決策（6 角色 SOP）· <span className="num">{String(d.last_decision.ts).slice(0, 16)}</span></h3>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Stg n="1" role="市場分析師">價 <span className="num">{d.last_decision.price}</span> · 高 <span className="num">{d.last_decision.high}</span> · 低 <span className="num">{d.last_decision.low}</span>{d.last_decision.volume != null ? <> · 量 <span className="num">{d.last_decision.volume}</span></> : ''}{d.last_decision.anomaly ? ' · ⚠ 暴量' : ''}</Stg>
                <Stg n="2" role="信號工程師">{Object.keys(d.last_decision.ind || {}).length ? <span className="num">{Object.entries(d.last_decision.ind).map(([k, v]) => `${k}=${v}`).join(' · ')}</span> : '—'}</Stg>
                <Stg n="3" role="量化研究員">目前 {TLBL[d.last_decision.pos_before]} → 目標{' '}
                  {d.last_decision.target != null
                    ? <span className={`badge ${d.last_decision.target === 1 ? 'badge-long' : d.last_decision.target === -1 ? 'badge-short' : 'badge-flat'}`}>{TLBL[d.last_decision.target]}</span>
                    : <span className="badge badge-flat">—</span>}
                </Stg>
                <Stg n="4" role="風控官">{d.last_decision.risk
                  ? <>{d.last_decision.risk.allow ? '准入' : '否決'} · 量 <span className="num">{d.last_decision.risk.qty}</span> · {d.last_decision.risk.reason}</>
                  : '本根未觸發進場檢查'}</Stg>
                <Stg n="5" role="執行工程師">{(d.last_decision.actions || []).map(actT).join('；') || '—'} · 權益 <span className="num">{d.last_decision.equity}</span></Stg>
              </div>
            </div>
          )}

          <TechPanel
            lastDecision={d.last_decision}
            price={d.price}
            inPos={d.in_position}
            direction={d.direction}
            entryPrice={d.entry_price}
            sl={d.sl}
            tp={d.tp}
          />

          {d.in_position && (
            <div className="panel is-active">
              <h3>持倉 · {d.direction === -1 ? '持空' : '持多'}</h3>
              <div className="cards">
                <div className="card"><div className="v num">{d.entry_price.toFixed(2)}</div><div className="k">進場價</div></div>
                <div className="card"><div className="v num neg">{d.sl != null ? d.sl.toFixed(2) : '—'}</div><div className="k">停損</div></div>
                <div className="card"><div className="v num pos">{d.tp != null ? d.tp.toFixed(2) : '—'}</div><div className="k">停利</div></div>
                <div className="card"><div className="v num">{d.base}</div><div className="k">持幣量</div></div>
                <div className="card"><div className="v num">{d.cash != null ? d.cash.toFixed(2) : '—'}</div><div className="k">現金</div></div>
              </div>
            </div>
          )}
          <div className="panel">
            <h3>近期成交（{d.mode === 'futures' ? '合約測試網' : 'Paper 模擬'}）</h3>
            <table>
              <thead><tr><th>時間</th><th>動作</th><th>價格</th><th>數量</th><th>損益</th></tr></thead>
              <tbody>
                {d.recent_trades.map((t, i) => (
                  <tr key={i}>
                    <td>{String(t.ts).slice(0, 16)}</td>
                    <td>
                      <span className={`badge ${sideBadge(t.side)}`}>{t.side}</span>
                    </td>
                    <td className="num">{Number(t.price).toFixed(2)}</td>
                    <td className="num">{Number(t.qty).toFixed(6)}</td>
                    <td className={`num ${cls(t.pnl)}`}>{t.pnl >= 0 ? '+' : ''}{Number(t.pnl).toFixed(2)}</td>
                  </tr>
                ))}
                {d.recent_trades.length === 0 && <tr><td colSpan={5} style={{ color: 'var(--muted-dim)', fontFamily: 'var(--font-mono)', textAlign: 'left' }}>// 無資料</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
