/**
 * TechPanel — 技術分析指標視覺化面板
 * Props: ind (object from last_decision.ind), price, direction, entry_price, sl, tp
 */

/* ── RSI 半圓儀表 ─────────────────────────────────────── */
function RsiGauge({ rsi }) {
  if (rsi == null) return <GaugePlaceholder label="RSI" />
  const v = Math.min(Math.max(Number(rsi), 0), 100)
  // semicircle: left = 180°, right = 0°, needle at angle = 180 - v*1.8
  const cx = 60, cy = 60, r = 48
  function arc(a1, a2, color, w = 8) {
    const toRad = d => (d * Math.PI) / 180
    const x1 = cx + r * Math.cos(toRad(a1))
    const y1 = cy - r * Math.sin(toRad(a1))
    const x2 = cx + r * Math.cos(toRad(a2))
    const y2 = cy - r * Math.sin(toRad(a2))
    const large = Math.abs(a1 - a2) > 180 ? 1 : 0
    return <path d={`M${x1},${y1} A${r},${r},0,${large},0,${x2},${y2}`}
      fill="none" stroke={color} strokeWidth={w} strokeLinecap="round" />
  }
  const ang = 180 - v * 1.8   // 180° = 0, 0° = 100
  const nx = cx + (r - 14) * Math.cos((ang * Math.PI) / 180)
  const ny = cy - (r - 14) * Math.sin((ang * Math.PI) / 180)
  const color = v < 30 ? '#2a8' : v > 70 ? '#e05' : 'var(--accent)'
  const label = v < 30 ? '超賣' : v > 70 ? '超買' : v < 50 ? '偏弱' : '偏強'
  return (
    <div style={{ textAlign: 'center' }}>
      <svg viewBox="0 0 120 72" width={130} height={80}>
        {arc(180, 0, 'var(--border)', 9)}
        {arc(180, 126, '#2a8', 9)}      {/* 0–30 oversold green */}
        {arc(126, 54, 'var(--border)', 9)}{/* 30–70 neutral */}
        {arc(54, 0, '#e05', 9)}          {/* 70–100 overbought red */}
        <line x1={cx} y1={cy} x2={nx} y2={ny}
          stroke={color} strokeWidth={2.5} strokeLinecap="round" />
        <circle cx={cx} cy={cy} r={3} fill={color} />
        <text x={cx} y={cy + 12} textAnchor="middle" fontSize={12} fontWeight={700} fill={color}>
          {v.toFixed(1)}
        </text>
        <text x={10} y={70} fontSize={8} fill="#2a8">超賣</text>
        <text x={110} y={70} fontSize={8} fill="#e05" textAnchor="end">超買</text>
      </svg>
      <div style={{ fontSize: 11, color, fontWeight: 600, marginTop: -4 }}>{label}</div>
      <div style={{ fontSize: 10, color: 'var(--muted)' }}>RSI (14)</div>
    </div>
  )
}

/* ── Fibonacci 位置條 ─────────────────────────────────── */
function FibBar({ fibPos, fib382, fib618, price }) {
  if (fibPos == null) return <GaugePlaceholder label="Fibonacci" />
  const pos = Math.min(Math.max(Number(fibPos), 0), 1.5)
  const pct = (pos / 1.5) * 100          // scale 0–1.5 → 0–100%
  const zone382 = (0.382 / 1.5) * 100   // 25.5%
  const zone618 = (0.618 / 1.5) * 100   // 41.2%
  const exit55  = (0.55  / 1.5) * 100
  const exit45  = (0.45  / 1.5) * 100

  const inLongZone  = pos < 0.382
  const inShortZone = pos > 0.618
  const dotColor    = inLongZone ? '#2a8' : inShortZone ? '#e05' : 'var(--muted)'
  const signal      = inLongZone ? '在多單進場區' : inShortZone ? '在空單進場區' : '中性區'

  return (
    <div style={{ width: '100%' }}>
      <div style={{ fontSize: 10, color: 'var(--muted)', marginBottom: 4 }}>
        Fib 位置（0=低點 → 1=高點）
      </div>
      <div style={{ position: 'relative', height: 20, borderRadius: 4, overflow: 'hidden',
        background: 'var(--border)' }}>
        {/* long entry zone */}
        <div style={{ position: 'absolute', left: 0, width: `${zone382}%`, height: '100%',
          background: '#2a833' }} />
        {/* short entry zone */}
        <div style={{ position: 'absolute', left: `${zone618}%`, right: 0, height: '100%',
          background: '#e0523' }} />
        {/* exit lines */}
        <div style={{ position: 'absolute', left: `${exit45}%`, top: 0, width: 1, height: '100%',
          background: 'rgba(255,255,255,0.3)' }} />
        <div style={{ position: 'absolute', left: `${exit55}%`, top: 0, width: 1, height: '100%',
          background: 'rgba(255,255,255,0.3)' }} />
        {/* current marker */}
        <div style={{ position: 'absolute', left: `${Math.min(pct, 99)}%`, top: 0,
          width: 3, height: '100%', background: dotColor,
          transform: 'translateX(-50%)', borderRadius: 2 }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9,
        color: 'var(--muted)', marginTop: 2 }}>
        <span style={{ color: '#2a8' }}>做多區 &lt;0.382</span>
        <span>0.5</span>
        <span style={{ color: '#e05' }}>&gt;0.618 做空區</span>
      </div>
      {fib382 && fib618 && (
        <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 4 }}>
          支撐 {Number(fib382).toFixed(1)} · 阻力 {Number(fib618).toFixed(1)}
        </div>
      )}
      <div style={{ fontSize: 12, fontWeight: 600, color: dotColor, marginTop: 4 }}>
        {signal}（{Number(fibPos).toFixed(3)}）
      </div>
    </div>
  )
}

