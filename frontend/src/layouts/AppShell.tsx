import { useState } from 'react'
import { useNavigate, useLocation, Outlet } from 'react-router-dom'
import {
  ChatCircle, House, Key, Lightbulb, PlugsConnected,
  PuzzlePiece, SignOut, Toolbox, Wrench, type Icon as PhosphorIcon,
} from '@phosphor-icons/react'
import { useAuthStore } from '../stores/authStore'
import AccountModal from '../components/AccountModal'
import Icon from '../components/Icon'

// 深色玻璃 Sidebar（208px）+ Header（58px）
// 2026-09-18：导航图标由 emoji 换成 Phosphor Light（统一走 components/Icon 封装）
// 2026-09-28：分组导航（工作台 / 能力 / 支持），「AI 外部工具」更名「MCP 工具」
const NAV: { path: string; icon: PhosphorIcon; label: string; group: string; role?: string }[] = [
  { path: '/home', icon: House, label: '首页', group: '工作台' },
  { path: '/qa', icon: ChatCircle, label: '智能助手', group: '工作台' },
  { path: '/skills', icon: PuzzlePiece, label: '技能管理', group: '能力' },
  { path: '/mcp', icon: PlugsConnected, label: 'MCP 工具', group: '能力' },
  { path: '/tools', icon: Wrench, label: '定制化工具', group: '能力' },
  // 2026-09-02：团队定制化工具板块（白名单制，入口常驻；板块内工具按团队开通）
  { path: '/dtools', icon: Toolbox, label: '团队定制化工具', group: '能力' },
]
const NAV_GROUPS = ['工作台', '能力']

const CRUMBS: Record<string, string> = {
  '/home': '首页',
  '/qa': '智能助手',
  '/skills': '技能管理',
  '/mcp': 'MCP 工具',
  '/tools': '定制化工具',
  '/dtools': '团队定制化工具',
  '/guide': '使用说明',
  '/feedback': '反馈',
}
// 面包屑兜底：子路由（/tools/kb 等）取最长已注册前缀（扩展⑤，原 CRUMBS[path] 查无 → 尾随空段）
const crumbFor = (path: string) =>
  CRUMBS[path] ||
  Object.entries(CRUMBS).sort((a, b) => b[0].length - a[0].length).find(([p]) => path.startsWith(p))?.[1] || ''

export default function AppShell() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()
  const path = location.pathname
  // 2026-09-01：账号设置（改账号名/改密码）入口
  const [accountOpen, setAccountOpen] = useState(false)

  const handleLogout = () => {
    logout()
    navigate('/login')
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <img src="/logo.jpg" alt="logo" style={{ width: 30, height: 30, borderRadius: 6, objectFit: 'cover' }} />
          灵枢
        </div>
        <nav className="sidebar-nav">
          {NAV_GROUPS.map((group) => {
            const items = NAV.filter((item) => item.group === group && (!item.role || item.role === user?.role))
            if (!items.length) return null
            return (
              <div key={group}>
                <div className="sidebar-group">{group}</div>
                {items.map((item) => (
                  <div
                    key={item.path}
                    // C11（2026-08-12）：子路由（/tools/kb 等）父项高亮（原严格相等不高亮）
                    className={`sidebar-item ${path.startsWith(item.path) ? 'active' : ''}`}
                    onClick={() => navigate(item.path)}
                  >
                    <Icon as={item.icon} style={{ flexShrink: 0 }} /> {item.label}
                  </div>
                ))}
              </div>
            )
          })}
        </nav>
        <div className="sidebar-footer">
          {/* 2026-09-01：修改账号密码入口（反馈上方；用户自助改账号名/改密码） */}
          <div className="sidebar-item" onClick={() => setAccountOpen(true)}>
            <Icon as={Key} style={{ flexShrink: 0 }} /> 修改账号密码
          </div>
          {/* 反馈中心入口（退出上方） */}
          <div className={`sidebar-item ${path.startsWith('/feedback') ? 'active' : ''}`} onClick={() => navigate('/feedback')}>
            <Icon as={Lightbulb} style={{ flexShrink: 0 }} /> 反馈
          </div>
          <div className="sidebar-item" onClick={handleLogout}>
            <Icon as={SignOut} style={{ flexShrink: 0 }} /> 退出登录
          </div>
        </div>
      </aside>
      <AccountModal open={accountOpen} onClose={() => setAccountOpen(false)} />
      <div className="main-area">
        <header className="header">
          <div style={{ fontSize: 14 }}>
            <span style={{ fontWeight: 600 }}>{user?.dept_name || ''}</span>
            <span style={{ color: 'var(--text-2)' }}> / 灵枢 / {crumbFor(path)}</span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ color: 'var(--text-2)', fontSize: 12.5 }}>{user?.username}</span>
            <button
              onClick={handleLogout}
              className="badge badge-gray"
              style={{ cursor: 'pointer', fontFamily: 'inherit' }}
            >
              退出
            </button>
          </div>
        </header>
        <div className="content-area">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
