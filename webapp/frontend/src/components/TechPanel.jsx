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
  const color = v < 30 ? 'var(--pos)' : v > 70 ? 'var(--neg)' : 'var(--accent)'
  const label = v < 30 ? '超賣' : v > 70 ? '超買' : v < 50 ? '偏弱' : '偏強'
  return (
    <div style={{ textAlign: 'center' }}>
      <svg viewBox="0 0 120 72" width={130} height={80}>
        {arc(180, 0, 'var(--line)', 9)}
        {arc(180, 126, 'var(--pos)', 9)}      {/* 0–30 oversold */}
        {arc(126, 54, 'var(--line)', 9)}{/* 30–70 neutral */}
        {arc(54, 0, 'var(--neg)', 9)}          {/* 70–100 overbought */}
        <line x1={cx} y1={cy} x2={nx} y2={ny}
          stroke={color} strokeWidth={2.5} strokeLinecap="round" />
        <circle cx={cx} cy={cy} r={3} fill={color} />
        <text x={cx} y={cy + 12} textAnchor="middle" fontSize={13} fontWeight={600}
          fill={color} fontFamily="var(--font-mono)">
          {v.toFixed(1)}
        </text>
        <text x={10} y={70} fontSize={8} fill="var(--pos)">超賣</text>
        <text x={110} y={70} fontSize={8} fill="var(--neg)" textAnchor="end">超買</text>
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
  const dotColor    = inLongZone ? 'var(--pos)' : inShortZone ? 'var(--neg)' : 'var(--muted)'
  const signal      = inLongZone ? '在多單進場區' : inShortZone ? '在空單進場區' : '中性區'

  return (
    <div style={{ width: '100%' }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 8 }}>
        Fib 位置（0=低點 → 1=高點）
      </div>
      <div style={{ position: 'relative', height: 20, borderRadius: 'var(--radius)', overflow: 'hidden',
        background: 'var(--line)' }}>
        {/* long entry zone */}
        <div style={{ position: 'absolute', left: 0, width: `${zone382}%`, height: '100%',
          background: 'var(--pos-soft)' }} />
        {/* short entry zone */}
        <div style={{ position: 'absolute', left: `${zone618}%`, right: 0, height: '100%',
          background: 'var(--neg-soft)' }} />
        {/* exit lines */}
        <div style={{ position: 'absolute', left: `${exit45}%`, top: 0, width: 1, height: '100%',
          background: 'var(--line-strong)' }} />
        <div style={{ position: 'absolute', left: `${exit55}%`, top: 0, width: 1, height: '100%',
          background: 'var(--line-strong)' }} />
        {/* current marker */}
        <div style={{ position: 'absolute', left: `${Math.min(pct, 99)}%`, top: 0,
          width: 3, height: '100%', background: dotColor,
          transform: 'translateX(-50%)', borderRadius: 2 }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10,
        color: 'var(--muted)', marginTop: 4 }}>
        <span style={{ color: 'var(--pos)' }}>做多區 &lt;0.382</span>
        <span className="num">0.5</span>
        <span style={{ color: 'var(--neg)' }}>&gt;0.618 做空區</span>
      </div>
      {fib382 && fib618 && (
        <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
          支撐 <span className="num">{Number(fib382).toFixed(1)}</span> · 阻力 <span className="num">{Number(fib618).toFixed(1)}</span>
        </div>
      )}
      <div style={{ fontSize: 12, fontWeight: 600, color: dotColor, marginTop: 8 }}>
        {signal}（<span className="num">{Number(fibPos).toFixed(3)}</span>）
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
  const regimeLabel = regime === 'range' ? '盤整盤' : '趨勢盤'

  return (
    <div>
      <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        {votes.map(v => {
          if (v.value == null) return null
          const isR = v.rangeWhen(Number(v.value))
          const c = isR ? 'var(--accent)' : 'var(--neg)'
          return (
            <div key={v.label} style={{ flex: 1, background: 'var(--surface-2)',
              border: '1px solid var(--line)', borderRadius: 'var(--radius)',
              padding: '8px', textAlign: 'center' }}>
              <div style={{ fontSize: 10, color: 'var(--muted)', letterSpacing: '0.5px' }}>{v.label}</div>
              <div className="num" style={{ fontSize: 15, fontWeight: 600, color: c }}>{v.value}</div>
              <div style={{ fontSize: 9, color: c }}>{isR ? '盤整' : '趨勢'}</div>
            </div>
          )
        })}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>2/3 多數決 →</span>
        <span className={`badge ${isRange ? 'badge-flat' : 'badge-short'}`}>
          {regimeLabel}（{isRange ? `盤整 ${rangeVotes}` : `趨勢 ${trendVotes}`} 票）
        </span>
      </div>
      {!isRange && (
        <div style={{ fontSize: 11, color: 'var(--neg)', marginTop: 8 }}>
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
  const color = above ? 'var(--pos)' : 'var(--neg)'
  const arrow = above ? '↑' : '↓'
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 36, color, lineHeight: 1 }}>{arrow}</div>
      <div style={{ fontSize: 13, fontWeight: 600, color }}>
        {above ? '上升趨勢' : '下降趨勢'}
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
        現價 <span className="num">{p.toFixed(1)}</span> · EMA200 <span className="num">{e.toFixed(1)}</span>
      </div>
      <div className="num" style={{ fontSize: 13, color, fontWeight: 600, marginTop: 4 }}>
        {above ? '+' : ''}{pct}%
      </div>
      <div style={{ fontSize: 10, color: 'var(--muted)', marginTop: 4 }}>
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
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 12 }}>
        <div style={{ textAlign: 'center' }}>
          <div className="num" style={{ fontSize: 18, fontWeight: 600, color: 'var(--accent)' }}>{a.toFixed(1)}</div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>ATR (14)</div>
        </div>
        <div style={{ textAlign: 'center' }}>
          <div className="num" style={{ fontSize: 18, fontWeight: 600, color: 'var(--text-strong)' }}>{atrPct}%</div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>佔現價比</div>
        </div>
        <div style={{ textAlign: 'center' }}>
          <div className="num" style={{ fontSize: 18, fontWeight: 600, color: 'var(--pos)' }}>{rr.toFixed(1)}R</div>
          <div style={{ fontSize: 10, color: 'var(--muted)' }}>盈虧比</div>
        </div>
      </div>
      {inPos && entryPrice && (
        <>
          <div style={{ fontSize: 10, color: 'var(--muted)', marginBottom: 8 }}>
            持倉停損 / 停利距離
          </div>
          <div style={{ position: 'relative', height: 12, borderRadius: 'var(--radius)', overflow: 'visible',
            background: 'var(--line)', marginBottom: 4 }}>
            {/* SL zone */}
            <div style={{
              position: 'absolute',
              left: direction === 1 ? 0 : `${(tpDist / (slDist + tpDist)) * 100}%`,
              width: `${(slDist / (slDist + tpDist)) * 100}%`,
              height: '100%', background: 'var(--neg-soft)',
              borderRadius: direction === 1 ? 'var(--radius) 0 0 var(--radius)' : '0 var(--radius) var(--radius) 0',
            }} />
            {/* TP zone */}
            <div style={{
              position: 'absolute',
              left: direction === 1 ? `${(slDist / (slDist + tpDist)) * 100}%` : 0,
              width: `${(tpDist / (slDist + tpDist)) * 100}%`,
              height: '100%', background: 'var(--pos-soft)',
              borderRadius: direction === 1 ? '0 var(--radius) var(--radius) 0' : 'var(--radius) 0 0 var(--radius)',
            }} />
            {/* entry marker */}
            <div style={{ position: 'absolute',
              left: direction === 1 ? `${(slDist / (slDist + tpDist)) * 100}%` : `${(tpDist / (slDist + tpDist)) * 100}%`,
              top: -2, width: 3, height: 16, background: 'var(--text-strong)',
              transform: 'translateX(-50%)', borderRadius: 2 }} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10 }}>
            <span style={{ color: 'var(--neg)' }}>SL <span className="num">−{slDist.toFixed(1)}</span></span>
            <span style={{ color: 'var(--muted)' }}>進場</span>
            <span style={{ color: 'var(--pos)' }}>TP <span className="num">+{tpDist.toFixed(1)}</span></span>
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

  const longReady  = isRange && fibPos < 0.382 && rsi < 55
  const shortReady = isRange && fibPos > 0.618 && rsi < 50
  const sig = longReady ? 'LONG' : shortReady ? 'SHORT' : 'FLAT'
  const sigClass = sig === 'LONG' ? 'pos' : sig === 'SHORT' ? 'neg' : ''
  const sigColor = sig === 'FLAT' ? 'var(--muted)' : undefined

  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
      <div style={{ textAlign: 'center', minWidth: 80 }}>
        <div className={`display signal-glow ${sigClass}`}
          style={{ fontSize: 28, fontWeight: 600, letterSpacing: '1px',
            color: sigColor, ...(sig === 'FLAT' ? { textShadow: 'none' } : null) }}>
          {sig}
        </div>
        <div style={{ fontSize: 10, color: 'var(--muted)' }}>當前訊號</div>
      </div>
      <div style={{ flex: 1, minWidth: 220 }}>
        {[
          { label: 'Regime 盤整（盤整策略前提）', pass: isRange },
          { label: `Fib ${fibPos != null ? fibPos.toFixed(3) : '?'} < 0.382（做多支撐區）`, pass: fibPos != null && fibPos < 0.382 },
          { label: `Fib ${fibPos != null ? fibPos.toFixed(3) : '?'} > 0.618（做空阻力區）`, pass: fibPos != null && fibPos > 0.618 },
          { label: `RSI ${rsi != null ? rsi.toFixed(1) : '?'} < 55（做多 RSI 未過熱）`, pass: rsi != null && rsi < 55 },
          { label: `RSI ${rsi != null ? rsi.toFixed(1) : '?'} < 50（做空動能偏弱）`, pass: rsi != null && rsi < 50 },
        ].map(({ label, pass }) => (
          <div key={label} style={{ fontSize: 11, display: 'flex', alignItems: 'center', gap: 8,
            color: pass ? 'var(--pos)' : 'var(--muted)', marginBottom: 4 }}>
            <span style={{ width: 12, textAlign: 'center', color: pass ? 'var(--pos)' : 'var(--faint)' }}>
              {pass ? '✓' : '✗'}
            </span>
            {label}
          </div>
        ))}
      </div>
    </div>
  )
}

