import { Fragment, useEffect, useState } from 'react'
import { api } from '../api'

const TARGET_LABEL = { 1: '做多 (+1)', 0: '空手 (0)', '-1': '做空 (-1)' }
const ACT_LABEL = {
  entry: '進場做多', entry_short: '進場做空', exit_signal: '信號平倉',
  exit_sltp: '停損/停利', exit_final: '收尾平倉', hold: '續抱', flat: '觀望',
}

// 目標倉位 → 徽章語意（多/空/空手），數值雙重編碼保留中文字
function targetBadgeClass(t) {
  if (t === 1 || t === '1') return 'badge-long'
  if (t === -1 || t === '-1') return 'badge-short'
  return 'badge-flat'
}

function actText(a) {
  let s = ACT_LABEL[a.act] || a.act
  if (a.price != null) s += ` @ ${a.price}`
  if (a.hit) s += ` (${a.hit === 'sl' ? '停損' : '停利'})`
  if (a.qty != null) s += ` ×${a.qty}`
  if (a.sl != null) s += ` [SL ${a.sl} / TP ${a.tp}]`
  return s
}

// 區塊小標：mono uppercase ＋ 青色短豎條識別碼
function Label({ children }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 8,
      fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 600,
      letterSpacing: '0.12em', textTransform: 'uppercase', color: 'var(--muted)',
    }}>
      <span style={{ width: 3, height: 12, background: 'var(--accent)', display: 'inline-block', flex: 'none' }} />
      {children}
    </div>
  )
}

// 逐關決策的單一角色關卡（HUD 小卡）
function Stage({ n, role, children }) {
  return (
    <div className="card" style={{ flex: 1, minWidth: 160, margin: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span className="num" style={{ color: 'var(--accent)', fontSize: 13, fontWeight: 600 }}>{n}</span>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 10, fontWeight: 600,
          letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)',
        }}>{role}</span>
      </div>
      <div style={{ fontSize: 13, marginTop: 6, color: 'var(--text)', lineHeight: 1.5 }}>{children}</div>
    </div>
  )
}

function Detail({ s }) {
  const ind = s.ind || {}
  return (
    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', padding: '12px 0 4px' }}>
      <Stage n="1" role="市場分析師">
        價 <span className="num">{s.close}</span> · 高 <span className="num">{s.high}</span> · 低 <span className="num">{s.low}</span>
        {s.volume != null ? <> · 量 <span className="num">{s.volume.toFixed(2)}</span></> : ''}
      </Stage>
      <Stage n="2" role="信號工程師">
        {Object.keys(ind).length
          ? Object.entries(ind).map(([k, v]) => <Fragment key={k}>{k}=<span className="num">{v}</span>{' '}·{' '}</Fragment>)
          : <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--muted-dim)' }}>// 無指標</span>}
      </Stage>
      <Stage n="3" role="量化研究員">
        目前 {TARGET_LABEL[s.pos_before]} → 目標{' '}
        <span className={`badge ${targetBadgeClass(s.target)}`} style={{ verticalAlign: 'middle' }}>{TARGET_LABEL[s.target]}</span>
      </Stage>
      <Stage n="4" role="風控官">
        {s.risk
          ? <>
              <span className={`badge ${s.risk.allow ? 'badge-long' : 'badge-short'}`}>{s.risk.allow ? '准入' : '否決'}</span>
              {' '}量 <span className="num">{s.risk.qty}</span> · {s.risk.reason}
            </>
          : '本根未觸發進場檢查'}
      </Stage>
      <Stage n="5" role="執行工程師">
        {s.actions.map(actText).join('；')} · 權益 <span className="num">{s.equity}</span>
      </Stage>
    </div>
  )
}

