import { useState } from 'react'
import Live from './components/Live.jsx'
import Backtest from './components/Backtest.jsx'
import Explain from './components/Explain.jsx'
import Optimize from './components/Optimize.jsx'
import Journal from './components/Journal.jsx'
import Whales from './components/Whales.jsx'

const TABS = [
  { key: 'live',     label: '即時監控',   el: <Live /> },
  { key: 'whales',   label: '大戶籌碼',   el: <Whales /> },
  { key: 'backtest', label: '回測',        el: <Backtest /> },
  { key: 'explain',  label: '決策流程',   el: <Explain /> },
  { key: 'optimize', label: '參數最佳化', el: <Optimize /> },
  { key: 'journal',  label: '交易日誌',   el: <Journal /> },
]

export default function App() {
  const [tab, setTab] = useState('live')
  return (
    <div className="wrap">
      <header>
        <h1>丹丹交易團隊 — 儀表板</h1>
        <div className="sub">幣安測試網模擬盤 · 虛擬資金 · 非投資建議</div>
      </header>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.key} className={`tab ${tab === t.key ? 'active' : ''}`}
            onClick={() => setTab(t.key)}>{t.label}</button>
        ))}
      </div>
      {TABS.map((t) => (
        <div key={t.key} style={{ display: tab === t.key ? 'block' : 'none' }}>{t.el}</div>
      ))}
    </div>
  )
}
