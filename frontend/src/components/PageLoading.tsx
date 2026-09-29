// 页面骨架预加载（2026-09-04 问题 6：网络差时首屏空白/卡顿——统一 Skeleton 骨架）
// 用法：<PageLoading show={loading} />（loading=true 渲染骨架占位，false 返回 null）
import { Skeleton } from 'antd'

export default function PageLoading({ show, rows = 3 }: { show: boolean, rows?: number }) {
  if (!show) return null
  return (
    <div style={{ padding: '8px 4px' }}>
      <Skeleton active title={{ width: 180 }} paragraph={{ rows: 1, width: 420 }} />
      <Skeleton active title={{ width: 300 }} paragraph={{ rows }} style={{ marginTop: 24 }} />
    </div>
  )
}
