// 白話說明小工具：替專業指標/標示加上看得懂的解釋。
//   <Hint text="...">標籤</Hint>  → 標籤旁出現可 hover 的「?」圖示
//   <Plain>這頁在做什麼…</Plain>   → 頁面頂端的白話說明框

/** 標籤旁的「?」圖示，hover（或長按）顯示白話說明。 */
export default function Hint({ text, children, style }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, ...style }}>
      {children}
      <span
        title={text}
        role="img"
        aria-label={typeof text === 'string' ? text : '說明'}
        style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 14, height: 14, borderRadius: '50%', cursor: 'help', flex: 'none',
          fontSize: 10, fontWeight: 700, lineHeight: 1,
          color: 'var(--muted)', border: '1px solid var(--line-strong)',
        }}
      >?</span>
    </span>
  )
}

/** 頁面頂端的白話說明框（用人話講這頁在幹嘛、數字怎麼看）。 */
export function Plain({ children }) {
  return (
    <div
      style={{
        fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.65,
        background: 'var(--hint-bg)', border: '1px solid var(--hint-border)',
        borderRadius: 'var(--radius)', padding: '9px 12px', margin: '0 0 14px',
      }}
    >
      <span style={{ marginRight: 6 }}>💡</span>{children}
    </div>
  )
}
