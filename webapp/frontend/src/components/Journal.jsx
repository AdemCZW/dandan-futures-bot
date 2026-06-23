import { useEffect, useState } from 'react'
import { api, cls } from '../api'

const MODES = ['paper', 'backtest', 'live_testnet', 'live_testnet_ws', 'live_futures_testnet']

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

  return (
    <div className="panel">
      <div className="controls" style={{ marginBottom: 12 }}>
        <div className="field"><label>來源 (mode)</label>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="">全部</option>
            {MODES.map((m) => <option key={m}>{m}</option>)}
          </select>
        </div>
        <button className="run" onClick={load}>重新整理</button>
      </div>
      {err && <div className="err">⚠ {err}</div>}
      <div className="muted" style={{ marginBottom: 8 }}>
        共 {rows.length} 筆（來自 trades.db；測試網每月重置，靠這裡留底）
      </div>
      <table>
        <thead><tr><th>時間</th><th>來源</th><th>策略</th><th>動作</th><th>價格</th><th>數量</th><th>損益</th></tr></thead>
        <tbody>
          {rows.map((t, i) => (
            <tr key={i}>
              <td>{String(t.ts).slice(0, 16)}</td><td>{t.mode}</td><td>{t.strategy}</td><td>{t.side}</td>
              <td>{Number(t.price).toFixed(2)}</td><td>{t.qty}</td>
              <td className={cls(t.pnl)}>{t.pnl >= 0 ? '+' : ''}{Number(t.pnl).toFixed(2)}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={7} className="muted">（尚無交易留底；先在回測用 --journal，或跑 live）</td></tr>}
        </tbody>
      </table>
    </div>
  )
}
