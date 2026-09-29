import { useNavigate } from 'react-router-dom'
import {
  BookOpen, ChatCircle, Lightbulb, PlugsConnected,
  PuzzlePiece, Toolbox, Wrench, type Icon as PhosphorIcon,
} from '@phosphor-icons/react'
import { useAuthStore } from '../stores/authStore'
import Icon from '../components/Icon'

// 首页：hero + 功能卡栅格（深色玻璃主题；主推卡占两格做非对称 bento）
interface HomeCard {
  path: string
  icon: PhosphorIcon
  title: string
  desc: string
  tag: string
  tagStyle: string
  span2?: boolean
}

const BASE_CARDS: HomeCard[] = [
  {
    path: '/qa', icon: ChatCircle, span2: true, title: '智能助手',
    desc: '以自然语言描述需求：检索资料、分析数据、生成图表与演示文稿，全程自主调度工具并交付结果。',
    tag: '核心', tagStyle: 'badge badge-blue',
  },
  {
    path: '/skills', icon: PuzzlePiece, title: '技能管理',
    desc: '勾选启用 AI 技能，管理团队技能与全局技能，决定助手的能力面。',
    tag: '15 项能力', tagStyle: 'badge badge-blue',
  },
  {
    path: '/mcp', icon: PlugsConnected, title: 'MCP 工具',
    desc: '按 MCP 协议接入外部服务与数据源，启用后助手即可调用。',
    tag: '可扩展', tagStyle: 'badge badge-blue',
  },
  {
    path: '/tools', icon: Wrench, title: '定制化工具',
    desc: '会议纪要与知识库浏览等批处理工具，上传即出结果。',
    tag: '批处理', tagStyle: 'badge badge-blue',
  },
  {
    path: '/dtools', icon: Toolbox, title: '团队定制化工具',
    desc: '按团队开通的定制化工具（简历初筛等），由团队管理员分配。',
    tag: '团队能力', tagStyle: 'badge badge-blue',
  },
  {
    path: '/feedback', icon: Lightbulb, title: '反馈',
    desc: '提交问题与建议，跟踪处理进度，查看管理员回复。',
    tag: '已上线', tagStyle: 'badge badge-green',
  },
  {
    path: '/guide', icon: BookOpen, title: '使用说明',
    desc: '三分钟上手：快速开始向导、逐功能图文教程与模拟演示。',
    tag: '已上线', tagStyle: 'badge badge-green',
  },
]

export default function HomePage() {
  const navigate = useNavigate()
  const { user } = useAuthStore()
  const roleLabel = user?.role === 'dept_admin' ? '管理员' : user?.role === 'admin' ? '管理员' : ''

  return (
    <div>
      {/* Hero：眉标 + 大标题 + 价值主张 + 主行动 */}
      <section className="welcome-banner rise-in">
        <span className="eyebrow">灵枢 · 智能体平台</span>
        <h1>
          把复杂任务，
          <br />
          交给智能体
        </h1>
        <p className="hero-sub">
          {user?.dept_name ? `${user.dept_name} · ${user.username}` : '欢迎回来'}
          {roleLabel && `（${roleLabel}）`}——以自然语言描述需求，助手自主完成资料检索、数据分析与成果产出，
          交付图表、文档与报告。
        </p>
        <div style={{ display: 'flex', gap: 12, marginTop: 22, flexWrap: 'wrap' }}>
          <button className="btn-primary" style={{ letterSpacing: 0 }} onClick={() => navigate('/qa')}>
            开始对话
          </button>
          <button className="btn-ghost" onClick={() => navigate('/guide')}>
            查看使用说明
          </button>
        </div>
      </section>

      {/* 功能卡栅格 */}
      <div className="func-grid">
        {BASE_CARDS.map((c, i) => (
          <div
            key={c.path}
            className={`func-card card-hover rise-in ${c.span2 ? 'span-2' : ''}`}
            style={{ animationDelay: `${60 + i * 55}ms` }}
            onClick={() => navigate(c.path)}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
              <span className="func-icon-inline"><Icon as={c.icon} size={18} /></span>
              <span style={{ fontSize: 15, fontWeight: 640, letterSpacing: '-0.01em' }}>{c.title}</span>
            </div>
            <p style={{ fontSize: 12.5, color: 'var(--text-2)', lineHeight: 1.75 }}>{c.desc}</p>
          </div>
        ))}
      </div>
    </div>
  )
}
