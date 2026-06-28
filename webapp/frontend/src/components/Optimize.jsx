import { useState } from 'react'
import { api, pct } from '../api'
import Hint, { Plain } from './Hint'

// 單色藍階（--heat-low → --heat-mid → --heat-high，dark theme triples）：
// 低分近底色、高分亮藍，單色階讓「只有一兩格亮＝過擬合」一眼可辨
const HEAT_STOPS = [
  [22, 22, 26],    // --heat-low  near-bg
  [47, 74, 134],   // --heat-mid
  [91, 140, 255],  // --heat-high accent blue
]
function lerp(a, b, t) { return Math.round(a + (b - a) * t) }
function color(v, min, max) {
  if (v == null) return 'var(--surface-2)'
  const t = max > min ? (v - min) / (max - min) : 0.5
  const seg = t < 0.5 ? 0 : 1
  const lt = t < 0.5 ? t * 2 : (t - 0.5) * 2
  const [a, b] = [HEAT_STOPS[seg], HEAT_STOPS[seg + 1]]
  return `rgb(${lerp(a[0], b[0], lt)},${lerp(a[1], b[1], lt)},${lerp(a[2], b[2], lt)})`
}
// 低分端深底用一般字、高分端亮藍底用白字，確保對比
function heatInk(v, min, max) {
  if (v == null) return 'var(--faint)'
  const t = max > min ? (v - min) / (max - min) : 0.5
  return t > 0.55 ? '#ffffff' : 'var(--text)'
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
      <div className={`panel${loading ? ' is-active' : ''}`}>
        <h3>參數最佳化</h3>
        <Plain>
          <b>參數最佳化</b>＝同一個策略，把參數（如均線天數）一格一格試過去，找出歷史上表現最好的組合。
          重點是<b>別被「剛好過去最好」的數字騙了</b>（過擬合）——所以下面還有 Walk-forward 用「沒看過的資料」驗證它是不是真的有效。
        </Plain>
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
          <div className="field"><label><Hint text="要最佳化哪個目標：sharpe＝賺得最穩、return＝賺最多、return_dd＝報酬除以最大回撤（兼顧賺與抗跌）。">目標</Hint></label>
            <select value={objective} onChange={(e) => setObjective(e.target.value)}>
              {['sharpe', 'return', 'return_dd'].map((o) => <option key={o}>{o}</option>)}
            </select>
          </div>
          <button className="run" onClick={run} disabled={loading}>{loading ? '執行中…' : '跑最佳化'}</button>
        </div>
        {err && <div className="err">⚠ {err}</div>}
        {loading && <div className="spinner" style={{ marginTop: 12 }}>掃描參數中（可能需數秒）…</div>}
      </div>

      {res && (
        <>
          <div className="panel">
            <h3>參數掃描熱圖（{hm.metric}）</h3>
            <div className="muted"><span className="num">{res.combos}</span> 組合 · 只有一兩格亮綠＝過擬合的味道</div>
            <table className="heat" style={{ marginTop: 12 }}>
              <thead><tr><th></th>{hm.xticks.map((x) => <th key={x}>{hm.xlabel}=<span className="num">{x}</span></th>)}</tr></thead>
              <tbody>
                {hm.grid.map((row, i) => (
                  <tr key={i}>
                    <th>{hm.ylabel}=<span className="num">{hm.yticks[i]}</span></th>
                    {row.map((v, j) => (
                      <td key={j} className="num" style={{ background: color(v, min, max), color: heatInk(v, min, max) }}>
                        {v == null ? '–' : v}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="muted" style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12 }}>
              <span>低</span>
              <span style={{ flex: '0 0 120px', height: 6, borderRadius: 'var(--radius)', background: 'linear-gradient(90deg, var(--heat-low), var(--heat-mid), var(--heat-high))' }} />
              <span>高</span>
              <span className="num" style={{ marginLeft: 'auto' }}>{Number.isFinite(min) ? min : '–'} → {Number.isFinite(max) ? max : '–'}</span>
            </div>
          </div>

          <div className="panel">
            <h3><Hint text="把時間切成好幾段，每段「用前半段挑最佳參數、拿後半段（沒看過的資料）實測」。如果後半段也賺，才算真有效、不是運氣。">Walk-forward（樣本外泛化）</Hint></h3>
            <Plain>
              <b>IS（樣本內）</b>＝拿來挑參數那段；<b>OOS（樣本外）</b>＝沒看過、用來驗證那段。
              看 <b>OOS 平均報酬</b>是否還是正的、<b>OOS 為正比例</b>越高越好；<b>IS→OOS 衰減</b>越大代表越可能是過擬合（換到新資料就失靈）。
            </Plain>
            {wf.folds === 0 ? <div className="muted" style={{ color: 'var(--faint)' }}>// 資料不足，切不出 fold</div> : (
              <>
                <div className="cards">
                  <div className="card"><div className="v num">{wf.folds}</div><div className="k">fold 數</div></div>
                  <div className={`card${wf.IS_mean > 0 ? ' is-long' : ' is-short'}`}><div className={wf.IS_mean > 0 ? 'v num pos' : 'v num neg'}>{pct(wf.IS_mean)}</div><div className="k">IS 平均報酬</div></div>
                  <div className={`card${wf.OOS_mean > 0 ? ' is-long' : ' is-short'}`}><div className={wf.OOS_mean > 0 ? 'v num pos' : 'v num neg'}>{pct(wf.OOS_mean)}</div><div className="k">OOS 平均報酬</div></div>
                  <div className="card"><div className="v num">{pct(wf.OOS_positive_ratio)}</div><div className="k">OOS 為正比例</div></div>
                  <div className="card"><div className="v num">{pct(wf.decay)}</div><div className="k">IS→OOS 衰減</div></div>
                </div>
                <table style={{ marginTop: 12 }}>
                  <thead><tr><th>fold</th><th>測試起</th><th>測試迄</th><th>IS報酬</th><th>OOS報酬</th><th>OOS Sharpe</th><th>OOS筆數</th></tr></thead>
                  <tbody>
                    {res.walkforward.folds.map((f) => (
                      <tr key={f.fold}>
                        <td className="num">{f.fold}</td><td>{f.test_start.slice(0, 10)}</td><td>{f.test_end.slice(0, 10)}</td>
                        <td className={f.IS_return > 0 ? 'num pos' : f.IS_return < 0 ? 'num neg' : 'num'}>{pct(f.IS_return)}</td>
                        <td className={f.OOS_return > 0 ? 'num pos' : f.OOS_return < 0 ? 'num neg' : 'num'}>{pct(f.OOS_return)}</td>
                        <td className="num">{f.OOS_sharpe}</td><td className="num">{f.OOS_trades}</td>
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
