import { useState } from 'react'
import { useNavigate, useLocation, Outlet } from 'react-router-dom'
import {
  Books, Brain, Buildings, ChartBar, ChatCircleDots, Database, FileText, Gear,
  Key, Package, PlugsConnected, PuzzlePiece, ShieldCheck, SignOut,
  Wrench, type Icon as PhosphorIcon,
} from '@phosphor-icons/react'
import { useAuthStore } from '../stores/authStore'
import AccountModal from '../components/AccountModal'
import Icon from '../components/Icon'

// 管理后台布局：左侧菜单 + 顶部账号（PRD：与业务功能完全分离）
// 2026-08-07：用户管理与团队管理合并为一页（团队列表 + 右侧用户表，含用户级工具权限）
// 2026-09-18：菜单图标由 emoji 换成 Phosphor Light（统一走 components/Icon 封装）
const MENUS: { path: string; icon: PhosphorIcon; label: string }[] = [
  { path: '/admin/overview', icon: ChartBar, label: '系统概览' },
  { path: '/admin/departments', icon: Buildings, label: '组织与用户' },
  { path: '/admin/memory', icon: Brain, label: '记忆库管理' },
  { path: '/admin/kb', icon: Books, label: '知识库管理' },
  { path: '/admin/global-skills', icon: PuzzlePiece, label: '技能管理' },
  { path: '/admin/mcp', icon: PlugsConnected, label: 'MCP 工具' },
  { path: '/admin/tool-downloads', icon: Package, label: '工具下载' },
  { path: '/admin/data', icon: Database, label: '数据备份' },
  { path: '/admin/feedback', icon: ChatCircleDots, label: '用户反馈' },
  { path: '/admin/config', icon: Gear, label: '配置管理' },
  { path: '/admin/logs', icon: FileText, label: '日志与监控' },
  { path: '/admin/maintenance', icon: Wrench, label: '系统维护' },
]

export default function AdminLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()
  // 2026-09-01：运维账号设置（改账号名/改密码）
  const [accountOpen, setAccountOpen] = useState(false)

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <img src="/logo.jpg" alt="logo" style={{ width: 30, height: 30, borderRadius: 6, objectFit: 'cover' }} />
          管理后台
        </div>
        <nav className="sidebar-nav">
          {MENUS.map((m) => (
            <div
              key={m.path}
              className={`sidebar-item ${location.pathname.startsWith(m.path) ? 'active' : ''}`}
              onClick={() => navigate(m.path)}
            >
              <Icon as={m.icon} style={{ flexShrink: 0 }} /> {m.label}
            </div>
          ))}
        </nav>
        <div className="sidebar-footer">
          {/* 2026-09-01：运维账号设置入口（退出上方） */}
          <div className="sidebar-item" onClick={() => setAccountOpen(true)}>
            <Icon as={Key} style={{ flexShrink: 0 }} /> 修改账号密码
          </div>
          <div
            className="sidebar-item"
            onClick={() => {
              logout()
              navigate('/login')
            }}
          >
            <Icon as={SignOut} style={{ flexShrink: 0 }} /> 退出登录
          </div>
        </div>
      </aside>
      <AccountModal open={accountOpen} onClose={() => setAccountOpen(false)} />
      <div className="main-area">
        <header className="header">
          <div style={{ fontSize: 14 }}>管理后台 / 运维控制台</div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span className="badge badge-blue"><Icon as={ShieldCheck} style={{ flexShrink: 0 }} /> {user?.username}</span>

          </div>
        </header>
        <div className="content-area">
          <Outlet />
        </div>
      </div>
    </div>
  )
}
