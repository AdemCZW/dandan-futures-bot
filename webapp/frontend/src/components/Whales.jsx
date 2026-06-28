import { useEffect, useRef, useState } from 'react'
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, CartesianGrid, ReferenceLine,
} from 'recharts'
import { api } from '../api'
import Hint, { Plain } from './Hint'

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

// 指標卡：標籤在上(.k)、等寬數值在下(.v)。tone = 'pos' | 'neg' | undefined（中性不發光）
function Metric({ label, value, sub, tone, glow, side }) {
  const cls = ['card']
  if (side === 'long') cls.push('is-long')
  if (side === 'short') cls.push('is-short')
  const vCls = ['v', 'num']
  if (tone) vCls.push(tone)
  if (glow) vCls.push('signal-glow')
  return (
    <div className={cls.join(' ')} style={{ flex: 1 }}>
      <div className="k">{label}</div>
      <div className={vCls.join(' ')}>{value ?? '—'}</div>
      {sub && <div className="k" style={{ marginTop: 2 }}>{sub}</div>}
    </div>
  )
}

// 市場信號徽章：色彩 + 中文「偏多/偏空」雙重編碼（照顧色弱）
function Signal({ label, value, threshold, reverse }) {
  if (value == null) return null
  const bullish = reverse ? value < threshold : value > threshold
  return (
    <span className={`badge ${bullish ? 'badge-long' : 'badge-short'}`}>
      {label} · {bullish ? '偏多' : '偏空'}
    </span>
  )
}

const AXIS_TICK = { fontSize: 11, fill: 'var(--muted)' }
const TOOLTIP_STYLE = {
  background: 'var(--tooltip-bg)',
  border: '1px solid var(--tooltip-border)',
  borderRadius: 'var(--radius-sm)',
  fontSize: 12,
}

