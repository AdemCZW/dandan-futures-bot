import { useEffect, useState } from 'react'
import { api } from '../api'
import { Plain } from './Hint'

// 決策流程（簡化版，GitHub Pages 建置用）：只顯示各台 bot「目前這一根」的 SOP 決策，
// 不重跑歷史（那需要 vectorbt/回測模組，這裡刻意不含，bot 容器才不會背肥依賴）。
// 資料直接來自 bot 的 last_decision（與本機完整版 Explain 同一個 dict 結構）。

const TARGET_LABEL = { 1: '做多 (+1)', 0: '空手 (0)', '-1': '做空 (-1)' }

function targetBadgeClass(t) {
  if (t === 1 || t === '1') return 'badge-long'
  if (t === -1 || t === '-1') return 'badge-short'
  return 'badge-flat'
}

const ACT_LABEL = {
  entry: '進場做多', entry_short: '進場做空', exit_signal: '訊號平倉',
  exit_sltp: '停損/停利', exit_tp: '停利目標', exit_sl: '停損',
  exit_trail: '移動停利', exit_breakeven: '保本出場', scale_out: '部分了結',
  exit_manual: '手動平倉', exit_reconciled: '對帳平倉', hold: '續抱', flat: '觀望',
  dcg_blocked: '通道護欄擋進場', ml_rejected: 'ML 過濾否決', skip_anomaly: '暴量跳過',
  cb_paused: '熔斷暫停中',
}

function actText(a) {
  let s = ACT_LABEL[a.act] || a.act
  if (a.price != null) s += ` @ ${a.price}`
  if (a.qty != null) s += ` ×${a.qty}`
  if (a.sl != null) s += ` [SL ${a.sl} / TP ${a.tp}]`
  return s
}

function Stage({ n, role, children }) {
  return (
    <div className="card" style={{ flex: 1, minWidth: 150, margin: 0 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
        <span className="num" style={{ color: 'var(--accent)', fontSize: 12, fontWeight: 600 }}>{n}</span>
        <span style={{ fontFamily: 'var(--font-mono)', fontSize: 9, fontWeight: 600,
                       letterSpacing: '0.1em', textTransform: 'uppercase', color: 'var(--muted)' }}>
          {role}
        </span>
      </div>
      <div style={{ fontSize: 12, marginTop: 5, color: 'var(--text)', lineHeight: 1.5 }}>{children}</div>
    </div>
  )
}

function BotDecision({ meta, data }) {
  const d = data?.last_decision
  return (
    <div className="panel" style={{ marginBottom: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
        <span style={{ fontFamily: 'var(--font-display)', fontWeight: 700, fontSize: 12 }}>
          {meta.id.toUpperCase()}
        </span>
        <span className="muted" style={{ fontSize: 11 }}>
          {meta.strategy} · {meta.symbol} · {meta.interval}
        </span>
        {d && (
          <span className="muted" style={{ fontSize: 10, marginLeft: 'auto' }}>{d.ts}</span>
        )}
      </div>
      {!d ? (
        <div className="muted" style={{ fontSize: 12 }}>尚無決策紀錄（剛啟動或還在暖機）</div>
      ) : (
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <Stage n="1" role="市場">
            價 <span className="num">{d.price}</span>
            {d.volume != null && <> · 量 <span className="num">{d.volume.toFixed?.(0) ?? d.volume}</span></>}
          </Stage>
          <Stage n="2" role="指標">
            {d.ind && Object.keys(d.ind).length
              ? Object.entries(d.ind).slice(0, 4).map(([k, v]) => (
                  <span key={k} style={{ marginRight: 6 }}>{k}=<span className="num">{v}</span></span>
                ))
              : <span className="muted">無</span>}
          </Stage>
          <Stage n="3" role="訊號">
            {TARGET_LABEL[d.pos_before]} →{' '}
            <span className={`badge ${targetBadgeClass(d.target)}`}>{TARGET_LABEL[d.target]}</span>
          </Stage>
          <Stage n="4" role="動作">
            {(d.actions || []).map(actText).join('；') || '—'}
          </Stage>
        </div>
      )}
    </div>
  )
}

export default function LiveDecisions() {
  const [bots, setBots] = useState([])
  const [err, setErr] = useState('')

  async function load() {
    setErr('')
    try {
      const list = await api.bots()
      const datas = await Promise.all(
        list.map(async (b) => ({ meta: b, data: await api.botLive(b.id).catch(() => null) }))
      )
      setBots(datas)
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  return (
    <div>
      <Plain>
        每台 bot「目前這一根」K 棒收盤時的實際決策過程（市場數據 → 指標 → 訊號 → 執行動作）。
        只顯示最新一筆，不重跑歷史——完整回放要在本機跑完整版看板。
      </Plain>
      {err && <div className="err">⚠ {err}</div>}
      {bots.map((b) => <BotDecision key={b.meta.id} meta={b.meta} data={b.data} />)}
    </div>
  )
}
