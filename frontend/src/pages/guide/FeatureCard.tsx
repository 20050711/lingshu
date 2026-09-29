/**
 * 功能教学卡片（Bento 网格项）：点击弹出浮窗（浮窗内完整教学 + 演示）。
 */
import Icon from '../../components/Icon'
import type { Feature } from './features'

interface Props {
  feature: Feature
  onOpen: (f: Feature) => void
  disabled?: boolean  // 置灰（如 CEO 看板功能待完善）
}

export default function FeatureCard({ feature, onOpen, disabled }: Props) {
  return (
    <div className={`gd-card-shell ${disabled ? 'gd-card-disabled' : ''}`}>
      <div className="gd-card-core">
        <div
          className="gd-card-head"
          role="button"
          tabIndex={disabled ? -1 : 0}
          aria-label={`打开 ${feature.title} 教学`}
          onClick={() => !disabled && onOpen(feature)}
          onKeyDown={(e) => {
            if (!disabled && (e.key === 'Enter' || e.key === ' ')) {
              e.preventDefault()
              onOpen(feature)
            }
          }}
        >
          <div className="gd-card-icon"><Icon as={feature.icon} size={22} /></div>
          <div className="gd-card-meta">
            <div className="gd-card-title">{feature.title}</div>
            <div className="gd-card-desc">{feature.desc}</div>
          </div>
          <span className="gd-card-open-hint">{disabled ? '待完善' : '查看教程 ↗'}</span>
        </div>
      </div>
    </div>
  )
}
