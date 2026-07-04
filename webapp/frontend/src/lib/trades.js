// 交易紀錄純邏輯（可單元測試，與 React 解耦）。
// 後端 recent_trades 每列：{ ts, side, price, qty, pnl }
//   side ∈ entry | entry_short | scale_out | exit_signal | exit_sltp

/** 把成交列配對成「回合」列（最新在前）。
 *  - entry/entry_short：開倉，記住成本與剩餘量
 *  - scale_out：部分了結 → 自成一列（kind:'scale'），開倉仍續、剩餘量遞減
 *  - exit_*：平倉 → 自成一列（kind:'exit'），exit_type 標明停損停利/訊號
 *  - 收尾仍持倉 → open 列（kind:'open'），qty = 剩餘量
 *  - 孤立 exit（entry 在視窗外）→ orphan 列
 */
export function pairTrades(trades = []) {
  const ordered = [...trades].reverse()   // 轉時間正序
  const rows = []
  let entry = null
  let remaining = 0

  const dirOf = (e, pnl) =>
    e ? (e.side === 'entry' ? 'long' : 'short') : (pnl > 0 ? 'long' : 'short')

  for (const t of ordered) {
    if (t.side === 'entry' || t.side === 'entry_short') {
      // 連續兩筆 entry 中間沒有 exit（資料缺漏，多因重啟時交易所已平倉但漏記）：
      // 把前一筆未平倉 entry 顯示成「紀錄缺漏」列，不讓它被覆蓋後靜默消失。
      if (entry) {
        rows.push({
          kind: 'exit',
          dir: entry.side === 'entry' ? 'long' : 'short',
          entry_price: entry.price,
          exit_price: null,
          qty: entry.qty,
          pnl: null,
          ts: entry.ts,
          entry_ts: entry.ts,
          pos_value: Math.round(entry.qty * entry.price),
          exit_type: 'gap',
          orphan: true,
          gap: true,
        })
      }
      entry = t
      remaining = t.qty
    } else if (t.side === 'scale_out') {
      rows.push({
        kind: 'scale',
        dir: dirOf(entry, t.pnl),
        entry_price: entry ? entry.price : null,
        exit_price: t.price,
        qty: t.qty,
        pnl: t.pnl,
        ts: t.ts,
        entry_ts: entry ? entry.ts : null,
        pos_value: Math.round(t.qty * t.price),
        exit_type: 'scale',
      })
      remaining = Math.max(0, remaining - t.qty)
    } else if (t.side && t.side.startsWith('exit')) {
      rows.push({
        kind: 'exit',
        dir: dirOf(entry, t.pnl),
        entry_price: entry ? entry.price : null,
        exit_price: t.price,
        qty: t.qty,
        pnl: t.pnl,
        ts: t.ts,
        entry_ts: entry ? entry.ts : null,
        pos_value: Math.round((entry ? entry.qty * entry.price : t.qty * t.price)),
        // 取 exit_ 後綴作為細分原因：tp/sl/trail/breakeven/sltp(舊)/signal
        exit_type: t.side.startsWith('exit_') ? t.side.slice(5) : t.side,
        orphan: !entry,
      })
      entry = null
      remaining = 0
    }
  }

  if (entry) {
    rows.push({
      kind: 'open',
      dir: entry.side === 'entry' ? 'long' : 'short',
      entry_price: entry.price,
      exit_price: null,
      qty: remaining || entry.qty,
      orig_qty: entry.qty,
      pnl: null,
      ts: entry.ts,
      entry_ts: entry.ts,
      pos_value: Math.round(entry.qty * entry.price),
      open: true,
    })
  }
  return rows.reverse()   // 最新在前
}

/** 視窗內逐筆累積餘額（最舊→最新加 pnl），回傳與 rows 同序（最新在前）。
 *  scale_out 與 exit 的 pnl 都計入；open 列無 pnl → 該位置回傳 null。 */
export function calcBalances(rows, initCapital = 5000) {
  const ordered = [...rows].reverse()
  let bal = initCapital
  const bals = []
  for (const r of ordered) {
    if (r.pnl != null) bal += r.pnl
    bals.push(r.pnl != null ? bal : null)
  }
  return bals.reverse()
}

/** 連續同方向虧損偵測（只看平倉列 kind:'exit'，scale/open 跳過）。
 *  從最新往回數：最近一筆平倉若是贏 → null；否則數同方向連虧筆數。
 *  回傳 { dir, count, totalPnl } 或 null。 */
