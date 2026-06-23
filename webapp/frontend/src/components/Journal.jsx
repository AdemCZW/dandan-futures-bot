import { useEffect, useState } from 'react'
import { api, cls } from '../api'

const MODES = ['paper', 'backtest', 'live_testnet', 'live_testnet_ws', 'live_futures_testnet']

// 把 UTC 時間字串轉成台灣時間（UTC+8）
function toTaipei(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T') + 'Z')
  if (isNaN(d)) return String(ts).slice(0, 16)
  const tw = new Date(d.getTime() + 8 * 3600 * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${tw.getUTCFullYear()}/${p(tw.getUTCMonth() + 1)}/${p(tw.getUTCDate())} ${p(tw.getUTCHours())}:${p(tw.getUTCMinutes())}`
}

function labelSide(side) {
  const map = {
    entry:        { text: '進場做多',          color: 'pos' },
    entry_short:  { text: '進場做空',          color: 'neg' },
    exit_signal:  { text: '出場　信號觸發',    color: '' },
    exit_sltp:    { text: '出場　停損 / 停利', color: '' },
    exit_sl:      { text: '出場　停損',        color: 'neg' },
    exit_tp:      { text: '出場　停利',        color: 'pos' },
  }
  return map[side] ?? { text: side, color: '' }
}

function labelMode(mode) {
  const map = {
    live_futures_testnet: '合約測試網',
    paper:                'Paper 模擬',
    live_testnet:         '現貨測試網',
    live_testnet_ws:      '現貨測試網 WS',
    backtest:             '回測',
  }
  return map[mode] ?? mode
}

function labelStrategy(s) {
  const map = {
    fib_retracement: 'Fibonacci',
    ema_cross:       'EMA 交叉',
    zscore_ls:       'Z-Score 多空',
    zscore_revert:   'Z-Score 回歸',
  }
  return map[s] ?? s
}

export default function Journal() {
  const [rows, setRows] = useState([])
  const [mode, setMode] = useState('')
  const [err, setErr] = useState('')

  async function load() {
    setErr('')
    try { setRows(await api.trades(100, mode || undefined)) }
    catch (e) { setErr(String(e.message || e)) }
  }
  useEffect(() => { load() }, [mode])   // eslint-disable-line react-hooks/exhaustive-deps

  const exits = rows.filter(r => !String(r.side).startsWith('entry'))
  const wins = exits.filter(r => Number(r.pnl) > 0).length
  const totalPnl = exits.reduce((s, r) => s + (Number(r.pnl) || 0), 0)

  return (
    <div className="panel">
      <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap', gap: 8 }}>
        <div className="field">
          <label>來源篩選</label>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="">全部</option>
            {MODES.map((m) => <option key={m} value={m}>{labelMode(m)}</option>)}
          </select>
        </div>
        <button className="run" onClick={load}>重新整理</button>
        {exits.length > 0 && (
          <span style={{ fontSize: 13, lineHeight: '32px' }} className="muted">
            已出場 {exits.length} 筆 ·
            勝率 <strong>{(wins / exits.length * 100).toFixed(0)}%</strong> ·
            總損益 <strong className={cls(totalPnl)}>
              {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USDT
            </strong>
          </span>
        )}
      </div>

      {err && <div className="err">⚠ {err}</div>}
      <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>
        共 {rows.length} 筆 · 時間為台灣時間（UTC+8）· 最新交易在最上方
      </div>

      <table>
        <thead>
          <tr>
            <th>時間（台灣）</th>
            <th>來源</th>
            <th>策略</th>
            <th>動作說明</th>
            <th style={{ textAlign: 'right' }}>成交價</th>
            <th style={{ textAlign: 'right' }}>數量（BTC）</th>
            <th style={{ textAlign: 'right' }}>損益（USDT）</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => {
            const s = labelSide(t.side)
            const isEntry = String(t.side).startsWith('entry')
            const pnl = Number(t.pnl)
            return (
              <tr key={i}>
                <td style={{ fontVariantNumeric: 'tabular-nums', whiteSpace: 'nowrap' }}>
                  {toTaipei(t.ts)}
                </td>
                <td>{labelMode(t.mode)}</td>
                <td>{labelStrategy(t.strategy)}</td>
                <td className={s.color} style={{ fontWeight: 500 }}>
                  {s.text}
                </td>
                <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                  {Number(t.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                  {Number(t.qty).toFixed(6)}
                </td>
                <td style={{ textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}
                    className={isEntry ? 'muted' : cls(pnl)}>
                  {isEntry
                    ? '—'
                    : `${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)}`}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={7} className="muted" style={{ textAlign: 'center', padding: '24px 0' }}>
                尚無交易留底（跑 run_live_futures.py 或 run_paper.py 後會自動出現）
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
