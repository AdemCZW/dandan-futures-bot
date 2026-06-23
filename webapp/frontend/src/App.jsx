import { useState } from 'react'
import Live from './components/Live.jsx'
import Backtest from './components/Backtest.jsx'
import Explain from './components/Explain.jsx'
import Optimize from './components/Optimize.jsx'
import Journal from './components/Journal.jsx'
import Whales from './components/Whales.jsx'
import Chart from './components/Chart.jsx'
import CopyTrading from './components/CopyTrading.jsx'

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
  return (
    <div className="wrap">
      <header className="hud-neon-top">
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          <h1>丹丹交易團隊 — 儀表板</h1>
          <span
            className="display"
            style={{
              fontSize: 12,
              fontWeight: 600,
              letterSpacing: '0.18em',
              color: 'var(--muted)',
              textTransform: 'uppercase',
            }}
          >
            Dandan Trading Terminal
          </span>
          <span
            className="badge badge-system"
            style={{ marginLeft: 'auto' }}
            title="幣安合約測試網 · 模擬盤"
          >
            合約測試網
          </span>
        </div>
        <div className="sub">幣安測試網模擬盤 · 虛擬資金 · 非投資建議</div>
      </header>
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