function GaugePlaceholder({ label }) {
  return <div style={{ color: 'var(--faint)', fontFamily: 'var(--font-mono)', fontSize: 12, textAlign: 'center', padding: 16 }}>// {label} 無資料</div>
}

/* ── Supertrend：趨勢方向 + 趨勢線 + 距翻轉距離 ───────── */
function SupertrendCard({ stDir, supertrend, price, atr }) {
  if (stDir == null || supertrend == null || price == null) return <GaugePlaceholder label="Supertrend" />
  const up = Number(stDir) > 0
  const line = Number(supertrend), p = Number(price)
  const gap = p - line                                   // 多頭時為正（價在線上）
  const atrUnits = atr ? Math.abs(gap) / Number(atr) : null
  const color = up ? 'var(--pos)' : 'var(--neg)'
  return (
    <div style={{ textAlign: 'center' }}>
      <div style={{ fontSize: 34, lineHeight: 1, color }}>{up ? '↑' : '↓'}</div>
      <div className="display" style={{ fontSize: 16, fontWeight: 700, color }}>
        {up ? '做多趨勢' : '做空趨勢'}
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
        翻轉價 <span className="num">{line.toFixed(1)}</span>
      </div>
      <div style={{ fontSize: 12, color, fontWeight: 600 }}>
        距翻轉 <span className="num">{atrUnits != null ? atrUnits.toFixed(2) : (Math.abs(gap)).toFixed(1)}</span>
        {atrUnits != null ? ' ATR' : ''}
      </div>
      <div style={{ fontSize: 10, color: 'var(--muted)' }}>
        {up ? '跌破翻轉價 → 轉空' : '突破翻轉價 → 轉多'}
      </div>
    </div>
  )
}