export function lossStreak(rows = []) {
  const exits = rows.filter(r => r.kind === 'exit' && r.pnl != null)
  if (!exits.length || exits[0].pnl >= 0) return null
  const dir = exits[0].dir
  let count = 0
  let totalPnl = 0
  for (const e of exits) {
    if (e.pnl < 0 && e.dir === dir) {
      count += 1
      totalPnl += e.pnl
    } else {
      break
    }
  }
  return { dir, count, totalPnl: Math.round(totalPnl * 100) / 100 }
}

/** 結單原因 → { label 短標籤, desc 詳細說明, tone pos/neg/flat }。
 *  細分原因（後端細分後）：tp/trail/breakeven/sl/scale；
 *  舊紀錄後備：sltp（依損益推斷）、signal（依損益分獲利/反轉）。 */
export function exitReason(exitType, pnl) {
  const p = pnl ?? 0
  switch (exitType) {
    case 'scale':
      return { label: '部分了結', tone: 'pos',
               desc: '浮盈達 0.5R：先平一半倉落袋，剩餘半倉停損移到進場成本（保本）' }
    case 'tp':
      return { label: '停利目標', tone: 'pos',
               desc: '價格觸及固定停利價（TP，約 2R）→ 達標出場' }
    case 'trail':
      return { label: '移動停利', tone: 'pos',
               desc: '吊燈追蹤停損：價格從最佳點回檔、觸及已上移到成本之上的停損 → 鎖住獲利出場' }
    case 'breakeven':
      return { label: '保本出場', tone: 'flat',
               desc: '部分了結後停損已移到成本，價格回到成本價附近 → 不賺不賠出場' }
    case 'sl':
      return { label: '停損', tone: 'neg',
               desc: '價格跌破成本、觸及停損價（SL）→ 認賠出場' }
    case 'gap':
      return { label: '紀錄缺漏', tone: 'flat',
               desc: '此筆進場後直接出現新的進場、中間沒有平倉紀錄（多因重啟時交易所端已平倉但來不及補記）。該回合損益無法得知，已從勝率統計排除。' }
    case 'manual':
      return p > 0 ? { label: '手動平倉', tone: 'pos', desc: '你在儀表板按「手動平倉」結算，獲利出場' }
           : p < 0 ? { label: '手動平倉', tone: 'neg', desc: '你在儀表板按「手動平倉」結算，認賠出場' }
           :         { label: '手動平倉', tone: 'flat', desc: '你在儀表板按「手動平倉」結算' }
    case 'sltp':                                  // 舊紀錄未細分 → 用損益推斷
      return p > 0 ? { label: '停利/移停', tone: 'pos', desc: '觸及停利或移動停損，獲利出場（舊紀錄未細分停利目標/吊燈）' }
           : p < 0 ? { label: '停損',      tone: 'neg', desc: '觸及停損價認賠出場（舊紀錄）' }
           :         { label: '保本',      tone: 'flat', desc: '約略打平出場（舊紀錄）' }
    case 'signal':
      return p > 0 ? { label: '訊號獲利', tone: 'pos', desc: '策略訊號反轉、獲利了結（reversion 多為觸及通道對側／回歸目標達成）' }
           : p < 0 ? { label: '訊號轉向', tone: 'neg', desc: '策略訊號反轉、順勢換邊出場（趨勢或動能翻轉）' }
           :         { label: '訊號平倉', tone: 'flat', desc: '策略訊號改變 → 平倉' }
    default:
      return { label: exitType || '平倉', tone: 'flat', desc: '平倉' }
  }
}

/** 報酬率 % = pnl / 倉位名目 × 100；無效輸入回傳 null。 */
export function roiPct(pnl, posValue) {
  if (pnl == null || !posValue) return null
  return (pnl / posValue) * 100
}

/** 進出場時間差 → 易讀字串（45m / 2h30m / 1d1h）。 */
export function holdDuration(entryTs, exitTs) {
  if (!entryTs || !exitTs) return '—'
  const a = new Date(String(entryTs).replace(' ', 'T') + 'Z').getTime()
  const b = new Date(String(exitTs).replace(' ', 'T') + 'Z').getTime()
  if (isNaN(a) || isNaN(b) || b < a) return '—'
  const mins = Math.round((b - a) / 60000)
  const d = Math.floor(mins / 1440)
  const h = Math.floor((mins % 1440) / 60)
  const m = mins % 60
  if (d > 0) return `${d}d${h}h`
  if (h > 0) return `${h}h${m}m`
  return `${m}m`
}
