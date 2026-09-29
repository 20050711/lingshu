// 团队定制化工具板块返回按钮（2026-09-02 统一）
// 2026-09-24：样式与行为抽到通用组件 BackButton（页面级与"页面内上一层"共用一份实现）。
import BackButton from './BackButton'

export default function DeptToolsBack({ label = '返回' }: { label?: string }) {
  return <BackButton label={label} />
}