function fmtVal(v) {
  if (v == null) return '—'
  if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(2)}B`
  if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(1)}M`
  if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(0)}K`
  return `$${v}`
}

// 多空方向徽章：色彩 + 中文字雙重編碼
function DirBadge({ dir }) {
  const cfg = {
    long:  { label: '做多', cls: 'badge-long' },
    short: { label: '做空', cls: 'badge-short' },
    flat:  { label: '空手', cls: 'badge-flat' },
  }[dir] ?? { label: dir, cls: 'badge-flat' }
  return <span className={`badge ${cfg.cls}`}>{cfg.label}</span>
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
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>HYPERLIQUID 頂尖交易者 · BTC 持倉</h3>
        <span className="muted" style={{ fontSize: 11 }}>鏈上公開數據 · 每 2 分鐘刷新</span>
        {loading && <span className="badge badge-system">載入中</span>}
        <button className="btn-ghost" onClick={load} style={{ marginLeft: 'auto', fontSize: 11 }}>
          手動刷新
        </button>
      </div>

      {hlErr && <div className="err">⚠ {hlErr}</div>}

      {sum && (
        <div className="cards" style={{ margin: '0 0 12px' }}>
          <Metric label="做多人數" value={`${sum.long} 人`} tone="pos" side="long" />
          <Metric label="做空人數" value={`${sum.short} 人`} tone="neg" side="short" />
          <Metric label="空手人數" value={`${sum.flat} 人`} />
          <Metric
            label="多空比（做多）"
            value={sum.long + sum.short > 0
              ? `${((sum.long / (sum.long + sum.short)) * 100).toFixed(0)}%`
              : '—'}
          />
        </div>
      )}

      {hl?.traders?.length > 0 && (
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>交易者</th>
                <th>帳戶規模</th>
                <th>今日損益</th>
                <th style={{ textAlign: 'center' }}>BTC 方向</th>
                <th>BTC 數量</th>
                <th>未實現損益</th>
              </tr>
            </thead>
            <tbody>
              {hl.traders.map((t, i) => (
                <tr key={t.address}>
                  <td style={{ color: 'var(--muted)' }}>{i + 1}</td>
                  <td>
                    <a href={`https://app.hyperliquid.xyz/explorer/address/${t.address}`}
                      target="_blank" rel="noreferrer"
                      style={{ color: 'var(--accent)', textDecoration: 'none' }}>
                      {t.name}
                    </a>
                  </td>
                  <td>{fmtVal(t.account_value)}</td>
                  <td className={t.day_pnl > 0 ? 'pos' : t.day_pnl < 0 ? 'neg' : ''}
                    style={t.day_pnl === 0 ? { color: 'var(--muted)' } : undefined}>
                    {t.day_pnl >= 0 ? '+' : ''}{fmtVal(t.day_pnl)}
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    <DirBadge dir={t.btc_direction} />
                  </td>
                  <td>{t.btc_size > 0 ? `${t.btc_size} BTC` : '—'}</td>
                  <td className={t.btc_size > 0 ? (t.btc_upnl > 0 ? 'pos' : t.btc_upnl < 0 ? 'neg' : '') : ''}
                    style={t.btc_size <= 0 || t.btc_upnl === 0 ? { color: 'var(--muted)' } : undefined}>
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
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <h3 style={{ margin: 0 }}>大戶籌碼追蹤</h3>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginLeft: 'auto' }}>
            <span className="muted" style={{ fontSize: 11 }}>幣安合約主網 · 公開數據 · 每 30 秒刷新</span>
            <span className="badge badge-system">
              <span className="display">SYNC</span>&nbsp;#<span className="num">{tick}</span>
            </span>
          </div>
        </div>
        <div className="muted" style={{ marginTop: 8 }}>
          資料來源：Binance Futures 公開 API — 大戶帳戶 / 持倉多空比、全市場情緒、主動買賣流量、未平倉合約。
        </div>
        {/* period selector */}
        <div className="controls" style={{ marginTop: 12 }}>
          <span className="muted" style={{ fontSize: 11, alignSelf: 'center' }}>週期</span>
          {PERIODS.map(p => (
            <button key={p}
              className={`btn-ghost ${period === p ? 'is-active' : ''}`}
              aria-pressed={period === p}
              onClick={() => changePeriod(p)}>
              <span className="num">{p}</span>
            </button>
          ))}
        </div>
        {err && <div className="err">⚠ {err}</div>}
      </div>

      <Plain>
        這頁看的是<b>「市場上其他人怎麼下注」</b>（幣安公開籌碼），不是我們 bot 的倉。
        大戶/散戶偏多偏空、主動買賣力道、未平倉資金規模——當大戶與散戶分歧時常是觀察點。僅供參考、非買賣訊號。
      </Plain>

      {/* ── snapshot cards ───────────────────────────────────── */}
      {s && (
        <div className="cards">
          <Metric
            label={<Hint text="幣安「持倉前段大戶」帳戶裡，做多的比例。>50% 代表大戶整體偏多。對照下面散戶看分歧。">大戶做多帳戶</Hint>}
            value={s.top_long_pct != null ? `${s.top_long_pct}%` : '—'}
            sub={`做空 ${s.top_short_pct ?? '—'}%`}
            tone={s.top_long_pct > 50 ? 'pos' : 'neg'}
            glow
            side={s.top_long_pct > 50 ? 'long' : 'short'}
          />
          <Metric
            label={<Hint text="全市場所有帳戶（多為散戶）做多的比例。散戶極度偏多時，行情常反向（散戶常是反指標）。">全市場做多帳戶</Hint>}
            value={s.global_long_pct != null ? `${s.global_long_pct}%` : '—'}
            sub={`做空 ${s.global_short_pct ?? '—'}%`}
            tone={s.global_long_pct > 50 ? 'pos' : 'neg'}
            side={s.global_long_pct > 50 ? 'long' : 'short'}
          />
          <Metric
            label={<Hint text="主動成交裡「市價買 ÷ 市價賣」的量比。>1 代表主動買盤較兇（追多），<1 代表主動賣壓較重（殺多）。">主動買入/賣出比</Hint>}
            value={s.taker_ratio ?? '—'}
            sub={`買 ${s.taker_buy_vol ?? '—'} / 賣 ${s.taker_sell_vol ?? '—'} BTC`}
            tone={s.taker_ratio > 1 ? 'pos' : 'neg'}
            side={s.taker_ratio > 1 ? 'long' : 'short'}
          />
          <Metric
            label={<Hint text="未平倉合約（OI）：市場上還沒平掉的合約總額，等於「在場資金規模」。上升=新錢進場（趨勢確認），下降=資金撤離（倉位平掉）。">未平倉合約</Hint>}
            value={fmtOI(s.oi_usdt)}
            sub={`${s.oi_btc ?? '—'} BTC`}
          />
        </div>
      )}

      {/* ── signal bar ───────────────────────────────────────── */}
      {s && (
        <div className="panel" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <span className="muted" style={{ fontSize: 12 }}>市場信號</span>
          <Signal label="大戶帳戶" value={s.top_long_pct} threshold={50} />
          <Signal label="大戶持倉" value={d?.top_pos_series?.at(-1)?.long} threshold={50} />
          <Signal label="全市場" value={s.global_long_pct} threshold={50} />
          <Signal label="主動買賣" value={s.taker_ratio} threshold={1} />
        </div>
      )}

      {/* ── chart ────────────────────────────────────────────── */}
      <div className="panel">
        {/* chart tabs */}
        <div className="controls" style={{ marginBottom: 12 }}>
          {[
            ['top_acct', '大戶帳戶多空'],
            ['top_pos',  '大戶持倉多空'],
            ['global',   '全市場多空'],
            ['taker',    '主動買賣比'],
            ['oi',       '未平倉合約'],
          ].map(([k, lbl]) => (
            <button key={k}
              className={`btn-ghost ${chart === k ? 'is-active' : ''}`}
              aria-pressed={chart === k}
              onClick={() => setChart(k)}>{lbl}</button>
          ))}
        </div>

        <h3 style={{ margin: '0 0 8px' }}>{chartTitle}</h3>

        {chartData && chartData.length > 0 ? (
          <ResponsiveContainer width="100%" height={260}>
            {isLS ? (
              <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis dataKey="ts" tickFormatter={toTaipei} tick={AXIS_TICK} interval="preserveStartEnd" />
                <YAxis domain={[0, 100]} tick={AXIS_TICK} unit="%" width={38} />
                <Tooltip
                  labelFormatter={v => `台灣時間 ${toTaipei(v)}`}
                  formatter={(v, n) => [`${v}%`, n === 'long' ? '做多' : '做空']}
                  contentStyle={TOOLTIP_STYLE}
                />
                <ReferenceLine y={50} stroke="var(--muted)" strokeDasharray="4 4" />
                <Legend formatter={v => v === 'long' ? '做多 %' : '做空 %'} />
                <Line type="monotone" dataKey="long"  stroke="var(--pos)" dot={false} strokeWidth={2} />
                <Line type="monotone" dataKey="short" stroke="var(--neg)" dot={false} strokeWidth={2} />
              </LineChart>
            ) : chart === 'oi' ? (
              <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis dataKey="ts" tickFormatter={toTaipei} tick={AXIS_TICK} interval="preserveStartEnd" />
                <YAxis tickFormatter={fmtOI} tick={AXIS_TICK} width={52} />
                <Tooltip
                  labelFormatter={v => `台灣時間 ${toTaipei(v)}`}
                  formatter={v => [fmtOI(v), '未平倉合約']}
                  contentStyle={TOOLTIP_STYLE}
                />
                <Line type="monotone" dataKey="usdt" stroke="var(--chart-line)" dot={false} strokeWidth={2} />
              </LineChart>
            ) : (
              <LineChart data={chartData} margin={{ top: 4, right: 12, bottom: 4, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis dataKey="ts" tickFormatter={toTaipei} tick={AXIS_TICK} interval="preserveStartEnd" />
                <YAxis tick={AXIS_TICK} width={38} />
                <Tooltip
                  labelFormatter={v => `台灣時間 ${toTaipei(v)}`}
                  formatter={v => [v, '主動買/賣比']}
                  contentStyle={TOOLTIP_STYLE}
                />
                <ReferenceLine y={1} stroke="var(--muted)" strokeDasharray="4 4" />
                <Line type="monotone" dataKey="ratio" stroke="var(--chart-line)" dot={false} strokeWidth={2} />
              </LineChart>
            )}
          </ResponsiveContainer>
        ) : (
          <div className="muted" style={{ textAlign: 'center', padding: 40 }}>
            {d ? '// 暫無數據' : '載入中…'}
          </div>
        )}
      </div>

      {/* ── Hyperliquid leaderboard ──────────────────────────── */}
      <HLLeaderboard />

      {/* ── explanation ──────────────────────────────────────── */}
      <div className="panel" style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.8 }}>
        <h3 style={{ margin: '0 0 8px' }}>指標說明</h3>
        <div><b style={{ color: 'var(--text)' }}>大戶帳戶多空比</b> — 持倉規模前 20% 的帳戶中，做多帳戶佔比。&gt;50% 代表大戶偏多。</div>
        <div><b style={{ color: 'var(--text)' }}>大戶持倉多空比</b> — 大戶的多單倉位佔總持倉比例（比帳戶比更反映集中度）。</div>
        <div><b style={{ color: 'var(--text)' }}>全市場多空比</b> — 所有帳戶做多比例，散戶情緒參考（常與大戶反向）。</div>
        <div><b style={{ color: 'var(--text)' }}>主動買入/賣出比</b> — &gt;1 代表主動買盤大於賣盤（市場偏積極做多）。</div>
        <div><b style={{ color: 'var(--text)' }}>未平倉合約</b> — 合約市場資金規模，上升代表新資金流入（趨勢確認），下降代表倉位平掉。</div>
      </div>
    </>
  )
}
