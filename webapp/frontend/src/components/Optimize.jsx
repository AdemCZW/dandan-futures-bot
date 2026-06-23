import { useState } from 'react'
import { api, pct } from '../api'

// 紅→黃→綠：低分紅、高分綠，凸顯「只有一兩格亮」的過擬合特徵
function color(v, min, max) {
  if (v == null) return '#475569'
  const t = max > min ? (v - min) / (max - min) : 0.5
  const r = t < 0.5 ? 235 : Math.round(235 - (t - 0.5) * 2 * 175)
  const g = t > 0.5 ? 195 : Math.round(70 + t * 2 * 125)
  return `rgb(${r},${g},85)`
}

export default function Optimize() {
  const [strategy, setStrategy] = useState('ema_cross')
  const [source, setSource] = useState('synthetic')
  const [objective, setObjective] = useState('sharpe')
  const [loading, setLoading] = useState(false)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState('')

  async function run() {
    setLoading(true); setErr('')
    try { setRes(await api.optimize({ strategy, source, objective })) }
    catch (e) { setErr(String(e.message || e)); setRes(null) }
    finally { setLoading(false) }
  }

  const hm = res?.heatmap
  const flat = hm ? hm.grid.flat().filter((v) => v != null) : []
  const min = Math.min(...flat), max = Math.max(...flat)
  const wf = res?.walkforward?.summary

  return (
    <>
      <div className="panel">
        <div className="controls">
          <div className="field"><label>策略</label>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {['ema_cross', 'zscore_revert', 'zscore_ls'].map((s) => <option key={s}>{s}</option>)}
            </select>
          </div>
          <div className="field"><label>資料來源</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="synthetic">synthetic（離線）</option>
              <option value="testnet">testnet</option>
            </select>
          </div>
          <div className="field"><label>目標</label>
            <select value={objective} onChange={(e) => setObjective(e.target.value)}>
              {['sharpe', 'return', 'return_dd'].map((o) => <option key={o}>{o}</option>)}
            </select>
          </div>
          <button className="run" onClick={run} disabled={loading}>{loading ? '執行中…' : '跑最佳化'}</button>
        </div>
        {err && <div className="err">⚠ {err}</div>}
        {loading && <div className="spinner" style={{ marginTop: 10 }}>掃描參數中（可能需數秒）…</div>}
      </div>

      {res && (
        <>
          <div className="panel">
            <h3>參數掃描熱圖（{hm.metric}）</h3>
            <div className="muted">{res.combos} 組合 · 只有一兩格亮綠＝過擬合的味道</div>
            <table className="heat" style={{ marginTop: 10 }}>
              <thead><tr><th></th>{hm.xticks.map((x) => <th key={x}>{hm.xlabel}={x}</th>)}</tr></thead>
              <tbody>
                {hm.grid.map((row, i) => (
                  <tr key={i}>
                    <th>{hm.ylabel}={hm.yticks[i]}</th>
                    {row.map((v, j) => <td key={j} style={{ background: color(v, min, max) }}>{v == null ? '–' : v}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <h3>Walk-forward（樣本外泛化）</h3>
            {wf.folds === 0 ? <div className="muted">資料不足，切不出 fold。</div> : (
              <>
                <div className="cards">
                  <div className="card"><div className="v">{wf.folds}</div><div className="k">fold 數</div></div>
                  <div className="card"><div className={wf.IS_mean > 0 ? 'v pos' : 'v neg'}>{pct(wf.IS_mean)}</div><div className="k">IS 平均報酬</div></div>
                  <div className="card"><div className={wf.OOS_mean > 0 ? 'v pos' : 'v neg'}>{pct(wf.OOS_mean)}</div><div className="k">OOS 平均報酬</div></div>
                  <div className="card"><div className="v">{pct(wf.OOS_positive_ratio)}</div><div className="k">OOS 為正比例</div></div>
                  <div className="card"><div className="v">{pct(wf.decay)}</div><div className="k">IS→OOS 衰減</div></div>
                </div>
                <table>
                  <thead><tr><th>fold</th><th>測試起</th><th>測試迄</th><th>IS報酬</th><th>OOS報酬</th><th>OOS Sharpe</th><th>OOS筆數</th></tr></thead>
                  <tbody>
                    {res.walkforward.folds.map((f) => (
                      <tr key={f.fold}>
                        <td>{f.fold}</td><td>{f.test_start.slice(0, 10)}</td><td>{f.test_end.slice(0, 10)}</td>
                        <td>{pct(f.IS_return)}</td><td>{pct(f.OOS_return)}</td><td>{f.OOS_sharpe}</td><td>{f.OOS_trades}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        </>
      )}
    </>
  )
}
