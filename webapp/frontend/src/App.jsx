// 2026-06-30 UI 精簡：移除頂部標題區（<header>）含以下元素
//   · <h1>丹丹交易團隊 — 儀表板</h1>
//   · <span class="display">Dandan Trading Terminal</span>
//   · <span class="badge badge-system">合約測試網</span>
//   · <div class="sub">幣安測試網模擬盤 · 虛擬資金 · 非投資建議</div>
//   主題切換鈕保留，移入 tab 列右端。
import { useState } from 'react'
import Live from './components/Live.jsx'
import Backtest from './components/Backtest.jsx'
import Explain from './components/Explain.jsx'
import Optimize from './components/Optimize.jsx'
import Journal from './components/Journal.jsx'
import Chart from './components/Chart.jsx'
import LiveDecisions from './components/LiveDecisions.jsx'
import { useTheme, toggleTheme } from './lib/theme.js'

// GitHub Pages 建置（VITE_PUBLIC_BUILD=true）只留輕量分頁：bot 容器直連就能供應，
// 不需要 dashboard 常駐。回測/參數最佳化要靠 dashboard 的 vectorbt/optuna 肥依賴，
// 線上沒有這個後端可打，故隱藏（元件仍打包但不掛載，不影響 bundle 正確性，只是沒被渲染）。
const PUBLIC_BUILD = import.meta.env.VITE_PUBLIC_BUILD === 'true'

const ALL_TABS = [
  { key: 'live', label: '即時監控', Comp: Live, public: true },
  { key: 'chart', label: 'K 線圖表', Comp: Chart, public: true },
  { key: 'backtest', label: '回測', Comp: Backtest, public: false },
  { key: 'explain', label: '決策流程', Comp: PUBLIC_BUILD ? LiveDecisions : Explain, public: true },
  { key: 'optimize', label: '參數最佳化', Comp: Optimize, public: false },
  { key: 'journal', label: '交易日誌', Comp: Journal, public: true },
]

const TABS = PUBLIC_BUILD ? ALL_TABS.filter((t) => t.public) : ALL_TABS

export default function App() {
  const [tab, setTab] = useState('live')
  const theme = useTheme()
  const active = TABS.find((t) => t.key === tab) || TABS[0]
  const ActiveComp = active.Comp
  return (
    <div className="wrap">
      <nav className="tabs" role="tablist" aria-label="儀表板分頁">
        {TABS.map((t, i) => {
          const active = tab === t.key
          return (
            <button
              key={t.key}
              role="tab"
              aria-selected={active}
              className={`tab ${active ? 'active' : ''}`}
              onClick={() => setTab(t.key)}
            >
              <span
                className="num"
                aria-hidden="true"
                style={{
                  fontSize: 10,
                  marginRight: 7,
                  color: active ? 'var(--accent)' : 'var(--muted-dim)',
                  verticalAlign: '1px',
                }}
              >
                {String(i + 1).padStart(2, '0')}
              </span>
              {t.label}
            </button>
          )
        })}
        <button
          className="theme-toggle"
          onClick={toggleTheme}
          aria-label={theme === 'light' ? '切換深色主題' : '切換亮色主題'}
          title={theme === 'light' ? '切換深色主題' : '切換亮色主題'}
          style={{ marginLeft: 'auto' }}
        >
          {theme === 'light' ? '◑ 深色' : '◐ 亮色'}
        </button>
      </nav>
      <div key={active.key} role="tabpanel" aria-hidden="false">
        <ActiveComp />
      </div>
    </div>
  )
}