/* ── Donchian：價格在通道中的位置 + 距突破/出場 ──────── */
function DonchianCard({ upper, lower, exitLong, exitShort, price, inPos, direction }) {
  if (upper == null || lower == null || price == null) return <GaugePlaceholder label="Donchian" />
  const u = Number(upper), l = Number(lower), p = Number(price)
  const span = Math.max(u - l, 1e-9)
  const pct = Math.min(Math.max((p - l) / span, 0), 1) * 100
  return (
    <div style={{ width: '100%' }}>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 8 }}>
        價格於通道位置（下軌 → 上軌）
      </div>
      <div style={{ position: 'relative', height: 20, borderRadius: 'var(--radius)',
        background: 'var(--line)', overflow: 'hidden' }}>
        <div style={{ position: 'absolute', left: `${Math.min(pct, 99)}%`, top: 0,
          width: 3, height: '100%', background: 'var(--accent)', transform: 'translateX(-50%)' }} />
      </div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 10,
        color: 'var(--muted)', marginTop: 4 }}>
        <span style={{ color: 'var(--neg)' }}>跌破 <span className="num">{l.toFixed(0)}</span> 做空</span>
        <span style={{ color: 'var(--pos)' }}>突破 <span className="num">{u.toFixed(0)}</span> 做多</span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 8 }}>
        現價 <span className="num">{p.toFixed(1)}</span>
        {inPos && (direction === 1 ? exitLong != null : exitShort != null) && (
          <> · 出場線 <span className="num">{Number(direction === 1 ? exitLong : exitShort).toFixed(1)}</span></>
        )}
      </div>
    </div>
  )
}

