import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import MiniChart from './MiniChart'
import { pairTrades, calcBalances, lossStreak, roiPct, holdDuration, exitReason } from '../lib/trades'
import Hint, { Plain } from './Hint'

const BOT_COLORS   = ['var(--bot1)', 'var(--bot2)', 'var(--bot3)', 'var(--bot4)']
const INIT_CAPITAL = 5000   // 每台機器人起始預算（測試網虛擬資金）

// ── 工具函式 ────────────────────────────────────────────────────────────────

function fmt(n, dec = 2) {
  if (n == null || isNaN(n)) return '—'
  return Number(n).toFixed(dec)
}
function fmtSign(n, dec = 2) {
  if (n == null || isNaN(n)) return '—'
  return (n >= 0 ? '+' : '') + Number(n).toFixed(dec)
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return '—'
  return (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%'
}

/** UTC 時間字串 → 台灣時間（UTC+8），顯示 MM-DD HH:mm。 */
function fmtTwTime(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T') + 'Z')
  if (isNaN(d.getTime())) return String(ts).slice(5, 16)   // 解析失敗回退原樣
  const tw = new Date(d.getTime() + 8 * 3600 * 1000)
  const p  = (x) => String(x).padStart(2, '0')
  return `${p(tw.getUTCMonth() + 1)}-${p(tw.getUTCDate())} ${p(tw.getUTCHours())}:${p(tw.getUTCMinutes())}`
}

/** 現在時刻 → 後端同格式 UTC 字串（供持倉持有時長計算）。 */
function nowUtcStr() {
  return new Date().toISOString().slice(0, 19).replace('T', ' ')
}

/** 平倉/部分列的類型標籤 + 顏色 + 詳細說明（tooltip）。 */
function typeLabel(row) {
  if (row.kind === 'open') return { txt: '持有中', cls: 'badge-flat', desc: '目前持倉中，尚未平倉' }
  const r = exitReason(row.exit_type, row.pnl)
  const cls = row.kind === 'scale' ? 'badge-system'
            : r.tone === 'pos'     ? 'badge-long'
            : r.tone === 'neg'     ? 'badge-short'
            :                        'badge-flat'
  return { txt: r.label, cls, desc: r.desc }
}

/** 迷你權益曲線 SVG sparkline。bals = 每筆成交後餘額（最新在前）。*/
function EquitySpark({ bals, color, height = 48, showLabel = true }) {
  const pts = [...bals].reverse().filter(b => b != null)
  if (pts.length < 2) {
    // 佔位：新 bot 還沒有平倉紀錄 → 顯示虛線基線，磁磚高度一致不跳動
    return (
      <div style={{ height, display: 'flex', alignItems: 'center', justifyContent: 'center',
                    borderBottom: '1px dashed var(--line)', marginTop: 4 }}>
        <span className="muted" style={{ fontSize: 10 }}>尚無平倉紀錄 · 曲線累積中</span>
      </div>
    )
  }

  const W = 300, H = height
  const minV = Math.min(...pts)
  const maxV = Math.max(...pts)
  const range = maxV - minV || 1

  const toX = i  => (i / (pts.length - 1)) * W
  const toY = v  => H - ((v - minV) / range) * (H - 4) - 2

  const polyline = pts.map((v, i) => `${toX(i)},${toY(v)}`).join(' ')
  const area = [
    `M ${toX(0)},${toY(pts[0])}`,
    ...pts.slice(1).map((v, i) => `L ${toX(i + 1)},${toY(v)}`),
    `L ${W},${H} L 0,${H} Z`,
  ].join(' ')

  const up = pts[pts.length - 1] >= pts[0]
  const lineColor = up ? 'var(--pos)' : 'var(--neg)'

  return (
    <div style={{ marginTop: 4 }}>
      {showLabel && (
        <div className="muted" style={{ fontSize: 10, marginBottom: 4, fontFamily: 'var(--font-display)' }}>
          獲利曲線
        </div>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
           style={{ display: 'block', overflow: 'visible' }}>
        <defs>
          <linearGradient id={`eq-fill-${color.replace(/\W/g, '')}`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={lineColor} stopOpacity="0.3" />
            <stop offset="100%" stopColor={lineColor} stopOpacity="0" />
          </linearGradient>
        </defs>
        {/* baseline */}
        <line x1="0" y1={toY(INIT_CAPITAL)} x2={W} y2={toY(INIT_CAPITAL)}
              stroke="var(--faint)" strokeWidth="0.5" strokeDasharray="3 3" />
        {/* fill */}
        <path d={area} fill={`url(#eq-fill-${color.replace(/\W/g, '')})`} />
        {/* line */}
        <polyline points={polyline} fill="none" stroke={lineColor} strokeWidth="1.5"
                  strokeLinejoin="round" strokeLinecap="round" />
        {/* endpoint dot */}
        <circle cx={toX(pts.length - 1)} cy={toY(pts[pts.length - 1])} r="3"
                fill={lineColor} />
      </svg>
    </div>
  )
}

/** 持倉即時情況條：SL ←─ 進場 · 現價 ─→ TP，標出現價落在停損/停利之間的位置。 */
function PositionBar({ entry, sl, tp, price, dir }) {
  if (!sl || !tp || !price) return null
  const lo = Math.min(sl, tp), hi = Math.max(sl, tp)
  const span = hi - lo || 1
  const frac = (x) => Math.max(0, Math.min(1, (x - lo) / span)) * 100
  const slIsLeft = sl <= tp
  return (
    <div style={{ marginTop: 2 }}>
      <div style={{
        position: 'relative', height: 6, borderRadius: 3,
        background: slIsLeft
          ? 'linear-gradient(90deg, var(--neg-soft), var(--pos-soft))'
          : 'linear-gradient(90deg, var(--pos-soft), var(--neg-soft))',
      }}>
        {/* 進場點 */}
        <div style={{
          position: 'absolute', left: `${frac(entry)}%`, top: -2, width: 2, height: 10,
          background: 'var(--text)', transform: 'translateX(-1px)',
        }} />
        {/* 現價點 */}
        <div style={{
          position: 'absolute', left: `${frac(price)}%`, top: -3, width: 8, height: 12,
          background: 'var(--accent)', borderRadius: 2, transform: 'translateX(-4px)',
        }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, marginTop: 3 }}>
        <span className={slIsLeft ? 'neg' : 'pos'}>{slIsLeft ? `SL ${fmt(sl)}` : `TP ${fmt(tp)}`}</span>
        <span className={slIsLeft ? 'pos' : 'neg'}>{slIsLeft ? `TP ${fmt(tp)}` : `SL ${fmt(sl)}`}</span>
      </div>
    </div>
  )
}

/** 小統計格：標籤 + 數值（進階統計區用）。 */
function MiniStat({ label, children, title }) {
  return (
    <div title={title} style={{
      background: 'var(--surface-2)', borderRadius: 'var(--radius-sm)', padding: '6px 8px',
      cursor: title ? 'help' : 'default',
    }}>
      <div className="muted" style={{ fontSize: 9, marginBottom: 2 }}>{label}</div>
      <div className="num" style={{ fontSize: 12, fontWeight: 600 }}>{children}</div>
    </div>
  )
}

const winPctOf = (trades, wins) => (trades > 0 ? Math.round((wins / trades) * 100) : null)


/** 根據策略 + 週期 → 一句白話說明目前的交易規劃。 */
function strategyPlan(strategy, interval) {
  const tf = interval || ''
  switch (strategy) {
    case 'fib_ema':
      return `等 ${tf} EMA 多空完整排列後順勢進場，Chandelier 追蹤停損保住趨勢浮盈，不追 ${tf} 以下短線噪音`
    case 'fib_channel':
      return `Fib 斜向通道均值回歸：在通道上下緣接 ${tf} 短線反彈，震盪盤賺差價；趨勢行情暫停進場`
    case 'trend_pullback':
      return `200EMA 判斷 ${tf} 主方向，等回踩到支撐區後 KD 觸發才順勢進場，不逆趨勢`
    default:
      return `${(strategy || '').replace(/_/g, ' ')} ${tf} 自動交易`
  }
}

// ── BotCard ─────────────────────────────────────────────────────────────────

function BotCard({ data, num, color, livePrice: propLivePrice, botId, defaultCollapsed = false }) {
  // 手動平倉狀態（hooks 必須無條件在最上方，故置於 !data 早退之前）
  const [closing, setClosing] = useState(false)
  const [closeMsg, setCloseMsg] = useState(null)
  const [confirmClose, setConfirmClose] = useState(false)
  const [collapsed, setCollapsed] = useState(defaultCollapsed)   // 多台籃子預設收合成一行摘要
  const [chartOpen, setChartOpen] = useState(false)   // 預設收合：卡片主體留給損益/持倉，要看再展開
  const [statsOpen, setStatsOpen] = useState(false)   // 進階統計+權益曲線 預設收合（減雜訊）

  // 即時價格方向追蹤：漲 → 綠閃、跌 → 紅閃
  const prevPriceRef            = useRef(null)
  const [priceDir, setPriceDir] = useState(0)   // 1=漲 -1=跌 0=持平
  const [flashKey, setFlashKey] = useState(0)
  useEffect(() => {
    if (propLivePrice == null) return
    if (prevPriceRef.current != null && propLivePrice !== prevPriceRef.current) {
      setPriceDir(propLivePrice > prevPriceRef.current ? 1 : -1)
      setFlashKey(k => k + 1)
    }
    prevPriceRef.current = propLivePrice
  }, [propLivePrice])

  async function handleClose() {
    setClosing(true); setCloseMsg(null); setConfirmClose(false)
    try {
      // N 台籃子走通用代理 /api/close/{botId}（bot 直連或 dashboard 代理，由 api 層決定）
      const r = await api.closeBot(botId)
      setCloseMsg(r?.ok ? { ok: true, text: r.msg || '已送出平倉' }
                        : { ok: false, text: r?.msg || '平倉失敗' })
    } catch (e) {
      setCloseMsg({ ok: false, text: String(e.message || e) })
    } finally {
      setClosing(false)
    }
  }

  if (!data) return (
    <div className="panel" style={{ borderTop: `2px solid ${color}`, opacity: 0.4 }}>
      <div style={{ fontSize: 11, fontFamily: 'var(--font-display)', color }}>Bot #{num}</div>
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>載入中…</div>
    </div>
  )

  // collapsed 摘要列用到的數值（必須在 hooks 之後、早退之後計算）
  const _dir  = data.direction ?? 0
  const _pos  = data.in_position

  const fresh    = data.age_seconds != null && data.age_seconds < (data.poll ? data.poll * 3 : 180)
  const realized = data.realized_pnl ?? 0
  const dir      = data.direction ?? 0
  const posBase  = data.base ?? 0

  // 即時現價：WS aggTrade 優先，REST 兜底
  const price = propLivePrice ?? data.price ?? null

  // 即時浮盈浮虧：有持倉 + 有即時現價 + 有進場價 → 前端即時計算；否則用 REST 快照
  const unrealLive = data.in_position && price && data.entry_price && posBase && dir !== 0
    ? (price - data.entry_price) * posBase * dir
    : null
  const unreal   = unrealLive ?? (data.unrealized_pnl ?? 0)

  // 優先用交易所回報的真實現金餘額（cash），避免漏記歷史交易造成誤差。
  // 無持倉時 netVal = cash；有持倉時加上即時浮盈（unreal 已含方向）。
  const cashBase = data.cash != null ? data.cash : (INIT_CAPITAL + realized)
  const netVal   = cashBase + (data.in_position ? unreal : 0)
  const netPct   = (netVal - INIT_CAPITAL) / INIT_CAPITAL * 100
  const winPct   = data.total_trades > 0
    ? Math.round((data.win_trades / data.total_trades) * 100)
    : null

  const posVal  = data.in_position && price ? Math.round(posBase * price) : 0

  const pairs  = pairTrades(data.recent_trades)
  const bals   = calcBalances(pairs, INIT_CAPITAL)
  const streak = lossStreak(pairs)

  // 持倉即時數據
  const openRow   = pairs.find(p => p.open)
  const scaledOut = openRow && openRow.orig_qty != null && openRow.qty < openRow.orig_qty - 1e-6
  const holdStr   = openRow ? holdDuration(openRow.entry_ts, nowUtcStr()) : '—'
  const distSL    = data.in_position && data.sl && price
    ? Math.abs(price - data.sl) / price * 100 : null
  const distTP    = data.in_position && data.tp && price
    ? Math.abs(data.tp - price) / price * 100 : null
  const posRoi    = roiPct(unreal, posVal)

  return (
    <div className="panel" style={{
      borderTop: `2px solid ${color}`,
      display: 'flex', flexDirection: 'column', gap: 8,
    }}>

      {/* ── 磁磚標頭（點擊展開詳情）：幣種為主角 + 即時價 ── */}
      <div
        onClick={() => setCollapsed(c => !c)}
        style={{ cursor: 'pointer', userSelect: 'none',
                 display: 'flex', alignItems: 'baseline', gap: 8 }}
      >
        <span style={{ fontFamily: 'var(--font-display)', fontSize: 15, fontWeight: 700 }}>
          {String(data.symbol || '').replace('USDT', '')}
          <span className="muted" style={{ fontSize: 10, fontWeight: 400 }}>/USDT</span>
        </span>
        <span className="muted" style={{ fontSize: 10 }}>
          {String(data.strategy || '').replace(/_/g, ' ')} · {data.interval}
        </span>
        {price != null && (
          <span
            key={flashKey}
            className={priceDir > 0 ? 'price-flash-up' : priceDir < 0 ? 'price-flash-down' : ''}
            style={{ fontFamily: 'var(--font-display)', fontSize: 14, fontWeight: 700, marginLeft: 'auto' }}
          >
            ${fmt(price)}
          </span>
        )}
        <span
          style={{ width: 7, height: 7, borderRadius: '50%', flexShrink: 0, alignSelf: 'center',
            background: fresh ? 'var(--pos)' : 'var(--faint)', display: 'inline-block' }}
          title={fresh ? '在線' : '離線'}
        />
        <span style={{ fontSize: 11, color: 'var(--muted)', flexShrink: 0 }}>
          {collapsed ? '▸' : '▾'}
        </span>
      </div>

      {/* ── 獲利曲線（磁磚主視覺，永遠可見）── */}
      <EquitySpark bals={bals} color={color} height={56} showLabel={false} />

      {/* ── 一行統計：本台累計損益 · 勝率 · 筆數 · 持倉 ── */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, flexWrap: 'wrap' }}>
        <span className={`num ${realized + unreal >= 0 ? 'pos' : 'neg'}`}
              style={{ fontSize: 17, fontWeight: 700 }}
              title="本台自己帳本：實現＋浮動損益">
          {fmtSign(realized + unreal)}
        </span>
        {data.total_trades > 0 && (
          <span className="muted" style={{ fontSize: 11 }}>
            勝率 <span className="num">{winPct}%</span> · {data.total_trades} 筆
          </span>
        )}
        {_pos ? (
          <span className={`badge ${_dir === 1 ? 'badge-long' : 'badge-short'}`}
                style={{ fontSize: 10, marginLeft: 'auto' }}>
            {_dir === 1 ? '持多' : '持空'} {unreal !== 0 && fmtSign(unreal)}
          </span>
        ) : (
          <span className="badge badge-flat" style={{ fontSize: 10, marginLeft: 'auto' }}>空手</span>
        )}
      </div>

      {/* ── 點開才顯示的詳情 ── */}
      {!collapsed && (<>

      {/* ── 連續同方向虧損警示（通道方向可能已反轉）── */}
      {streak && streak.count >= 2 && (
        <div style={{
          background: 'var(--neg-soft)', border: '1px solid var(--neg)',
          borderRadius: 'var(--radius-sm)', padding: '7px 10px', fontSize: 11, lineHeight: 1.4,
        }}>
          <span style={{ color: 'var(--neg)', fontWeight: 600 }}>⚠ 連續 {streak.count} 筆
            {streak.dir === 'short' ? '做空' : '做多'}虧損</span>
          <span className="muted">（{fmtSign(streak.totalPnl)}）— 通道方向可能已反轉，宜留意是否續逆勢接刀</span>
        </div>
      )}

      {/* ── 實現/浮動拆分（磁磚統計列已有累計，這裡給拆分明細）── */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'wrap' }}>
        <span className="muted" style={{ fontSize: 11 }}>
          實現 <span className={realized >= 0 ? 'pos' : 'neg'}>{fmtSign(realized)}</span>
        </span>
        <span className="muted" style={{ fontSize: 11 }}>
          浮盈 <span className={unreal >= 0 ? 'pos' : 'neg'}>{fmtSign(unreal)}</span>
        </span>
      </div>

      {/* ── 詳細統計（回撤/夏普/多空，預設收合減雜訊）── */}
      {data.total_trades > 0 && (
        <div>
          <div
            onClick={() => setStatsOpen(o => !o)}
            style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6,
                     marginBottom: statsOpen ? 6 : 0 }}
          >
            <span className="muted" style={{ fontSize: 11 }}>詳細統計</span>
            <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--muted)' }}>
              {statsOpen ? '▾ 收起' : '▸ 展開'}
            </span>
          </div>
          {statsOpen && (<>
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
              <MiniStat label="回撤" title="已實現權益曲線歷史最大峰谷跌幅%">
                <span className="neg">
                  {data.max_drawdown_pct != null ? `-${fmt(data.max_drawdown_pct)}%` : '—'}
                </span>
              </MiniStat>
              <MiniStat label="夏普" title="每筆 pnl/名目 的 均值÷標準差（非年化）">
                <span className={data.sharpe == null ? 'muted' : data.sharpe >= 0 ? 'pos' : 'neg'}>
                  {data.sharpe != null ? fmt(data.sharpe, 2) : '—'}
                </span>
              </MiniStat>
              <MiniStat label={`多 ${data.long_trades || 0}`} title="做多：勝率 + 累計損益">
                <span>{winPctOf(data.long_trades, data.long_wins) ?? '—'}{winPctOf(data.long_trades, data.long_wins) != null && '%'}</span>
                <span className={(data.long_pnl ?? 0) >= 0 ? 'pos' : 'neg'} style={{ marginLeft: 5, fontSize: 10 }}>
                  {fmtSign(data.long_pnl)}
                </span>
              </MiniStat>
              <MiniStat label={`空 ${data.short_trades || 0}`} title="做空：勝率 + 累計損益">
                <span>{winPctOf(data.short_trades, data.short_wins) ?? '—'}{winPctOf(data.short_trades, data.short_wins) != null && '%'}</span>
                <span className={(data.short_pnl ?? 0) >= 0 ? 'pos' : 'neg'} style={{ marginLeft: 5, fontSize: 10 }}>
                  {fmtSign(data.short_pnl)}
                </span>
              </MiniStat>
            </div>
            {/* 完善數據（2026-07-05）：期望值/獲利因子/平均賺虧/最大連虧/持倉時長 */}
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 6 }}>
              <MiniStat label="期望/筆" title="平均每筆已平倉損益（USDT）；>0 才是正期望系統">
                <span className={data.expectancy == null ? 'muted' : data.expectancy >= 0 ? 'pos' : 'neg'}>
                  {data.expectancy != null ? fmtSign(data.expectancy) : '—'}
                </span>
              </MiniStat>
              <MiniStat label="獲利因子" title="總賺÷總虧；>1 賺錢、<1 虧錢；無虧損時顯示 —">
                <span className={data.profit_factor == null ? 'muted' : data.profit_factor >= 1 ? 'pos' : 'neg'}>
                  {data.profit_factor != null ? fmt(data.profit_factor, 2) : '—'}
                </span>
              </MiniStat>
              <MiniStat label="均賺/均虧" title="贏單平均獲利 / 輸單平均虧損（賠率結構）">
                <span className="pos">{data.avg_win != null ? fmt(data.avg_win) : '—'}</span>
                <span className="muted" style={{ margin: '0 3px' }}>/</span>
                <span className="neg">{data.avg_loss != null ? fmt(data.avg_loss) : '—'}</span>
              </MiniStat>
              <MiniStat label="最大連虧" title="歷史最長連續虧損筆數（熔斷器參考：3 筆暫停）">
                <span className={(data.max_consec_losses ?? 0) >= 3 ? 'neg' : ''}>
                  {data.max_consec_losses ?? '—'}
                </span>
              </MiniStat>
              <MiniStat label="均持倉" title="平均每筆持倉時長（小時）">
                <span>{data.avg_hold_hours != null ? `${fmt(data.avg_hold_hours, 1)}h` : '—'}</span>
              </MiniStat>
            </div>
          </>)}
        </div>
      )}

      {/* ── K 線 + 技術位（可收合）── */}
      <div>
        <div
          onClick={() => setChartOpen(o => !o)}
          style={{ cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 6, marginBottom: chartOpen ? 4 : 0 }}
        >
          <span className="muted" style={{ fontSize: 11 }}>
            K 線 · {String(data.strategy || '').replace('_', ' ')}
          </span>
          <span className="muted" style={{ fontSize: 10 }}>{data.symbol}</span>
          <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--muted)' }}>
            {chartOpen ? '▾ 收起' : '▸ 展開'}
          </span>
        </div>
        {chartOpen && (
          <MiniChart
            symbol={data.symbol}
            interval={data.interval}
            strategy={data.strategy}
            entry={data.entry_price}
            sl={data.sl}
            tp={data.tp}
            inPosition={!!data.in_position}
            trades={data.recent_trades}
          />
        )}
      </div>

      {/* ── 持倉即時情況 ── */}
      {data.in_position ? (
        <div style={{
          background: 'var(--surface-2)', borderRadius: 'var(--radius-sm)', padding: '8px 10px',
          display: 'flex', flexDirection: 'column', gap: 7,
        }}>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <span className={`badge ${dir === 1 ? 'badge-long' : 'badge-short'}`} style={{ fontSize: 11 }}>
              {dir === 1 ? '持多' : '持空'}
            </span>
            <span style={{ fontSize: 12 }}>
              進場 <span className="num">${fmt(data.entry_price)}</span>
            </span>
            <span style={{ fontSize: 12 }}>
              數量 <span className="num">
                {fmt(posBase, 4)} {String(data.symbol || '').replace('USDT', '')}
              </span>
            </span>
            {scaledOut && (
              <span className="badge badge-system" style={{ fontSize: 9 }}>已部分了結</span>
            )}
            <span className="muted" style={{ fontSize: 11, marginLeft: 'auto' }}>持有 {holdStr}</span>
          </div>

          {/* 即時數據列 */}
          <div style={{ display: 'flex', gap: 14, fontSize: 12, flexWrap: 'wrap' }}>
            <span>
              倉位 <span className="num" style={{ color }}>${posVal.toLocaleString()}</span>
            </span>
            {price != null && (
              <span>現價 <span className="num">${fmt(price)}</span></span>
            )}
            <span className={`${unreal >= 0 ? 'pos' : 'neg'}`} style={{ marginLeft: 'auto', fontWeight: 600 }}>
              {fmtSign(unreal)} {posRoi != null && `(${fmtPct(posRoi)})`}
            </span>
          </div>

          {/* SL←現價→TP 即時情況條 */}
          <PositionBar entry={data.entry_price} sl={data.sl} tp={data.tp} price={price} dir={dir} />
          <div style={{ display: 'flex', gap: 14, fontSize: 11 }}>
            <span className="neg">距 SL {distSL != null ? distSL.toFixed(2) + '%' : '—'}</span>
            <span className="pos">距 TP {distTP != null ? distTP.toFixed(2) + '%' : '—'}</span>
          </div>

          {/* 手動平倉（結算）：兩步確認，避免誤觸 */}
          {confirmClose ? (
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', marginTop: 2 }}>
              <span className="neg" style={{ fontSize: 11 }}>確定平倉？</span>
              <button
                onClick={handleClose}
                disabled={closing}
                style={{
                  padding: '4px 10px', fontSize: 12, fontWeight: 700,
                  borderRadius: 'var(--radius-sm)', cursor: closing ? 'wait' : 'pointer',
                  border: '1px solid var(--neg)', color: 'var(--neg)',
                  background: 'var(--neg-soft)',
                }}
              >
                {closing ? '平倉中…' : '確認'}
              </button>
              <button
                onClick={() => setConfirmClose(false)}
                style={{
                  padding: '4px 10px', fontSize: 12,
                  borderRadius: 'var(--radius-sm)', cursor: 'pointer',
                  border: '1px solid var(--muted)', color: 'var(--muted)',
                  background: 'transparent',
                }}
              >
                取消
              </button>
            </div>
          ) : (
            <button
              onClick={() => setConfirmClose(true)}
              disabled={closing}
              style={{
                marginTop: 2, padding: '6px 10px', fontSize: 12, fontWeight: 600,
                borderRadius: 'var(--radius-sm)', cursor: 'pointer',
                border: '1px solid var(--neg)', color: 'var(--neg)',
                background: 'var(--neg-soft)',
              }}
              title="以市價立即平掉目前持倉（測試網虛擬倉）。bot 之後仍會繼續自動交易。"
            >
              ⏹ 手動平倉（結算）
            </button>
          )}
        </div>
      ) : (
        <div style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className="badge badge-flat">空手</span>
          {price != null && (
            <span className="muted">現價 ${fmt(price)}</span>
          )}
        </div>
      )}
      {/* 平倉結果訊息：放在 in_position 外，平倉後仍可見 */}
      {closeMsg && (
        <div className={closeMsg.ok ? 'pos' : 'neg'} style={{ fontSize: 11, marginTop: 4 }}>
          {closeMsg.ok ? '✓ ' : '⚠ '}{closeMsg.text}
        </div>
      )}

      {/* ── 交易紀錄（含部分了結、類型、報酬%、持有時長）── */}
      <div>
        <div style={{
          fontSize: 10, fontFamily: 'var(--font-display)',
          color: 'var(--muted)', marginBottom: 6,
        }}>
          交易紀錄
        </div>

        {pairs.length === 0 ? (
          <div className="muted" style={{ fontSize: 12 }}>// 尚無成交</div>
        ) : (
          <div style={{ overflowX: 'auto', maxHeight: 240, overflowY: 'auto' }}>
          <table style={{ width: '100%', fontSize: 11, borderCollapse: 'collapse', minWidth: 260 }}>
            <thead>
              <tr>
                {[
                  ['時間',  'left'],
                  ['方向',  'left'],
                  ['進場',  'right'],
                  ['平倉',  'right'],
                  ['損益',  'right'],
                ].map(([h, align]) => (
                  <th key={h} style={{
                    textAlign: align, paddingBottom: 4,
                    color: 'var(--muted)', fontWeight: 400, fontSize: 10,
                  }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pairs.slice(0, 12).map((p, i) => {
                const tl  = typeLabel(p)
                const roi = roiPct(p.pnl, p.pos_value)
                const hold = p.open ? holdStr : holdDuration(p.entry_ts, p.ts)
                return (
                  <tr key={i} style={{ borderTop: '1px solid var(--line)' }}>
                    <td style={{ padding: '5px 0', color: 'var(--muted)', whiteSpace: 'nowrap' }}>
                      {fmtTwTime(p.ts)}
                      <div style={{ fontSize: 9, opacity: 0.7 }}>{hold !== '—' ? hold : ''}</div>
                    </td>
                    <td style={{ padding: '5px 4px 5px 0' }}>
                      <span className={`badge ${p.dir === 'long' ? 'badge-long' : 'badge-short'}`}
                            style={{ fontSize: 9 }}>
                        {p.dir === 'long' ? '多' : '空'}
                      </span>
                      <span className={`badge ${tl.cls}`} style={{ fontSize: 9, marginLeft: 3, cursor: 'help' }}
                            title={tl.desc}>{tl.txt}</span>
                    </td>
                    <td className="num" style={{ textAlign: 'right', padding: '5px 2px' }}>
                      {p.entry_price != null ? `$${fmt(p.entry_price)}` : '—'}
                    </td>
                    <td className="num" style={{ textAlign: 'right', padding: '5px 2px' }}>
                      {p.exit_price != null
                        ? `$${fmt(p.exit_price)}`
                        : <span className="muted" style={{ fontSize: 10 }}>{p.open ? '持有中' : '—'}</span>
                      }
                    </td>
                    <td style={{ textAlign: 'right', padding: '5px 0' }}>
                      <span className={`num ${p.pnl == null ? '' : p.pnl >= 0 ? 'pos' : 'neg'}`}
                            style={{ fontWeight: 600 }}>
                        {p.pnl != null ? fmtSign(p.pnl) : '—'}
                      </span>
                      {roi != null && (
                        <div className={`num ${roi >= 0 ? 'pos' : 'neg'}`} style={{ fontSize: 9, opacity: 0.8 }}>
                          {fmtPct(roi)}
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          </div>
        )}
      </div>

      </>)}
    </div>
  )
}


// ── Live ─────────────────────────────────────────────────────────────────────

export default function Live() {
  const refreshMs = Number(import.meta.env.VITE_LIVE_REFRESH_MS || 20000)   // 唯一持續的狀態輪詢（背景頁籤自動暫停）
  const pricePollMs = Number(import.meta.env.VITE_LIVE_PRICE_POLL_MS || 2000)
  const [bots,       setBots]      = useState([])   // [{id, meta, data}] 動態 N 台
  const [tick,       setTick]      = useState(0)
  const [livePrices, setLivePrices]= useState({})   // { SOLUSDT: 168.42, ETHUSDT: 3245.1 }
  const timer = useRef(null)

  async function load() {
    // bot 容器直連（/bots 清單 + 逐台 /live），支援 N 台、不吃 dashboard 資源。
    // 2026-07-05 清理：移除舊四端點 fallback——目標服務已關閉合併，fallback 永遠拿不到資料。
    try {
      const list = await api.bots()
      if (Array.isArray(list) && list.length) {
        const datas = await Promise.all(list.map(async (b) => {
          try { return { id: b.id, meta: b, data: await api.botLive(b.id) } }
          catch { return { id: b.id, meta: b, data: null } }
        }))
        setBots(datas)
      }
    } catch { /* bot 容器暫時不可達 → 保留上一輪資料，下一輪重試 */ }
  }

  useEffect(() => {
    load()
    timer.current = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
      load(); setTick(t => t + 1)
    }, refreshMs)
    return () => clearInterval(timer.current)
  }, [refreshMs])

  // ── 即時現價：每個不同 symbol 輪詢 Binance 公開 API（不經 Railway）──────────
  const symbolKey = [...new Set(bots.map(b => b.data?.symbol || b.meta?.symbol).filter(Boolean))]
    .sort().join(',')

  useEffect(() => {
    if (!symbolKey) return
    const symbols = symbolKey.split(',')

    async function fetchPrices() {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
      const update = {}
      await Promise.all(symbols.map(async sym => {
        try {
          const res  = await fetch(`https://fapi.binance.com/fapi/v1/ticker/price?symbol=${sym}`)
          const data = await res.json()
          const p    = parseFloat(data.price)
          if (p > 0) update[sym] = p
        } catch {}
      }))
      if (Object.keys(update).length) setLivePrices(prev => ({ ...prev, ...update }))
    }

    fetchPrices()
    const t = setInterval(fetchPrices, pricePollMs)
    return () => clearInterval(t)
  }, [symbolKey, pricePollMs])

  const datas = bots.map(b => b.data).filter(Boolean)
  const anyFresh = datas.some(x => x?.age_seconds != null && x.age_seconds < 300)
  // ── 組合層 KPI（總數據置頂）：Σ損益 / 勝率 / 筆數 / 帳戶 / 持倉 ──
  const acctCash      = datas.find(x => x?.cash != null)?.cash
  const totalRealized = datas.reduce((s, x) => s + (x?.realized_pnl || 0), 0)
  const totalUnreal   = datas.reduce((s, x) => s + (x?.unrealized_pnl || 0), 0)
  const totalPnl      = totalRealized + totalUnreal
  const totalTrades   = datas.reduce((s, x) => s + (x?.total_trades || 0), 0)
  const totalWins     = datas.reduce((s, x) => s + (x?.win_trades || 0), 0)
  const poolWinPct    = totalTrades > 0 ? Math.round((totalWins / totalTrades) * 100) : null
  const inPosCount    = datas.filter(x => x?.in_position).length

  const kpi = (label, node, hint) => (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 108 }} title={hint || ''}>
      <span className="muted" style={{ fontSize: 10, fontFamily: 'var(--font-display)' }}>{label}</span>
      {node}
    </div>
  )

  return (
    <>
      {/* ── 總數據（組合層 KPI 置頂）── */}
      <div className={`panel${anyFresh ? ' is-active' : ''}`}
           style={{ display: 'flex', alignItems: 'flex-start', gap: 22, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span className={`status-pulse${anyFresh ? '' : ' is-offline'}`} />
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2, paddingLeft: 8 }}>
            <span style={{ fontFamily: 'var(--font-display)', fontSize: 13, fontWeight: 700 }}>
              總覽 {anyFresh ? '· 運行中' : '· 待命'}
            </span>
            <span className="badge badge-system" style={{ fontSize: 9 }}>合約測試網</span>
          </div>
        </div>
        {kpi('總損益（實現＋浮動）',
          <span className={`num ${totalPnl >= 0 ? 'pos' : 'neg'}`} style={{ fontSize: 22, fontWeight: 700 }}>
            {fmtSign(totalPnl)}
          </span>, '八台自己帳本的合計')}
        {kpi('勝率',
          <span className="num" style={{ fontSize: 22, fontWeight: 700 }}>
            {poolWinPct != null ? `${poolWinPct}%` : '—'}
            <span className="muted" style={{ fontSize: 11, fontWeight: 400 }}> / {totalTrades} 筆</span>
          </span>, '全部平倉交易匯總')}
        {kpi('帳戶餘額',
          <span className="num" style={{ fontSize: 22, fontWeight: 700 }}>
            {acctCash != null ? `$${fmt(acctCash)}` : '—'}
          </span>, '共用測試網帳戶 USDT')}
        {kpi('持倉',
          <span className="num" style={{ fontSize: 22, fontWeight: 700 }}>
            {inPosCount}<span className="muted" style={{ fontSize: 11, fontWeight: 400 }}> / {bots.length} 台</span>
          </span>)}
        <span className="badge badge-system" style={{ marginLeft: 'auto', alignSelf: 'flex-start' }}>
          每 {Math.max(1, Math.round(refreshMs / 1000))}s 刷新（#<span className="num">{tick}</span>）
        </span>
      </div>

      {/* ── 幣種磁磚（獲利曲線為主視覺，點開看詳情）── */}
      <div className="bots-grid">
        {bots.map((b, i) => (
          <BotCard key={b.id} botId={b.id} data={b.data} num={i + 1}
                   color={BOT_COLORS[i % BOT_COLORS.length]}
                   livePrice={livePrices[b.data?.symbol || b.meta?.symbol]}
                   defaultCollapsed={true} />
        ))}
      </div>
    </>
  )
}
