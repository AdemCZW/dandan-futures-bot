import { useState } from 'react'
import Live from './components/Live.jsx'
import Backtest from './components/Backtest.jsx'
import Explain from './components/Explain.jsx'
import Optimize from './components/Optimize.jsx'
import Journal from './components/Journal.jsx'
import Whales from './components/Whales.jsx'
import Chart from './components/Chart.jsx'
import CopyTrading from './components/CopyTrading.jsx'
import { useTheme, toggleTheme } from './lib/theme.js'

const TABS = [
  { key: 'live',        label: '即時監控',   el: <Live /> },
  { key: 'chart',       label: 'K 線圖表',   el: <Chart /> },
  { key: 'whales',      label: '大戶籌碼',   el: <Whales /> },
  { key: 'copytrading', label: '帶單追蹤',   el: <CopyTrading /> },
  { key: 'backtest',    label: '回測',        el: <Backtest /> },
  { key: 'explain',  label: '決策流程',   el: <Explain /> },
  { key: 'optimize', label: '參數最佳化', el: <Optimize /> },
  { key: 'journal',  label: '交易日誌',   el: <Journal /> },
]

export default function App() {
  const [tab, setTab] = useState('live')
  const theme = useTheme()
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
      {TABS.map((t) => (
        <div
          key={t.key}
          role="tabpanel"
          aria-hidden={tab !== t.key}
          style={{ display: tab === t.key ? 'block' : 'none' }}
        >
          {t.el}
        </div>
      ))}
    </div>
  )
}