/* ── Regime 投票 ──────────────────────────────────────── */
function RegimerVote({ er, chop, adx, regime }) {
  const votes = [
    {
      label: 'ER', value: er != null ? Number(er).toFixed(3) : null,
      rangeWhen: v => v < 0.3,
      desc: (v, isRange) => isRange ? `${v} < 0.3 → 盤整` : `${v} ≥ 0.3 → 趨勢`,
    },
    {
      label: 'CHOP', value: chop != null ? Number(chop).toFixed(1) : null,
      rangeWhen: v => v > 61.8,
      desc: (v, isRange) => isRange ? `${v} > 61.8 → 盤整` : `${v} ≤ 61.8 → 趨勢`,
    },
    {
      label: 'ADX', value: adx != null ? Number(adx).toFixed(1) : null,
      rangeWhen: v => v < 25,
      desc: (v, isRange) => isRange ? `${v} < 25 → 盤整` : `${v} ≥ 25 → 趨勢`,
    },
  ]
  const rangeVotes = votes.filter(v => v.value != null && v.rangeWhen(Number(v.value))).length
  const trendVotes = votes.filter(v => v.value != null && !v.rangeWhen(Number(v.value))).length
  const isRange = rangeVotes >= 2
  const regimeColor = isRange ? 'var(--accent)' : '#e05'
  const regimeLabel = regime === 'range' ? '盤整盤' : '趨勢盤'

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 8 }}>
        {votes.map(v => {
          if (v.value == null) return null
          const isR = v.rangeWhen(Number(v.value))
          const c = isR ? 'var(--accent)' : '#e05'
          return (
            <div key={v.label} style={{ flex: 1, background: 'var(--border)',
              borderRadius: 6, padding: '6px 8px', textAlign: 'center' }}>
              <div style={{ fontSize: 10, color: 'var(--muted)' }}>{v.label}</div>
              <div style={{ fontSize: 14, fontWeight: 700, color: c }}>{v.value}</div>
              <div style={{ fontSize: 9, color: c }}>{isR ? '盤整' : '趨勢'}</div>
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>2/3 多數決 →</span>
        <span style={{ padding: '2px 10px', borderRadius: 8, fontWeight: 700,
          background: regimeColor + '22', color: regimeColor,
          border: `1px solid ${regimeColor}44`, fontSize: 13 }}>
          {regimeLabel}（{isRange ? `盤整 ${rangeVotes}` : `趨勢 ${trendVotes}`} 票）
        </span>
      </div>
      {!isRange && (
        <div style={{ fontSize: 11, color: '#e05', marginTop: 4 }}>
          ⚠ 趨勢盤 — fib_retracement 進場被 Regime 擋住
        </div>
      )}
    </div>
  )
}

/* ── EMA 趨勢方向 ────────────────────────────────────── */
function EmaTrend({ price, emaTrend }) {
  if (price == null || emaTrend == null) return <GaugePlaceholder label="EMA 200" />
  const p = Number(price), e = Number(emaTrend)
  const above = p > e
  const pct = ((p - e) / e * 100).toFixed(2)
  const color = above ? '#2a8' : '#e05'
  const arrow = above ? '↑' : '↓'
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 36, color, lineHeight: 1 }}>{arrow}</div>
      <div style={{ fontSize: 13, fontWeight: 700, color }}>
        {above ? '上升趨勢' : '下降趨勢'}
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 4 }}>
        現價 {p.toFixed(1)} · EMA200 {e.toFixed(1)}
      </div>
      <div style={{ fontSize: 12, color, fontWeight: 600 }}>
        {above ? '+' : ''}{pct}%
      </div>
      <div style={{ fontSize: 10, color: 'var(--muted)' }}>
        {above ? '允許做多進場' : '允許做空進場'}
      </div>
    </div>
  )
}

