// 工具页统一页头（2026-09-04 布局统一：返回按钮独立一行 + 主标题/副标题 + 右侧操作位）
// 消除各页"返回+标题同行/换行、有无副标题、有无右侧按钮"的不一致（问题 1/2/3 修复）
import { ReactNode } from 'react'
import DeptToolsBack from './DeptToolsBack'

export default function PageHeader({ title, sub, action, backLabel = '返回', hideBack = false }: {
  title: ReactNode
  sub?: string
  action?: ReactNode
  backLabel?: string
  hideBack?: boolean  // admin 运维后台复用工具页（无板块上下文）时隐藏返回
}) {
  return (
    <header style={{ marginBottom: 16 }}>
      {!hideBack && <DeptToolsBack label={backLabel} />}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-end', gap: 12, marginTop: 8 }}>
        <div style={{ minWidth: 0 }}>
          {/* 2026-09-18：h2 改 flex 居中——标题里的图标（`<Icon/> + 文字`）原先是行内排版，
              图标偏高约 3px；改 flex 后图标与文字严格居中，且间距统一 6px，
              各页只需把图标放进 title、不必各自调样式。 */}
          <h2 style={{ fontSize: 18, marginBottom: sub ? 4 : 0, display: 'flex', alignItems: 'center', gap: 6 }}>{title}</h2>
          {sub && <p style={{ fontSize: 12, color: 'var(--text-3)', margin: 0, lineHeight: 1.6 }}>{sub}</p>}
        </div>
        {action ?? null}
      </div>
    </header>
  )
}
