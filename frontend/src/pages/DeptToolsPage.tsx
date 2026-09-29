// 团队定制化工具板块页（/dtools）：卡片首页 + 子路由（/dtools/resume 等）
// 2026-09-02：白名单制——工具按团队开通（custom_allow.{dept_id}，默认全团队关闭），
// 板块入口（导航/首页卡片）常驻；未开通团队显示空态；黑名单（custom_block）叠加最严生效
import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  CaretRight, FileText, Lock, Toolbox,
} from '@phosphor-icons/react'
import client from '../api/client'
import Icon from '../components/Icon'

// 板块工具卡片注册（id 对齐后端 DEPT_CUSTOM_TOOLS 注册表；显示与否由 /tools/dept-access 的 allowed 控制）
const DTOOLS = [
  { id: 'resume', path: '/dtools/resume', icon: FileText, title: '简历初筛', desc: '批量上传简历，设定 JD 与 7 维度权重，AI 评分排序并导出 TOP K', tag: 'AI 评分', tagStyle: 'badge badge-blue' },
]

interface DeptAccess { allowed: string[]; blocked: string[] | null }

// 走查：权限结果**模块级缓存**——首页卡片与子路由守卫共用。原实现每次进 /dtools 或每次导航
// 都重新请求一遍，期间渲染"加载中…/权限检查中…"占位（子工具页随之卸载重挂）。
// 缓存后：回来立刻渲染，后台静默刷新（权限变更仍会在下一次响应里生效）。
let _accessCache: DeptAccess | null = null

function DeptToolsIndex() {
  const navigate = useNavigate()
  // 走查：首页卡片也吃模块级缓存——原实现每次进 /dtools 都先渲染"加载中…"等一次接口往返
  // （权限判据与子路由守卫同源，缓存变量在文件下方声明，此处用函数取避免提升问题）
  const [access, setAccess] = useState<DeptAccess | null>(_accessCache)

  useEffect(() => {
    client.get('/tools/dept-access').then((r) => {
      const v = { allowed: r.data?.allowed ?? [], blocked: r.data?.blocked ?? null }
      _accessCache = v
      setAccess(v)
    }).catch(() => setAccess((prev) => prev ?? { allowed: [], blocked: null }))
  }, [])

  if (!access) return <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--text-3)', fontSize: 13 }}>加载中…</div>

  const openCards = DTOOLS.filter((t) => access.allowed.includes(t.id) && !(access.blocked ?? []).includes(t.id))

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>团队定制化工具</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>按团队开通的定制化工具（简历初筛），点击卡片进入</p>
      {openCards.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--text-3)' }}>
          <div style={{ fontSize: 40, marginBottom: 12 }}><Icon as={Toolbox} size={40} /></div>
          <div style={{ fontSize: 15, color: 'var(--text-2)', marginBottom: 6 }}>本团队暂未开通团队定制化工具</div>
          <div style={{ fontSize: 12 }}>如需使用，请联系团队管理员或通过「反馈」提交申请</div>
        </div>
      ) : (
        <div className="row-list">
          {openCards.map((t) => (
            <div key={t.id} className="row-item hoverable" onClick={() => navigate(t.path)}>
              <span className="row-icon"><Icon as={t.icon} size={17} /></span>
              <div className="row-main">
                <div className="row-title">{t.title}</div>
                <div className="row-desc">{t.desc}</div>
              </div>
              <span className="row-tail"><Icon as={CaretRight} size={14} /></span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// 子路由守卫：未开通的团队定制化工具显示未开通页
function DeptToolUnauthorized({ name }: { name: string }) {
  return (
    <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--text-3)' }}>
      <div style={{ fontSize: 40, marginBottom: 12 }}><Icon as={Lock} size={40} /></div>
      <div style={{ fontSize: 15, color: 'var(--text-2)', marginBottom: 6 }}>「{name}」未对本团队开通</div>
      <div style={{ fontSize: 12 }}>如需使用，请联系团队管理员开通，或通过「反馈」提交申请</div>
    </div>
  )
}

export default function DeptToolsPage() {
  const location = useLocation()
  const [access, setAccess] = useState<DeptAccess | null>(_accessCache)
  const [loaded, setLoaded] = useState(_accessCache !== null)

  useEffect(() => {
    client.get('/tools/dept-access').then((r) => {
      const v = { allowed: r.data?.allowed ?? [], blocked: r.data?.blocked ?? null }
      _accessCache = v
      setAccess(v)
      setLoaded(true)
    }).catch(() => setLoaded(true))   // 失败保留缓存值；从未成功过则按"没权限"渲染
  }, [location.pathname])

  // /dtools 精确匹配 → 卡片首页；/dtools/* 子路由 → 渲染工具页（含白名单守卫）
  if (location.pathname === '/dtools') return <DeptToolsIndex />
  const tool = DTOOLS.find((t) => t.path && location.pathname.startsWith(t.path))
  if (!loaded) {
    return <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--text-3)', fontSize: 13 }}>权限检查中…</div>
  }
  if (access && tool && (!access.allowed.includes(tool.id) || (access.blocked ?? []).includes(tool.id))) {
    return <DeptToolUnauthorized name={tool.title} />
  }
  return <Outlet />
}