/* ── ATR 波動度 + 停損停利距離 ──────────────────────── */
function AtrPanel({ atr, price, sl, tp, entryPrice, inPos, direction }) {
  if (atr == null || price == null) return <GaugePlaceholder label="ATR" />
  const a = Number(atr), p = Number(price)
  const atrPct = (a / p * 100).toFixed(3)
  const slDist = sl && entryPrice ? Math.abs(Number(entryPrice) - Number(sl)) : a * 2
  const tpDist = tp && entryPrice ? Math.abs(Number(tp) - Number(entryPrice)) : a * 4
  const rr = tpDist / slDist

  return (
    <div style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: 'var(--accent)' }}>{a.toFixed(1)}</div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>ATR (14)</div>
        </div>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 18, fontWeight: 700 }}>{atrPct}%</div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>佔現價比</div>
        </div>
        <div style={{ textAlign: 'center' }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: '#2a8' }}>{rr.toFixed(1)}R</div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>盈虧比</div>
        </div>
      </div>
      {inPos && entryPrice && (
        <>
          <div style={{ fontSize: 10, color: 'var(--muted)', marginBottom: 4 }}>
            持倉停損 / 停利距離
          </div>
          <div style={{ position: 'relative', height: 12, borderRadius: 6, overflow: 'visible',
            background: 'var(--border)', marginBottom: 2 }}>
            {/* SL zone */}
            <div style={{
              position: 'absolute',
              left: direction === 1 ? 0 : `${(tpDist / (slDist + tpDist)) * 100}%`,
              width: `${(slDist / (slDist + tpDist)) * 100}%`,
              height: '100%', background: '#e0533', borderRadius: direction === 1 ? '6px 0 0 6px' : '0 6px 6px 0',
            }} />
            {/* TP zone */}
            <div style={{
              position: 'absolute',
              left: direction === 1 ? `${(slDist / (slDist + tpDist)) * 100}%` : 0,
              width: `${(tpDist / (slDist + tpDist)) * 100}%`,
              height: '100%', background: '#2a833',
              borderRadius: direction === 1 ? '0 6px 6px 0' : '6px 0 0 6px',
            }} />
            {/* entry marker */}
            <div style={{ position: 'absolute',
              left: direction === 1 ? `${(slDist / (slDist + tpDist)) * 100}%` : `${(tpDist / (slDist + tpDist)) * 100}%`,
              top: -2, width: 3, height: 16, background: 'white',
              transform: 'translateX(-50%)', borderRadius: 2 }} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10 }}>
            <span style={{ color: '#e05' }}>SL −{slDist.toFixed(1)}</span>
            <span style={{ color: 'var(--muted)' }}>進場</span>
            <span style={{ color: '#2a8' }}>TP +{tpDist.toFixed(1)}</span>
          </div>
        </>
      )}
    </div>
  )
}