/* ── 訂單流：主動買盤佔比 ────────────────────────────── */
function OrderFlowCard({ takerRatio }) {
  if (takerRatio == null) return <GaugePlaceholder label="訂單流" />
  const r = Number(takerRatio)
  const pct = Math.min(Math.max(r, 0), 1) * 100
  const buy = r >= 0.5
  const color = buy ? 'var(--pos)' : 'var(--neg)'
  return (
    <div style={{ width: '100%' }}>
      <div style={{ textAlign: 'center', marginBottom: 8 }}>
        <span className="display num" style={{ fontSize: 22, fontWeight: 700, color }}>
          {(r * 100).toFixed(0)}%
        </span>
        <div style={{ fontSize: 10, color: 'var(--muted)' }}>主動買盤佔比（平滑）</div>
      </div>
      <div style={{ position: 'relative', height: 14, borderRadius: 'var(--radius)',
        background: 'var(--neg-soft)', overflow: 'hidden' }}>
        <div style={{ position: 'absolute', left: 0, width: `${pct}%`, height: '100%',
          background: 'var(--pos-soft)' }} />
        <div style={{ position: 'absolute', left: '50%', top: 0, width: 1, height: '100%',
          background: 'var(--line-strong)' }} />
      </div>
      <div style={{ fontSize: 11, color, fontWeight: 600, textAlign: 'center', marginTop: 6 }}>
        {buy ? '買盤主導' : '賣盤主導'}
      </div>
    </div>
  )
}

