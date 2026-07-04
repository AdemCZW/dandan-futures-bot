import { describe, it, expect } from 'vitest'
import { pairTrades, calcBalances, lossStreak, roiPct, holdDuration, exitReason } from './trades'

// 後端 recent_trades 列的形狀：{ ts, side, price, qty, pnl }
// side ∈ entry / entry_short / scale_out / exit_signal / exit_sltp
const T = (ts, side, price, qty, pnl = 0) => ({ ts, side, price, qty, pnl })
// 後端 recent_trades 實際是「最新在前」（時間遞減）；fixtures 以時間正序書寫
// 再用 feed() 反轉成後端的真實順序餵給 pairTrades。
const feed = (chrono) => [...chrono].reverse()

describe('pairTrades — 把成交列配對成回合（最新在前）', () => {
  it('一進一出 = 一列完整回合', () => {
    const rows = pairTrades(feed([
      T('2026-06-25 05:00:00', 'entry_short', 69.0, 13.0),
      T('2026-06-25 05:15:00', 'exit_signal', 68.98, 13.0, 0.26),
    ])    )
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      kind: 'exit', dir: 'short', entry_price: 69.0, exit_price: 68.98, pnl: 0.26,
      exit_type: 'signal', entry_ts: '2026-06-25 05:00:00',
    })
  })

  it('scale_out 必須是獨立可見的一列（這是 Bot2 +96 對不上的根因）', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 13:00:00', 'entry', 68.94, 13.1),
      T('2026-06-26 13:30:00', 'scale_out', 69.54, 6.5, 3.92),
      T('2026-06-26 13:45:00', 'exit_sltp', 70.74, 6.5, 11.75),
    ])    )
    // 一筆 entry → 一筆部分了結 + 一筆剩餘平倉 = 2 列可見
    expect(rows).toHaveLength(2)
    const kinds = rows.map(r => r.kind)
    expect(kinds).toContain('scale')
    expect(kinds).toContain('exit')
    const scale = rows.find(r => r.kind === 'scale')
    expect(scale).toMatchObject({ exit_type: 'scale', pnl: 3.92, dir: 'long' })
  })

  it('細分結單原因 → exit_type 取 exit_ 後綴（tp/sl/trail/breakeven）', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 10:00:00', 'entry', 100.0, 1.0),
      T('2026-06-26 10:30:00', 'exit_tp', 104.0, 1.0, 4.0),
    ])    )
    expect(rows[0].exit_type).toBe('tp')
  })

  it('未平倉的開倉 = open 列，qty 為剩餘量', () => {
    const rows = pairTrades(feed([
      T('2026-06-27 13:15:00', 'entry_short', 72.48, 41.4),
      T('2026-06-27 13:45:00', 'scale_out', 72.24, 20.7, 4.97),
    ])    )
    // 部分了結後仍持倉 → 應有 scale 列 + open 列
    const open = rows.find(r => r.kind === 'open')
    expect(open).toBeTruthy()
    expect(open.dir).toBe('short')
    expect(open.qty).toBeCloseTo(20.7, 1)   // 41.4 - 20.7 剩餘
    expect(open.orig_qty).toBeCloseTo(41.4, 1)   // 原始開倉量（供「已部分了結」判斷）
  })

  it('孤立 exit（entry 超出視窗）仍顯示', () => {
    const rows = pairTrades(feed([
      T('2026-06-25 05:15:00', 'exit_signal', 68.98, 13.0, 0.26),
    ])    )
    expect(rows).toHaveLength(1)
    expect(rows[0].orphan).toBe(true)
    expect(rows[0].entry_price).toBeNull()
  })

  it('最新在前', () => {
    const rows = pairTrades(feed([
      T('2026-06-25 05:00:00', 'entry', 69.0, 13.0),
      T('2026-06-25 05:15:00', 'exit_signal', 69.5, 13.0, 6.5),
      T('2026-06-25 06:00:00', 'entry', 70.0, 13.0),
      T('2026-06-25 06:15:00', 'exit_signal', 69.5, 13.0, -6.5),
    ])    )
    expect(rows[0].ts).toBe('2026-06-25 06:15:00')
  })

  it('連續兩筆 entry 無 exit → 舊那筆顯示為「紀錄缺漏」列，不靜默消失', () => {
    const rows = pairTrades(feed([
      T('2026-06-30 07:00:00', 'entry', 74.12, 20.2),
      T('2026-06-30 07:00:00', 'entry', 73.96, 20.28),
    ])    )
    // 舊 entry(74.12) 應成為 gap 列；新 entry(73.96) 為 open 列
    expect(rows).toHaveLength(2)
    const gap = rows.find(r => r.exit_type === 'gap')
    expect(gap).toBeTruthy()
    expect(gap.entry_price).toBe(74.12)
    expect(gap.exit_price).toBeNull()
    expect(gap.pnl).toBeNull()
    expect(gap.gap).toBe(true)
    const open = rows.find(r => r.kind === 'open')
    expect(open.entry_price).toBe(73.96)
  })

  it('gap 列不汙染連續虧損統計（lossStreak 只看真平倉）', () => {
    const rows = pairTrades(feed([
      T('2026-06-30 07:00:00', 'entry', 74.12, 20.2),
      T('2026-06-30 07:00:00', 'entry', 73.96, 20.28),
    ])    )
    expect(lossStreak(rows)).toBeNull()   // 無真平倉 → 不觸發警示
  })
})

