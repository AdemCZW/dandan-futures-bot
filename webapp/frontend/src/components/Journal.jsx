import { useEffect, useState } from 'react'
import { api, cls } from '../api'
import { Plain } from './Hint'

const MODES = ['paper', 'backtest', 'live_testnet', 'live_testnet_ws', 'live_futures_testnet']

// 把 UTC 時間字串轉成台灣時間（UTC+8）
function toTaipei(ts) {
  if (!ts) return '—'
  const d = new Date(String(ts).replace(' ', 'T') + 'Z')
  if (isNaN(d)) return String(ts).slice(0, 16)
  const tw = new Date(d.getTime() + 8 * 3600 * 1000)
  const p = (n) => String(n).padStart(2, '0')
  return `${tw.getUTCFullYear()}/${p(tw.getUTCMonth() + 1)}/${p(tw.getUTCDate())} ${p(tw.getUTCHours())}:${p(tw.getUTCMinutes())}`
}

// 動作標籤：保留原文字（含中文雙重編碼），並映射到統一徽章語意色
// badge：做多/獲利→long(綠)、做空/虧損→short(紅)、中性出場→flat(青)
function labelSide(side) {
  const map = {
    entry:        { text: '進場做多',          color: 'pos', badge: 'badge-long'  },
    entry_short:  { text: '進場做空',          color: 'neg', badge: 'badge-short' },
    exit_signal:    { text: '出場　訊號反轉',    color: '',    badge: 'badge-flat'   },
    exit_sltp:      { text: '出場　停損 / 停利', color: '',    badge: 'badge-flat'   },
    exit_sl:        { text: '出場　停損',        color: 'neg', badge: 'badge-short'  },
    exit_tp:        { text: '出場　停利目標',    color: 'pos', badge: 'badge-long'   },
    exit_trail:     { text: '出場　移動停利',    color: 'pos', badge: 'badge-long'   },
    exit_breakeven: { text: '出場　保本',        color: '',    badge: 'badge-flat'   },
    scale_out:      { text: '部分了結',          color: 'pos', badge: 'badge-system' },
    exit_manual:    { text: '出場　手動平倉',    color: '',    badge: 'badge-system' },
  }
  return map[side] ?? { text: side, color: '', badge: 'badge-flat' }
}

function labelMode(mode) {
  const map = {
    live_futures_testnet: '合約測試網',
    paper:                'Paper 模擬',
    live_testnet:         '現貨測試網',
    live_testnet_ws:      '現貨測試網 WS',
    backtest:             '回測',
  }
  return map[mode] ?? mode
}

function labelStrategy(s) {
  const map = {
    fib_retracement: 'Fibonacci',
    ema_cross:       'EMA 交叉',
    zscore_ls:       'Z-Score 多空',
    zscore_revert:   'Z-Score 回歸',
  }
  return map[s] ?? s
}

