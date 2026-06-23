import { useEffect, useRef, useState } from 'react'
import { api, cls } from '../api'

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
function Stg({ n, role, children }) {
  return (
    <div style={{ background: 'var(--panel2)', border: '1px solid var(--border)', borderRadius: 8, padding: '8px 12px', flex: 1, minWidth: 150 }}>
      <div style={{ fontSize: 11, color: 'var(--accent)' }}>{n}. {role}</div>
      <div style={{ fontSize: 13, marginTop: 4 }}>{children}</div>
    </div>
  )
}

export default function Live() {
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')
  const [tick, setTick] = useState(0)
  const timer = useRef(null)

  async function load() {
    try { setD(await api.live()); setErr('') }
    catch (e) { setErr(String(e.message || e)) }
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
      <div className="panel">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ width: 10, height: 10, borderRadius: 999, background: fresh ? 'var(--green)' : 'var(--muted)' }} />
          <h3 style={{ margin: 0 }}>即時監控 {fresh ? '· 運行中' : '· 待命/已停'}</h3>
          {d && (
            <span style={{ fontSize: 11, padding: '2px 8px', borderRadius: 12,
              background: d.mode === 'futures' ? 'var(--accent)' : 'var(--panel2)',
              color: d.mode === 'futures' ? '#000' : 'var(--muted)',
              border: '1px solid var(--border)', fontWeight: 600 }}>
              {d.mode === 'futures' ? '合約測試網' : 'Paper 模擬'}
            </span>
          )}
          <span className="muted" style={{ marginLeft: 'auto' }}>每 5 秒自動刷新（#{tick}）</span>
        </div>
        {err && <div className="err">⚠ {err}</div>}
        {d && (
          <div className="muted" style={{ marginTop: 6 }}>
            {d.strategy} · {d.symbol} {d.interval} · 輪詢 {d.poll}s ·
            最後更新 {d.age_seconds != null ? `${d.age_seconds}s 前` : '—'}
            {!d.active && ' · 尚未啟動（請先跑 run_paper.py 或 run_live_futures.py）'}
          </div>
        )}
      </div>

      {d && d.active && (
        <>
          <div className="cards">
            <div className="card"><div className="v">{posLabel}</div><div className="k">目前部位</div></div>
            <div className="card"><div className="v">{d.price != null ? d.price.toFixed(2) : '—'}</div><div className="k">現價（即時）</div></div>
            <div className="card"><div className="v">{d.equity != null ? d.equity.toFixed(2) : '—'}</div><div className="k">權益（USDT）</div></div>
            <div className="card"><div className={`v ${cls(d.unrealized_pnl)}`}>{d.unrealized_pnl >= 0 ? '+' : ''}{d.unrealized_pnl}</div><div className="k">未實現損益</div></div>
          </div>

          {d.last_decision && (
            <div className="panel">
              <h3 style={{ marginTop: 0 }}>本根決策（6 角色 SOP）· {String(d.last_decision.ts).slice(0, 16)}</h3>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <Stg n="1" role="市場分析師">價 {d.last_decision.price} · 高 {d.last_decision.high} · 低 {d.last_decision.low}{d.last_decision.volume != null ? ` · 量 ${d.last_decision.volume}` : ''}{d.last_decision.anomaly ? ' · ⚠ 暴量' : ''}</Stg>
                <Stg n="2" role="信號工程師">{Object.keys(d.last_decision.ind || {}).length ? Object.entries(d.last_decision.ind).map(([k, v]) => `${k}=${v}`).join(' · ') : '—'}</Stg>
                <Stg n="3" role="量化研究員">目前 {TLBL[d.last_decision.pos_before]} → 目標 <b>{d.last_decision.target != null ? TLBL[d.last_decision.target] : '—'}</b></Stg>
                <Stg n="4" role="風控官">{d.last_decision.risk ? `${d.last_decision.risk.allow ? '准入' : '否決'} · 量 ${d.last_decision.risk.qty} · ${d.last_decision.risk.reason}` : '本根未觸發進場檢查'}</Stg>
                <Stg n="5" role="執行工程師">{(d.last_decision.actions || []).map(actT).join('；') || '—'} · 權益 {d.last_decision.equity}</Stg>
              </div>
            </div>
          )}

          {d.in_position && (
            <div className="panel">
              <h3>持倉</h3>
              <div className="cards">
                <div className="card"><div className="v">{d.entry_price.toFixed(2)}</div><div className="k">進場價</div></div>
                <div className="card"><div className="v neg">{d.sl != null ? d.sl.toFixed(2) : '—'}</div><div className="k">停損</div></div>
                <div className="card"><div className="v pos">{d.tp != null ? d.tp.toFixed(2) : '—'}</div><div className="k">停利</div></div>
                <div className="card"><div className="v">{d.base}</div><div className="k">持幣量</div></div>
                <div className="card"><div className="v">{d.cash != null ? d.cash.toFixed(2) : '—'}</div><div className="k">現金</div></div>
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
                    <td>{String(t.ts).slice(0, 16)}</td><td>{t.side}</td>
                    <td>{Number(t.price).toFixed(2)}</td><td>{Number(t.qty).toFixed(6)}</td>
                    <td className={cls(t.pnl)}>{t.pnl >= 0 ? '+' : ''}{Number(t.pnl).toFixed(2)}</td>
                  </tr>
                ))}
                {d.recent_trades.length === 0 && <tr><td colSpan={5} className="muted">（尚無成交）</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
