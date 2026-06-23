import { useEffect, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { api, pct, cls } from '../api'

export default function Backtest() {
  const [strats, setStrats] = useState([])
  const [strategy, setStrategy] = useState('ema_cross')
  const [symbol, setSymbol] = useState('BTCUSDT')
  const [interval, setIntervalV] = useState('5m')
  const [source, setSource] = useState('synthetic')
  const [loading, setLoading] = useState(false)
  const [res, setRes] = useState(null)
  const [err, setErr] = useState('')

  useEffect(() => { api.strategies().then(setStrats).catch(() => {}) }, [])

  async function run() {
    setLoading(true); setErr('')
    try { setRes(await api.backtest({ strategy, symbol, interval, source })) }
    catch (e) { setErr(String(e.message || e)); setRes(null) }
    finally { setLoading(false) }
  }

  const m = res?.metrics
  return (
    <>
      <div className="panel">
        <div className="controls">
          <div className="field"><label>策略</label>
            <select value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              {strats.map((s) => <option key={s.name} value={s.name}>{s.name}{s.allow_short ? ' (多空)' : ''}</option>)}
            </select>
          </div>
          <div className="field"><label>交易對</label>
            <input value={symbol} onChange={(e) => setSymbol(e.target.value)} /></div>
          <div className="field"><label>週期</label>
            <select value={interval} onChange={(e) => setIntervalV(e.target.value)}>
              {['5m', '15m', '1h', '4h'].map((i) => <option key={i}>{i}</option>)}
            </select>
          </div>
          <div className="field"><label>資料來源</label>
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              <option value="synthetic">synthetic（離線）</option>
              <option value="testnet">testnet（公開行情）</option>
            </select>
          </div>
          <button className="run" onClick={run} disabled={loading}>{loading ? '執行中…' : '跑回測'}</button>
        </div>
        {err && <div className="err">⚠ {err}</div>}
        {loading && <div className="spinner" style={{ marginTop: 10 }}>計算中…</div>}
      </div>

      {res && (
        <>
          <div className="cards">
            <div className="card"><div className={`v ${cls(m.total_return)}`}>{pct(m.total_return)}</div><div className="k">總報酬</div></div>
            <div className="card"><div className="v neg">{pct(m.max_drawdown)}</div><div className="k">最大回撤</div></div>
            <div className="card"><div className="v">{pct(m.win_rate)}</div><div className="k">勝率</div></div>
            <div className="card"><div className={`v ${cls(m.sharpe)}`}>{m.sharpe.toFixed(2)}</div><div className="k">Sharpe</div></div>
            <div className="card"><div className="v">{m.trades}</div><div className="k">交易筆數</div></div>
          </div>

          <div className="panel">
            <h3>權益曲線</h3>
            <div className="muted">{res.bars} 根 · {res.start} ~ {res.end} · 來源 {res.source}</div>
            <ResponsiveContainer width="100%" height={320}>
              <LineChart data={res.equity} margin={{ top: 12, right: 20, bottom: 0, left: 0 }}>
                <CartesianGrid stroke="#334155" strokeDasharray="3 3" />
                <XAxis dataKey="t" tick={{ fill: '#94a3b8', fontSize: 11 }} minTickGap={60} tickFormatter={(t) => t.slice(5, 16)} />
                <YAxis tick={{ fill: '#94a3b8', fontSize: 11 }} domain={['auto', 'auto']} width={66} />
                <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, color: '#e2e8f0' }} />
                <Line type="monotone" dataKey="equity" stroke="#38bdf8" dot={false} strokeWidth={1.6} />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="panel">
            <h3>近期交易（最多 25 筆）</h3>
            <table>
              <thead><tr><th>時間</th><th>動作</th><th>方向</th><th>價格</th><th>數量</th><th>損益</th></tr></thead>
              <tbody>
                {res.trades.slice(-25).map((t, i) => (
                  <tr key={i}>
                    <td>{t.ts.slice(0, 16)}</td><td>{t.side}</td><td>{t.dir === -1 ? '空' : '多'}</td>
                    <td>{t.price.toFixed(2)}</td><td>{t.qty}</td>
                    <td className={cls(t.pnl)}>{t.pnl >= 0 ? '+' : ''}{t.pnl}</td>
                  </tr>
                ))}
                {res.trades.length === 0 && <tr><td colSpan={6} className="muted">（無已平倉交易）</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
