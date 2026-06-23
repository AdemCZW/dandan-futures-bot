import { useEffect, useRef, useState } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, CartesianGrid, ReferenceLine,
} from 'recharts'
import { api } from '../api'

const PERIODS = ['1m', '5m', '15m', '30m', '1h', '4h']

function toTaipei(ts) {
  const d = new Date(ts + 8 * 3600 * 1000)
  const h = String(d.getUTCHours()).padStart(2, '0')
  const m = String(d.getUTCMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

function fmtOI(v) {
  if (v == null) return '—'
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(0)}M`
  return String(v)
}

function Metric({ label, value, sub, color }) {
  return (
    <div style={{
      background: 'var(--panel2)', border: '1px solid var(--border)',
      borderRadius: 8, padding: '12px 16px', flex: 1, minWidth: 140,
    }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700, color: color || 'var(--fg)', fontVariantNumeric: 'tabular-nums' }}>
        {value ?? '—'}
      </div>
      {sub && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

function Signal({ label, value, threshold, reverse }) {
  if (value == null) return null
  const bullish = reverse ? value < threshold : value > threshold
  const color = bullish ? 'var(--green)' : 'var(--red, #e05)'
  return (
    <span style={{ fontSize: 12, padding: '2px 8px', borderRadius: 10,
      background: color + '22', color, border: `1px solid ${color}44`, fontWeight: 600 }}>
      {label}: {bullish ? '偏多' : '偏空'}
    </span>
  )
}

const CHART_STYLE = { fontSize: 11, fill: 'var(--muted)' }

function fmtVal(v) {
  if (v == null) return '—'
  if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M`
  if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(0)}K`
  return `$${v}`
}

function DirBadge({ dir }) {
  const cfg = {
    long:  { label: '做多', bg: '#0a2', color: '#fff' },
    short: { label: '做空', bg: '#c02', color: '#fff' },
    flat:  { label: '空手', bg: 'var(--panel2)', color: 'var(--muted)' },
  }[dir] ?? { label: dir, bg: 'var(--panel2)', color: 'var(--muted)' }
  return (
    <span style={{ fontSize: 11, padding: '1px 7px', borderRadius: 8,
      background: cfg.bg, color: cfg.color, fontWeight: 600 }}>
      {cfg.label}
    </span>
  )
}

function HLLeaderboard() {
  const [hl, setHl] = useState(null)
  const [hlErr, setHlErr] = useState('')
  const [loading, setLoading] = useState(false)

  async function load() {
    setLoading(true)
    try {
      setHl(await api.hlLeaderboard(30))
      setHlErr('')
    } catch (e) {
      setHlErr(String(e.message || e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    const t = setInterval(load, 120000)  // 每 2 分鐘（配合快取）
    return () => clearInterval(t)
  }, [])

  const sum = hl?.btc_summary
  return (
    <div className="panel">
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
        <div style={{ fontWeight: 600 }}>Hyperliquid 頂尖交易者 — BTC 持倉</div>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>鏈上公開數據 · 每 2 分鐘刷新</span>
        {loading && <span style={{ fontSize: 11, color: 'var(--accent)' }}>載入中…</span>}
        <button onClick={load} style={{ marginLeft: 'auto', fontSize: 11, padding: '2px 8px',
          borderRadius: 6, border: '1px solid var(--border)', background: 'var(--panel2)',
          color: 'var(--fg)', cursor: 'pointer' }}>手動刷新</button>
      </div>

      {hlErr && <div className="err">⚠ {hlErr}</div>}

      {sum && (
        <div style={{ display: 'flex', gap: 16, marginBottom: 12, fontSize: 13 }}>
          <span style={{ color: '#0a2', fontWeight: 700 }}>做多 {sum.long} 人</span>
          <span style={{ color: '#c02', fontWeight: 700 }}>做空 {sum.short} 人</span>
          <span style={{ color: 'var(--muted)' }}>空手 {sum.flat} 人</span>
          <span style={{ color: 'var(--muted)', marginLeft: 8 }}>
            多空比 {sum.long + sum.short > 0
              ? ((sum.long / (sum.long + sum.short)) * 100).toFixed(0)
              : '—'}% 做多
          </span>
        </div>
      )}

      {hl?.traders?.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
            <thead>
              <tr style={{ color: 'var(--muted)', borderBottom: '1px solid var(--border)' }}>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>#</th>
                <th style={{ textAlign: 'left', padding: '4px 8px' }}>交易者</th>
                <th style={{ textAlign: 'right', padding: '4px 8px' }}>帳戶規模</th>
                <th style={{ textAlign: 'right', padding: '4px 8px' }}>今日損益</th>
                <th style={{ textAlign: 'center', padding: '4px 8px' }}>BTC 方向</th>
                <th style={{ textAlign: 'right', padding: '4px 8px' }}>BTC 數量</th>
                <th style={{ textAlign: 'right', padding: '4px 8px' }}>未實現損益</th>
              </tr>
            </thead>
            <tbody>
              {hl.traders.map((t, i) => (
                <tr key={t.address}
                  style={{ borderBottom: '1px solid var(--border)',
                    background: i % 2 === 0 ? 'transparent' : 'var(--panel2)' }}>
                  <td style={{ padding: '5px 8px', color: 'var(--muted)' }}>{i + 1}</td>
                  <td style={{ padding: '5px 8px', fontWeight: 500 }}>
                    <a href={`https://app.hyperliquid.xyz/explorer/address/${t.address}`}
                      target="_blank" rel="noreferrer"
                      style={{ color: 'var(--accent)', textDecoration: 'none' }}>
                      {t.name}
                    </a>
                  </td>
                  <td style={{ padding: '5px 8px', textAlign: 'right' }}>{fmtVal(t.account_value)}</td>
                  <td style={{ padding: '5px 8px', textAlign: 'right',
                    color: t.day_pnl > 0 ? 'var(--green)' : t.day_pnl < 0 ? '#e05' : 'var(--muted)' }}>
                    {t.day_pnl >= 0 ? '+' : ''}{fmtVal(t.day_pnl)}
                  </td>
                  <td style={{ padding: '5px 8px', textAlign: 'center' }}>
                    <DirBadge dir={t.btc_direction} />
                  </td>
                  <td style={{ padding: '5px 8px', textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                    {t.btc_size > 0 ? `${t.btc_size} BTC` : '—'}
                  </td>
                  <td style={{ padding: '5px 8px', textAlign: 'right',
                    color: t.btc_upnl > 0 ? 'var(--green)' : t.btc_upnl < 0 ? '#e05' : 'var(--muted)' }}>
                    {t.btc_size > 0 ? (t.btc_upnl >= 0 ? '+' : '') + fmtVal(t.btc_upnl) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!hl && !loading && !hlErr && (
        <div className="muted" style={{ textAlign: 'center', padding: 30 }}>載入中…</div>
      )}
    </div>
  )
}

export default function Whales() {
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')
  const [period, setPeriod] = useState('5m')
  const [chart, setChart] = useState('top_acct')
  const [tick, setTick] = useState(0)
  const timer = useRef(null)

  async function load(p) {
    try {
      setErr('')
      setD(await api.whales('BTCUSDT', p || period, 30))
    } catch (e) {
      setErr(String(e.message || e))
    }
  }

  useEffect(() => {
    load()
    timer.current = setInterval(() => { load(); setTick(t => t + 1) }, 30000)
    return () => clearInterval(timer.current)
  }, [period])

  function changePeriod(p) { setPeriod(p); load(p) }

  const s = d?.snapshot
  const seriesMap = {
    top_acct: { data: d?.top_acct_series, title: '大戶帳戶多空比（%）', ls: true },
    top_pos:  { data: d?.top_pos_series,  title: '大戶持倉多空比（%）', ls: true },
    global:   { data: d?.global_series,   title: '全市場多空比（%）',   ls: true },
    taker:    { data: d?.taker_series,    title: '主動買入 / 賣出比',   ls: false },
    oi:       { data: d?.oi_series,       title: '未平倉合約（USDT）',  ls: false },
  }
  const { data: chartData, title: chartTitle, ls: isLS } = seriesMap[chart]

  return (
    <>
      {/* ── header ─────────────────────────────────────────── */}
      <div className="panel">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <h3 style={{ margin: 0 }}>大戶籌碼追蹤</h3>
          <span style={{ fontSize: 11, color: 'var(--muted)', marginLeft: 'auto' }}>
            幣安合約主網 · 公開數據 · 每 30 秒刷新（#{tick}）
          </span>
        </div>
        <div className="muted" style={{ marginTop: 4 }}>
          資料來源：Binance Futures 公開 API — 大戶帳戶 / 持倉多空比、全市場情緒、主動買賣流量、未平倉合約。
        </div>
        {/* period selector */}
        <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
          {PERIODS.map(p => (
            <button key={p}
              style={{
                padding: '3px 10px', borderRadius: 6, border: '1px solid var(--border)',
                background: period === p ? 'var(--accent)' : 'var(--panel2)',
                color: period === p ? '#000' : 'var(--fg)', cursor: 'pointer', fontSize: 12,
              }}
              onClick={() => changePeriod(p)}>{p}</button>
          ))}
        </div>
        {err && <div className="err">⚠ {err}</div>}
      </div>

      {/* ── snapshot cards ───────────────────────────────────── */}
      {s && (
        <div className="cards" style={{ flexWrap: 'wrap' }}>
          <Metric
            label="大戶做多帳戶"
            value={s.top_long_pct != null ? `${s.top_long_pct}%` : '—'}
            sub={`做空 ${s.top_short_pct ?? '—'}%`}
            color={s.top_long_pct > 50 ? 'var(--green)' : 'var(--red,#e05)'}
          />
          <Metric
            label="全市場做多帳戶"
            value={s.global_long_pct != null ? `${s.global_long_pct}%` : '—'}
            sub={`做空 ${s.global_short_pct ?? '—'}%`}
            color={s.global_long_pct > 50 ? 'var(--green)' : 'var(--red,#e05)'}
          />
          <Metric
            label="主動買入/賣出比"
            value={s.taker_ratio ?? '—'}
            sub={`買 ${s.taker_buy_vol ?? '—'} / 賣 ${s.taker_sell_vol ?? '—'} BTC`}
            color={s.taker_ratio > 1 ? 'var(--green)' : 'var(--red,#e05)'}
          />
          <Metric
            label="未平倉合約"
            value={fmtOI(s.oi_usdt)}
            sub={`${s.oi_btc ?? '—'} BTC`}
          />
        </div>
      )}

      {/* ── signal bar ───────────────────────────────────────── */}
      {s && (
        <div className="panel" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>市場信號：</span>
          <Signal label="大戶帳戶" value={s.top_long_pct} threshold={50} />
          <Signal label="大戶持倉" value={d?.top_pos_series?.at(-1)?.long} threshold={50} />
          <Signal label="全市場" value={s.global_long_pct} threshold={50} />
          <Signal label="主動買賣" value={s.taker_ratio} threshold={1} />
        </div>
      )}

      {/* ── chart ────────────────────────────────────────────── */}
      <div className="panel">
        {/* chart tabs */}
        <div style={{ display: 'flex', gap: 6, marginBottom: 12, flexWrap: 'wrap' }}>
          {[
            ['top_acct', '大戶帳戶多空'],
            ['top_pos',  '大戶持倉多空'],
            ['global',   '全市場多空'],
            ['taker',    '主動買賣比'],
            ['oi',       '未平倉合約'],
          ].map(([k, lbl]) => (
            <button key={k}
              style={{
                padding: '3px 10px', borderRadius: 6, border: '1px solid var(--border)',
                background: chart === k ? 'var(--accent)' : 'var(--panel2)',
                color: chart === k ? '#000' : 'var(--fg)', cursor: 'pointer', fontSize: 12,
              }}
              onClick={() => setChart(k)}>{lbl}</button>
          ))}
        </div>

        <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8 }}>{chartTitle}</div>

        {chartData && chartData.length > 0 ? (
          <ResponsiveContainer width="100%" height={260}>
            {isLS ? (
              <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={toTaipei} tick={CHART_STYLE} interval="preserveStartEnd" />
                <YAxis domain={[0, 100]} tick={CHART_STYLE} unit="%" width={38} />
                <Tooltip
                  labelFormatter={v => `台灣時間 ${toTaipei(v)}`}
                  formatter={(v, n) => [`${v}%`, n === 'long' ? '做多' : '做空']}
                  contentStyle={{ background: 'var(--panel2)', border: '1px solid var(--border)', fontSize: 12 }}
                />
                <ReferenceLine y={50} stroke="var(--muted)" strokeDasharray="4 4" />
                <Legend formatter={v => v === 'long' ? '做多 %' : '做空 %'} />
                <Line type="monotone" dataKey="long"  stroke="var(--green)" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="short" stroke="#e05"         dot={false} strokeWidth={2} />
              </LineChart>
            ) : chart === 'oi' ? (
              <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={toTaipei} tick={CHART_STYLE} interval="preserveStartEnd" />
                <YAxis tickFormatter={fmtOI} tick={CHART_STYLE} width={52} />
                <Tooltip
                  labelFormatter={v => `台灣時間 ${toTaipei(v)}`}
                  formatter={v => [fmtOI(v), '未平倉合約']}
                  contentStyle={{ background: 'var(--panel2)', border: '1px solid var(--border)', fontSize: 12 }}
                />
                <Line type="monotone" dataKey="usdt" stroke="var(--accent)" dot={false} strokeWidth={2} />
              </LineChart>
            ) : (
              <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis dataKey="ts" tickFormatter={toTaipei} tick={CHART_STYLE} interval="preserveStartEnd" />
                <YAxis tick={CHART_STYLE} width={38} />
                <Tooltip
                  labelFormatter={v => `台灣時間 ${toTaipei(v)}`}
                  formatter={v => [v, '主動買/賣比']}
                  contentStyle={{ background: 'var(--panel2)', border: '1px solid var(--border)', fontSize: 12 }}
                />
                <ReferenceLine y={1} stroke="var(--muted)" strokeDasharray="4 4" />
                <Line type="monotone" dataKey="ratio" stroke="var(--accent)" dot={false} strokeWidth={2} />
              </LineChart>
            )}
          </ResponsiveContainer>
        ) : (
          <div className="muted" style={{ textAlign: 'center', padding: 40 }}>
            {d ? '暫無數據' : '載入中…'}
          </div>
        )}
      </div>

      {/* ── Hyperliquid leaderboard ──────────────────────────── */}
      <HLLeaderboard />

      {/* ── explanation ──────────────────────────────────────── */}
      <div className="panel" style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.8 }}>
        <div style={{ fontWeight: 600, color: 'var(--fg)', marginBottom: 6 }}>指標說明</div>
        <div><b>大戶帳戶多空比</b> — 持倉規模前 20% 的帳戶中，做多帳戶佔比。&gt;50% 代表大戶偏多。</div>
        <div><b>大戶持倉多空比</b> — 大戶的多單倉位佔總持倉比例（比帳戶比更反映集中度）。</div>
        <div><b>全市場多空比</b> — 所有帳戶做多比例，散戶情緒參考（常與大戶反向）。</div>
        <div><b>主動買入/賣出比</b> — &gt;1 代表主動買盤大於賣盤（市場偏積極做多）。</div>
        <div><b>未平倉合約</b> — 合約市場資金規模，上升代表新資金流入（趨勢確認），下降代表倉位平掉。</div>
      </div>
    </>
  )
}