export default function Explain() {
  const [strats, setStrats] = useState([])
  const [strategy, setStrategy] = useState('ema_cross')
  const [source, setSource] = useState('synthetic')
  const [onlyDec, setOnlyDec] = useState(true)
  const [loading, setLoading] = useState(false)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState('')
  const [open, setOpen] = useState(null)

  useEffect(() => { api.strategies().then(setStrats).catch(() => {}) }, [])

  async function run() {
    setLoading(true); setErr(''); setOpen(null)
    try { setRes(await api.explain({ strategy, source, only_decisions: onlyDec })) }
    catch (e) { setErr(String(e.message || e)); setRes(null) }
    finally { setLoading(false) }
  }

  const pipeline = res?.pipeline || [
    { role: '市場分析師', does: '提供已收完 K 線（價、量）' },
    { role: '信號工程師', does: '算 EMA / RSI / ATR / z-score' },
    { role: '量化研究員', does: '依指標產生目標倉位 +1/0/-1' },
    { role: '風控官', does: '准入、倉位、停損停利、熔斷' },
    { role: '執行工程師', does: '對齊倉位、含手續費滑點成交' },
  ]

  return (
    <>
      <div className="panel">
        <Label>SOP 流程 · 6 角色管線</Label>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'stretch', marginTop: 12 }}>
          {pipeline.map((p, i, arr) => (
            <div key={i} style={{ display: 'flex', alignItems: 'stretch', flex: 1, minWidth: 140 }}>
              <div className="card" style={{ flex: 1, margin: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                  <span className="num" style={{ color: 'var(--accent)', fontSize: 16, fontWeight: 600 }}>{i + 1}</span>
                  <span className="display" style={{ color: 'var(--text-bright)', fontSize: 14 }}>{p.role}</span>
                </div>
                <div style={{ color: 'var(--muted)', fontSize: 11, lineHeight: 1.5 }}>{p.does}</div>
              </div>
              {i < arr.length - 1 && (
                <div style={{ color: 'var(--accent)', display: 'flex', alignItems: 'center', padding: '0 6px', fontFamily: 'var(--font-mono)' }}>→</div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="panel">
        <div className="controls">
          <div className="field"><label>策略</label>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {strats.map((s) => <option key={s.name} value={s.name}>{s.name}{s.allow_short ? ' (多空)' : ''}</option>)}
            </select>
          </div>
          <div className="field"><label>資料來源</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="synthetic">synthetic（離線）</option>
              <option value="testnet">testnet（公開行情）</option>
            </select>
          </div>
          <div className="field"><label>範圍</label>
            <select value={onlyDec ? 'dec' : 'all'} onChange={(e) => setOnlyDec(e.target.value === 'dec')}>
              <option value="dec">只看決策點（進出場）</option>
              <option value="all">每一根都看</option>
            </select>
          </div>
          <button className="run" onClick={run} disabled={loading}>{loading ? '執行中…' : '攤開決策'}</button>
        </div>
        {err && <div className="err">⚠ {err}</div>}
        {loading && <div className="spinner" style={{ marginTop: 10 }}>逐根記錄決策中…</div>}
      </div>

      {res && (
        <div className="panel">
          <Label>逐關決策紀錄</Label>
          <div className="muted" style={{ margin: '8px 0 12px' }}>
            <span className="num">{res.bars}</span> 根 · 決策點 <span className="num">{res.decision_points}</span> 個 · 點任一列展開該位置的逐關決策
          </div>
          <table>
            <thead><tr><th>時間</th><th>價格</th><th>信號</th><th>風控</th><th>動作</th><th></th></tr></thead>
            <tbody>
              {res.steps.map((s, i) => (
                <Fragment key={i}>
                  <tr onClick={() => setOpen(open === i ? null : i)} style={{ cursor: 'pointer' }}>
                    <td>{s.ts.slice(0, 16)}</td>
                    <td>{s.close}</td>
                    <td><span className={`badge ${targetBadgeClass(s.target)}`}>{TARGET_LABEL[s.target]}</span></td>
                    <td>
                      {s.risk
                        ? (s.risk.allow
                            ? <span className="badge badge-long">准入 ×{s.risk.qty}</span>
                            : <span className="badge badge-short">否決</span>)
                        : <span style={{ color: 'var(--muted-dim)' }}>—</span>}
                    </td>
                    <td>{s.actions.map((a) => ACT_LABEL[a.act] || a.act).join('；')}</td>
                    <td style={{ color: 'var(--accent)', textAlign: 'center' }}>{open === i ? '▲' : '▼'}</td>
                  </tr>
                  {open === i && <tr><td colSpan={6} style={{ background: 'var(--panel2)' }}><Detail s={s} /></td></tr>}
                </Fragment>
              ))}
              {res.steps.length === 0 && (
                <tr><td colSpan={6} style={{ fontFamily: 'var(--font-mono)', color: 'var(--muted-dim)' }}>// 此區間沒有進出場決策</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}
