// 统一「返回」按钮（2026-09-24 用户走查提出）
//
// 平台上"返回"有两种层级，以前各写各的（页面级是胶囊、层内是 antd Button，样式与文案都不一致）：
//   1) 页面级：工具页 → 工具列表（走浏览器历史）——PageHeader 里的 DeptToolsBack 已在用
//   2) 页面内上一层：列表 ↔ 详情/子树（走组件状态，不入历史）
// 现在两者共用这一个组件：**样式完全一致（胶囊按钮）**，页面级不传 onClick（history.back()），
// 层内返回传 `onClick` 回到上一层状态。label 建议写清目标（如「返回客户列表」）。
export default function BackButton({ label = '返回', onClick }:
  { label?: string; onClick?: () => void }) {
  return (
    <button
      onClick={onClick ?? (() => window.history.back())}
      style={{
        cursor: 'pointer', color: 'var(--brand-ink)', fontSize: 12, padding: '6px 14px',
        marginBottom: 8, borderRadius: 16, border: '1px solid rgba(26, 86, 219, 0.35)',
        background: 'var(--brand-soft)',
      }}
    >
      ← {label}
    </button>
  )
}