describe('calcBalances — 視窗內逐筆累積餘額', () => {
  it('scale_out 與 exit 的損益都要計入餘額', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 13:00:00', 'entry', 68.94, 13.1),
      T('2026-06-26 13:30:00', 'scale_out', 69.54, 6.5, 3.92),
      T('2026-06-26 13:45:00', 'exit_sltp', 70.74, 6.5, 11.75),
    ])    )
    const bals = calcBalances(rows, 5000)   // 最新在前
    // 最新一筆（exit）後餘額 = 5000 + 3.92 + 11.75
    expect(bals[0]).toBeCloseTo(5000 + 3.92 + 11.75, 2)
  })
})

describe('lossStreak — 連續同方向虧損偵測（Bot2 通道方向警示）', () => {
  it('連三筆做空虧損 → {dir:short, count:3}', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 05:30:00', 'entry_short', 68.0, 13),
      T('2026-06-26 05:45:00', 'exit_signal', 68.5, 13, -1.85),
      T('2026-06-26 06:00:00', 'entry_short', 68.5, 13),
      T('2026-06-26 06:15:00', 'exit_sltp', 69.6, 13, -14.27),
      T('2026-06-26 06:15:00', 'entry_short', 69.6, 13),
      T('2026-06-26 06:30:00', 'exit_sltp', 70.1, 13, -7.13),
    ])    )
    const s = lossStreak(rows)
    expect(s).toMatchObject({ dir: 'short', count: 3 })
  })

  it('被一筆贏單打斷 → 計數歸零（只算最近連續）', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 05:30:00', 'entry_short', 68.0, 13),
      T('2026-06-26 05:45:00', 'exit_signal', 68.5, 13, -1.85),
      T('2026-06-26 06:00:00', 'entry_short', 68.5, 13),
      T('2026-06-26 06:15:00', 'exit_signal', 68.0, 13, 6.5),   // 贏
      T('2026-06-26 06:30:00', 'entry_short', 68.0, 13),
      T('2026-06-26 06:45:00', 'exit_sltp', 68.5, 13, -2.0),    // 最近一筆虧
    ])    )
    const s = lossStreak(rows)
    expect(s.count).toBe(1)
  })

  it('方向改變 → 重新計數', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 05:00:00', 'entry', 68.0, 13),
      T('2026-06-26 05:15:00', 'exit_sltp', 67.5, 13, -6.5),    // 多虧
      T('2026-06-26 05:30:00', 'entry_short', 67.5, 13),
      T('2026-06-26 05:45:00', 'exit_sltp', 68.0, 13, -6.5),    // 空虧（不同向）
    ])    )
    const s = lossStreak(rows)
    expect(s).toMatchObject({ dir: 'short', count: 1 })
  })

  it('最近一筆是贏 → null（無警示）', () => {
    const rows = pairTrades(feed([
      T('2026-06-26 05:00:00', 'entry', 68.0, 13),
      T('2026-06-26 05:15:00', 'exit_sltp', 68.5, 13, 6.5),
    ])    )
    expect(lossStreak(rows)).toBeNull()
  })
})

describe('roiPct / holdDuration', () => {
  it('roiPct = pnl / 倉位名目 × 100', () => {
    expect(roiPct(11.75, 460)).toBeCloseTo(2.554, 2)
    expect(roiPct(5, 0)).toBeNull()
    expect(roiPct(null, 100)).toBeNull()
  })

  it('holdDuration 把進出場時間差轉成易讀字串', () => {
    expect(holdDuration('2026-06-26 13:00:00', '2026-06-26 13:45:00')).toBe('45m')
    expect(holdDuration('2026-06-26 13:00:00', '2026-06-26 15:30:00')).toBe('2h30m')
    expect(holdDuration('2026-06-26 13:00:00', '2026-06-27 14:00:00')).toBe('1d1h')
    expect(holdDuration(null, '2026-06-26 13:45:00')).toBe('—')
  })
})

describe('exitReason — 結單原因細分標籤 + 說明', () => {
  it('細分原因各有專屬標籤與語氣', () => {
    expect(exitReason('tp', 4).label).toBe('停利目標')
    expect(exitReason('tp', 4).tone).toBe('pos')
    expect(exitReason('trail', 2).label).toBe('移動停利')
    expect(exitReason('breakeven', 0).label).toBe('保本出場')
    expect(exitReason('breakeven', 0).tone).toBe('flat')
    expect(exitReason('sl', -5).label).toBe('停損')
    expect(exitReason('sl', -5).tone).toBe('neg')
    expect(exitReason('scale', 3).label).toBe('部分了結')
    expect(exitReason('manual', 3).label).toBe('手動平倉')
    expect(exitReason('manual', 3).tone).toBe('pos')
    expect(exitReason('manual', -3).tone).toBe('neg')
    expect(exitReason('manual', 0).tone).toBe('flat')
  })

  it('每個原因都附帶非空說明文字', () => {
    for (const t of ['tp', 'trail', 'breakeven', 'sl', 'scale', 'signal', 'sltp']) {
      expect(exitReason(t, 1).desc.length).toBeGreaterThan(0)
    }
  })

  it('舊紀錄 exit_sltp 依損益推斷（未細分時的後備）', () => {
    expect(exitReason('sltp', 10).tone).toBe('pos')   // 獲利 → 停利/移停
    expect(exitReason('sltp', -10).tone).toBe('neg')  // 虧損 → 停損
  })

  it('訊號平倉依損益分獲利/反轉', () => {
    expect(exitReason('signal', 8).tone).toBe('pos')
    expect(exitReason('signal', -8).tone).toBe('neg')
  })
})
