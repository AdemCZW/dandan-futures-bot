import { useEffect, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Area, ComposedChart } from 'recharts'
import { api, pct, cls } from '../api'
import Hint, { Plain } from './Hint'

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
      <div className={`panel ${loading ? 'is-active' : ''}`}>
        <h3>回測參數</h3>
        <Plain>
          <b>回測</b>＝拿「過去的歷史 K 線」把某個策略從頭跑一遍，看它<b>假如當時這樣做、現在會賺多少</b>。
          用來比較哪個策略相對好；但歷史不代表未來，數字僅供參考、非保證。
        </Plain>
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
        {loading && <div className="spinner" style={{ marginTop: 12 }}>計算中…</div>}
      </div>

      {res && (
        <>
          <div className="cards">
            <div className="card">
              <div className="k"><Hint text="整段期間結束時的總賺賠百分比（含做多做空）。正=賺、負=賠。">總報酬</Hint></div>
              <div className={`v num signal-glow ${cls(m.total_return)}`}>{pct(m.total_return)}</div>
            </div>
            <div className="card">
              <div className="k"><Hint text="最大回撤：資金從某個高點一路跌到最低點的最大跌幅%。代表「中途最慘會虧多少」，越小越好、越能抱得住。">最大回撤</Hint></div>
              <div className="v num neg">{pct(m.max_drawdown)}</div>
            </div>
            <div className="card">
              <div className="k"><Hint text="所有平倉交易裡賺錢的比例。70% = 每 10 筆約 7 筆賺。注意：高勝率不等於賺錢（可能贏小賠大）。">勝率</Hint></div>
              <div className="v num">{pct(m.win_rate)}</div>
            </div>
            <div className="card">
              <div className="k"><Hint text="夏普值：每承受一單位波動換到多少報酬，衡量「賺得穩不穩」。>1 不錯、>2 很好、<0 賠錢。越高越好。">Sharpe</Hint></div>
              <div className={`v num ${cls(m.sharpe)}`}>{m.sharpe.toFixed(2)}</div>
            </div>
            <div className="card">
              <div className="k"><Hint text="整段期間總共開平倉幾次。太少（十幾筆）樣本不足、結論別太當真。">交易筆數</Hint></div>
              <div className="v num">{m.trades}</div>
            </div>
          </div>

          <div className="panel">
            <h3>權益曲線</h3>
            <div className="muted">
              <span className="num">{res.bars}</span> 根 · <span className="num">{res.start}</span> ~ <span className="num">{res.end}</span> · 來源 {res.source}
            </div>
            <ResponsiveContainer width="100%" height={320}>
              <ComposedChart data={res.equity} margin={{ top: 12, right: 20, bottom: 0, left: 0 }}>
                <defs>
                  <linearGradient id="bt-equity-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--accent)" stopOpacity={0.18} />
                    <stop offset="100%" stopColor="var(--accent)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke="var(--chart-grid)" strokeDasharray="3 3" />
                <XAxis dataKey="t" tick={{ fill: 'var(--muted)', fontSize: 11 }} minTickGap={60} tickFormatter={(t) => t.slice(5, 16)} />
                <YAxis tick={{ fill: 'var(--muted)', fontSize: 11 }} domain={['auto', 'auto']} width={66} />
                <Tooltip contentStyle={{ background: 'var(--tooltip-bg)', border: '1px solid var(--tooltip-border)', borderRadius: 'var(--radius-sm)', color: 'var(--text)', fontSize: 12 }} />
                <Area type="monotone" dataKey="equity" stroke="none" fill="url(#bt-equity-fill)" />
                <Line type="monotone" dataKey="equity" stroke="var(--chart-line)" dot={false} strokeWidth={1.6} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          <div className="panel">
            <h3>近期交易（最多 25 筆）</h3>
            <table>
              <thead><tr><th>時間</th><th>動作</th><th>方向</th><th>價格</th><th>數量</th><th>損益</th></tr></thead>
              <tbody>
                {res.trades.slice(-25).map((t, i) => (
                  <tr key={i}>
                    <td>{t.ts.slice(0, 16)}</td>
                    <td>{t.side}</td>
                    <td>
                      <span className={`badge ${t.dir === -1 ? 'badge-short' : 'badge-long'}`}>{t.dir === -1 ? '做空' : '做多'}</span>
                    </td>
                    <td>{t.price.toFixed(2)}</td>
                    <td>{t.qty}</td>
                    <td className={cls(t.pnl)}>{t.pnl >= 0 ? '+' : ''}{t.pnl}</td>
                  </tr>
                ))}
                {res.trades.length === 0 && <tr><td colSpan={6} className="muted">// 無已平倉交易</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
    </>
  )
}
