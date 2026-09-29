import { Navigate, Route, Routes } from 'react-router-dom'
import { ReactNode } from 'react'
import AppShell from '../layouts/AppShell'
import AdminLayout from '../layouts/AdminLayout'
import LoginPage from '../pages/LoginPage'
import HomePage from '../pages/HomePage'
import SkillsPage, { SkillsDefaultPage, SkillsDeptPage, SkillsGlobalPage } from '../pages/SkillsPage'
import McpPage from '../pages/McpPage'
import McpAdminPage from '../pages/admin/McpAdminPage'
import ToolDownloadsAdminPage from '../pages/admin/ToolDownloadsAdminPage'
import FeedbackPage from '../pages/FeedbackPage'
import GuidePage from '../pages/GuidePage'
import ToolsPage from '../pages/ToolsPage'
import ToolsDownloadPage from '../pages/ToolsDownloadPage'
import DeptToolsPage from '../pages/DeptToolsPage'
import ResumeTool from '../pages/tools/ResumeTool'
import KbBrowseTool from '../pages/tools/KbBrowseTool'
import MeetingTool from '../pages/tools/MeetingTool'
import QAPage from '../pages/QAPage'
import { useAuthStore } from '../stores/authStore'
import {
  ConfigPage,
  DataBackupPage,
  DepartmentsPage,
  FeedbackAdminPage,
  GlobalSkillsPage,
  KbPage,
  LogsPage,
  MaintenancePage,
  MemoryPage,
  OverviewPage,
} from '../pages/admin'

function RequireAuth({ children }: { children: ReactNode }) {
  const user = useAuthStore((s) => s.user)  // L11：登录态基于 user（token 已移入 httpOnly cookie）
  const role = useAuthStore((s) => s.user?.role)
  const ready = useAuthStore((s) => s.ready)
  if (!ready) return null  // 2026-08-18：身份校验完成前不渲染（防 localStorage 身份闪跳）
  if (!user) return <Navigate to="/login" replace />
  // 四期重构：运维管理（admin）不参与业务区，访问业务路由一律回管理后台
  if (role === 'admin') return <Navigate to="/admin" replace />
  return <>{children}</>
}

function RequireAdmin({ children }: { children: ReactNode }) {
  const { user, ready } = useAuthStore()
  if (!ready) return null
  if (!user) return <Navigate to="/login" replace />
  if (user?.role !== 'admin') return <Navigate to="/home" replace />
  return <>{children}</>
}

export default function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/home" replace />} />
        <Route path="home" element={<HomePage />} />
        <Route path="qa" element={<QAPage />} />
        <Route path="skills" element={<SkillsPage />}>
          <Route path="default" element={<SkillsDefaultPage />} />
          <Route path="dept" element={<SkillsDeptPage />} />
          <Route path="global" element={<SkillsGlobalPage />} />
        </Route>
        <Route path="mcp" element={<McpPage />} />
        <Route path="feedback" element={<FeedbackPage />} />
        <Route path="guide" element={<GuidePage />} />
        <Route path="tools" element={<ToolsPage />}>
          <Route path="kb" element={<KbBrowseTool />} />
          <Route path="meeting" element={<MeetingTool />} />
          <Route path="download" element={<ToolsDownloadPage />} />
        </Route>
        {/* 2026-09-02：团队定制化工具板块（白名单制；admin 业务页重定向管理后台） */}
        <Route path="dtools" element={<DeptToolsPage />}>
          <Route path="resume" element={<ResumeTool />} />
        </Route>
      </Route>
      <Route
        path="/admin"
        element={
          <RequireAdmin>
            <AdminLayout />
          </RequireAdmin>
        }
      >
        <Route index element={<Navigate to="/admin/overview" replace />} />
        <Route path="overview" element={<OverviewPage />} />
        {/* 2026-08-07：用户管理并入"组织与用户"（团队列表+右侧用户表）；旧链接重定向 */}
        <Route path="users" element={<Navigate to="/admin/departments" replace />} />
        <Route path="departments" element={<DepartmentsPage />} />
        <Route path="memory" element={<MemoryPage />} />
        <Route path="kb" element={<KbPage />} />
        <Route path="global-skills" element={<GlobalSkillsPage />} />
        <Route path="mcp" element={<McpAdminPage />} />
        <Route path="tool-downloads" element={<ToolDownloadsAdminPage />} />
        <Route path="config" element={<ConfigPage />} />
        <Route path="feedback" element={<FeedbackAdminPage />} />
        <Route path="data" element={<DataBackupPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="maintenance" element={<MaintenancePage />} />
      </Route>
      <Route path="*" element={<Navigate to="/home" replace />} />
    </Routes>
  )
}