/* ── 信號總結 ────────────────────────────────────────── */
function SignalSummary({ ind, target, direction }) {
  if (!ind) return null
  const regime = ind.regime
  const fibPos = ind.fib_pos != null ? Number(ind.fib_pos) : null
  const rsi    = ind.rsi    != null ? Number(ind.rsi)    : null
  const er     = ind.er     != null ? Number(ind.er)     : null
  const chop   = ind.chop   != null ? Number(ind.chop)   : null
  const adx    = ind.adx    != null ? Number(ind.adx)    : null

  const isRange = regime === 'range' ||
    [er != null && er < 0.3, chop != null && chop > 61.8, adx != null && adx < 25]
      .filter(Boolean).length >= 2

  const checks = [
    { label: 'Regime 盤整', pass: isRange },
    { label: 'Fib 進多區', pass: fibPos != null && fibPos < 0.382 },
    { label: 'Fib 進空區', pass: fibPos != null && fibPos > 0.618 },
    { label: 'RSI < 55', pass: rsi != null && rsi < 55 },
    { label: 'RSI < 50', pass: rsi != null && rsi < 50 },
  ]

  const longReady  = isRange && fibPos < 0.382 && rsi < 55
  const shortReady = isRange && fibPos > 0.618 && rsi < 50
  const sig = longReady ? 'LONG' : shortReady ? 'SHORT' : 'FLAT'
  const sigColor = sig === 'LONG' ? '#2a8' : sig === 'SHORT' ? '#e05' : 'var(--muted)'

  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
      <div style={{ textAlign: 'center', minWidth: 80 }}>
        <div style={{ fontSize: 28, fontWeight: 900, color: sigColor }}>{sig}</div>
        <div style={{ fontSize: 10, color: 'var(--muted)' }}>當前訊號</div>
      </div>
      <div style={{ flex: 1 }}>
        {[
          { label: 'Regime 盤整（盤整策略前提）', pass: isRange },
          { label: `Fib ${fibPos != null ? fibPos.toFixed(3) : '?'} < 0.382（做多支撐區）`, pass: fibPos != null && fibPos < 0.382 },
          { label: `Fib ${fibPos != null ? fibPos.toFixed(3) : '?'} > 0.618（做空阻力區）`, pass: fibPos != null && fibPos > 0.618 },
          { label: `RSI ${rsi != null ? rsi.toFixed(1) : '?'} < 55（做多 RSI 未過熱）`, pass: rsi != null && rsi < 55 },
          { label: `RSI ${rsi != null ? rsi.toFixed(1) : '?'} < 50（做空動能偏弱）`, pass: rsi != null && rsi < 50 },
        ].map(({ label, pass }) => (
          <div key={label} style={{ fontSize: 11, color: pass ? '#2a8' : 'var(--muted)', marginBottom: 2 }}>
            {pass ? '✓' : '✗'} {label}
          </div>
        ))}
      </div>
    </div>
  )
}

function GaugePlaceholder({ label }) {
  return <div style={{ color: 'var(--muted)', fontSize: 12, textAlign: 'center', padding: 16 }}>{label} 暫無數據</div>
}

/* ── 主元件 ──────────────────────────────────────────── */
export default function TechPanel({ lastDecision, price, inPos, direction, entryPrice, sl, tp }) {
  if (!lastDecision) return null
  const ind = lastDecision.ind || {}

  const fib_pos  = ind.fib_pos  != null ? Number(ind.fib_pos)  : null
  const fib_382  = ind.fib_382  != null ? Number(ind.fib_382)  : null
  const fib_618  = ind.fib_618  != null ? Number(ind.fib_618)  : null
  const rsi      = ind.rsi      != null ? Number(ind.rsi)      : null
  const atr      = ind.atr      != null ? Number(ind.atr)      : null
  const emaTrend = ind.ema_trend != null ? Number(ind.ema_trend): null
  const er       = ind.er       != null ? Number(ind.er)       : null
  const chop     = ind.chop     != null ? Number(ind.chop)     : null
  const adx      = ind.adx      != null ? Number(ind.adx)      : null

  const card = (title, children) => (
    <div style={{ background: 'var(--panel2)', border: '1px solid var(--border)',
      borderRadius: 10, padding: '12px 16px', flex: 1, minWidth: 180 }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10, fontWeight: 600,
        textTransform: 'uppercase', letterSpacing: 1 }}>{title}</div>
      {children}
    </div>
  )

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0, marginBottom: 12 }}>技術分析訊號視覺化</h3>

      {/* 訊號總結 */}
      <div style={{ background: 'var(--panel2)', border: '1px solid var(--border)',
        borderRadius: 10, padding: '12px 16px', marginBottom: 12 }}>
        <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 8, fontWeight: 600,
          textTransform: 'uppercase', letterSpacing: 1 }}>訊號總結</div>
        <SignalSummary ind={ind} target={lastDecision.target} direction={direction} />
      </div>

      {/* 指標卡片列 */}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        {card('Regime 市場狀態',
          <RegimerVote er={er} chop={chop} adx={adx} regime={ind.regime} />
        )}
        {card('RSI 相對強弱',
          <RsiGauge rsi={rsi} />
        )}
        {card('EMA 200 趨勢',
          <EmaTrend price={price} emaTrend={emaTrend} />
        )}
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
        {card('Fibonacci 位置',
          <FibBar fibPos={fib_pos} fib382={fib_382} fib618={fib_618} price={price} />
        )}
        {card('ATR 波動度 & 風險',
          <AtrPanel atr={atr} price={price} sl={sl} tp={tp}
            entryPrice={entryPrice} inPos={inPos} direction={direction} />
        )}
      </div>
    </div>
  )
}