/* ── 趨勢策略訊號總結（supertrend / donchian） ────────── */
function TrendSignalSummary({ stDir, dcBreak, target, inPos, direction }) {
  const sig = target === 1 ? 'LONG' : target === -1 ? 'SHORT' : 'FLAT'
  const sigColor = sig === 'LONG' ? 'var(--pos)' : sig === 'SHORT' ? 'var(--neg)' : 'var(--muted)'
  const posLabel = inPos ? (direction === 1 ? '持多' : '持空') : '空手'
  return (
    <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap' }}>
      <div style={{ textAlign: 'center', minWidth: 80 }}>
        <div className="display signal-glow" style={{
          fontSize: 28, fontWeight: 900, color: sigColor,
          ...(sig === 'FLAT' ? { textShadow: 'none' } : {}),
        }}>{sig}</div>
        <div style={{ fontSize: 10, color: 'var(--muted)' }}>本根目標</div>
      </div>
      <div style={{ flex: 1, fontSize: 12, color: 'var(--muted)', lineHeight: 1.9 }}>
        <div>目前部位：<b style={{ color: 'var(--text)' }}>{posLabel}</b></div>
        {stDir != null && <div>Supertrend 方向：{Number(stDir) > 0
          ? <b style={{ color: 'var(--pos)' }}>多</b> : <b style={{ color: 'var(--neg)' }}>空</b>}</div>}
        <div style={{ fontSize: 11 }}>趨勢策略順勢操作：跟隨方向翻轉進出，不逆勢接刀。</div>
      </div>
    </div>
  )
}

/* ── 區塊小標（精緻 mono uppercase + 細 accent tick） ──────── */
function SectionLabel({ children }) {
  return (
    <div style={{
      fontFamily: 'var(--font-mono)', fontSize: 12, fontWeight: 600,
      color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 'var(--track-label)',
      marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8,
    }}>
      <span style={{ display: 'inline-block', width: 2, height: 12, background: 'var(--accent)', borderRadius: 1 }} />
      {children}
    </div>
  )
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

  // 依本根出現的指標欄位，自動判別策略型態（趨勢 / 均值回歸）
  const hasST  = ind.st_dir != null
  const hasDC  = ind.dc_upper != null
  const hasFib = ind.fib_pos != null
  const hasOF  = ind.taker_ratio_s != null
  const isTrend = hasST || hasDC

  const card = (title, children) => (
    <div className="card" style={{ flex: 1, minWidth: 180, display: 'block' }}>
      <SectionLabel>{title}</SectionLabel>
      {children}
    </div>
  )

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0, marginBottom: 16 }}>技術分析訊號視覺化</h3>

      {/* 訊號總結（趨勢策略 / 均值回歸策略 各自呈現） */}
      <div className={`card ${inPos ? (direction === 1 ? 'is-long' : 'is-short') : ''}`}
        style={{ display: 'block', marginBottom: 12 }}>
        <SectionLabel>訊號總結</SectionLabel>
        {isTrend
          ? <TrendSignalSummary stDir={ind.st_dir} target={lastDecision.target}
              inPos={inPos} direction={direction} />
          : <SignalSummary ind={ind} target={lastDecision.target} direction={direction} />}
      </div>

      {/* 趨勢策略：Supertrend / Donchian 卡片 */}
      {isTrend && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
          {hasST && card('Supertrend 趨勢',
            <SupertrendCard stDir={ind.st_dir} supertrend={ind.supertrend} price={price} atr={atr} />
          )}
          {hasDC && card('Donchian 通道',
            <DonchianCard upper={ind.dc_upper} lower={ind.dc_lower}
              exitLong={ind.dc_exit_long} exitShort={ind.dc_exit_short}
              price={price} inPos={inPos} direction={direction} />
          )}
          {card('ATR 波動度 & 風險',
            <AtrPanel atr={atr} price={price} sl={sl} tp={tp}
              entryPrice={entryPrice} inPos={inPos} direction={direction} />
          )}
        </div>
      )}

      {/* 均值回歸策略（fib）：Regime / RSI / EMA / Fib 卡片 */}
      {hasFib && (
        <>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {card('Regime 市場狀態',
              <RegimerVote er={er} chop={chop} adx={adx} regime={ind.regime} />
            )}
            {card('RSI 相對強弱', <RsiGauge rsi={rsi} />)}
            {card('EMA 200 趨勢', <EmaTrend price={price} emaTrend={emaTrend} />)}
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
        </>
      )}

      {/* 訂單流（任何帶 taker_ratio_s 的策略都顯示） */}
      {hasOF && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 10 }}>
          {card('訂單流 主動買賣', <OrderFlowCard takerRatio={ind.taker_ratio_s} />)}
        </div>
      )}
    </div>
  )
}