export default function Journal() {
  const [rows, setRows] = useState([])
  const [mode, setMode] = useState('')
  const [err, setErr] = useState('')

  async function load() {
    setErr('')
    // 首選：bot 容器直連（逐台 /trades 再合併，不需要 dashboard 常駐）
    try {
      const list = await api.bots()
      if (Array.isArray(list) && list.length) {
        const perBot = await Promise.all(
          list.map((b) => api.botTrades(b.id, 100).catch(() => []))
        )
        const merged = perBot.flat()
          .filter((t) => !mode || t.mode === mode)
          .sort((a, b) => String(b.ts).localeCompare(String(a.ts)))
        setRows(merged)
        return
      }
    } catch { /* fallback ↓ */ }
    // 舊版 fallback：dashboard /api/trades（共用 Postgres 直查）
    try { setRows(await api.trades(100, mode || undefined)) }
    catch (e) { setErr(String(e.message || e)) }
  }
  useEffect(() => { load() }, [mode])   // eslint-disable-line react-hooks/exhaustive-deps

  const exits = rows.filter(r => !String(r.side).startsWith('entry'))
  const wins = exits.filter(r => Number(r.pnl) > 0).length
  const totalPnl = exits.reduce((s, r) => s + (Number(r.pnl) || 0), 0)
  const winRate = exits.length > 0 ? (wins / exits.length * 100) : 0

  return (
    <div className="panel">
      <h3>交易日誌 · JOURNAL</h3>
      <Plain>
        四台 bot 每一筆「進場 / 部分了結 / 出場」都記在這。<b>動作說明</b>欄會標明這筆是怎麼結束的
        （停利目標 / 移動停利 / 保本 / 停損 / 訊號反轉 / 手動平倉）；<b>損益</b>欄是該筆實際賺賠（USDT），
        進場列不算損益顯示「—」。上方三格是<b>已出場筆數 / 勝率 / 總損益</b>的合計。
      </Plain>

      {/* 控制列：來源篩選 + 重新整理（次要動作走 ghost 描邊） */}
      <div className="controls" style={{ marginBottom: 12, flexWrap: 'wrap', gap: 12 }}>
        <div className="field">
          <label>來源篩選</label>
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="">全部</option>
            {MODES.map((m) => <option key={m} value={m}>{labelMode(m)}</option>)}
          </select>
        </div>
        <button className="btn-ghost" onClick={load} style={{ alignSelf: 'flex-end' }}>
          重新整理
        </button>
      </div>

      {/* 摘要讀數列：等寬數值右對齊、損益語意著色 */}
      {exits.length > 0 && (
        <div className="cards" style={{ marginBottom: 12 }}>
          <div className="card">
            <div className="k">已出場筆數</div>
            <div className="v num">{exits.length}</div>
          </div>
          <div className="card">
            <div className="k">勝率</div>
            <div className="v num">{winRate.toFixed(0)}<span style={{ fontSize: 14, color: 'var(--muted)' }}>%</span></div>
          </div>
          <div className={`card ${totalPnl >= 0 ? 'is-long' : 'is-short'}`}>
            <div className="k">總損益 · USDT</div>
            <div className={`v num signal-glow ${cls(totalPnl)}`}>
              {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)}
            </div>
          </div>
        </div>
      )}

      {err && <div className="err">⚠ {err}</div>}

      <div className="muted" style={{ marginBottom: 8, fontSize: 12 }}>
        共 <span className="num">{rows.length}</span> 筆 · 時間為台灣時間（UTC+8）· 最新交易在最上方
      </div>

      <table>
        <thead>
          <tr>
            <th>時間（台灣）</th>
            <th>來源</th>
            <th>策略</th>
            <th>動作說明</th>
            <th style={{ textAlign: 'right' }}>成交價</th>
            <th style={{ textAlign: 'right' }}>數量（BTC）</th>
            <th style={{ textAlign: 'right' }}>損益（USDT）</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => {
            const s = labelSide(t.side)
            const isEntry = String(t.side).startsWith('entry')
            const pnl = Number(t.pnl)
            return (
              <tr key={i}>
                <td className="num" style={{ whiteSpace: 'nowrap' }}>
                  {toTaipei(t.ts)}
                </td>
                <td>{labelMode(t.mode)}</td>
                <td>{labelStrategy(t.strategy)}</td>
                <td>
                  <span className={`badge ${s.badge}`}>{s.text}</span>
                </td>
                <td className="num" style={{ textAlign: 'right' }}>
                  {Number(t.price).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </td>
                <td className="num" style={{ textAlign: 'right' }}>
                  {Number(t.qty).toFixed(6)}
                </td>
                <td className={`num ${isEntry ? 'muted' : cls(pnl)}`} style={{ textAlign: 'right' }}>
                  {isEntry
                    ? '—'
                    : `${pnl >= 0 ? '+' : ''}${pnl.toFixed(4)}`}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={7} className="num" style={{ textAlign: 'center', padding: '24px 0', color: 'var(--faint)' }}>
                // 尚無交易留底（跑 run_live_futures.py 或 run_paper.py 後會自動出現）
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  )
}
