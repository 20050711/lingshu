// 定制化工具页：卡片首页（/tools）+ 子路由（/tools/kb、/tools/meeting、/tools/download）
// 点击卡片跳转对应工具页（与 MCP 页卡片风格一致）；再次点击导航即进入
// 2026-09-01（黑名单改造）：团队定制化工具黑名单——被禁卡片置灰 + 子路由守卫（后端接口同步拦截；未配置=全部可用）
import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  Books, CaretRight, Lock, Microphone, Package,
} from '@phosphor-icons/react'
import client from '../api/client'
import Icon from '../components/Icon'

// 卡片字段显式声明：`disabled`（"规划中"置灰卡）当前没有卡片在用——
// 由推断已推不出该字段，显式保留以免下次加置灰卡时又撞类型错。
const TOOLS: {
  id: string; path?: string; icon: typeof Books; title: string; desc: string
  tag: string; tagStyle: string; disabled?: boolean
}[] = [
  // 2026-09-02：video/resume 已迁入「团队定制化工具」板块（/dtools，白名单制）
  { id: 'kb', path: '/tools/kb', icon: Books, title: '知识库浏览', desc: '按分类目录浏览团队知识文档，关键词搜索与内容预览', tag: '团队知识', tagStyle: 'badge badge-blue' },
  // 2026-08-25：会议纪要工具（录音/音频上传 → 转写 → 场景总结 → zip 下载）
  { id: 'meeting', path: '/tools/meeting', icon: Microphone, title: '会议纪要', desc: '浏览器录音或上传音频，AI 转写并生成场景化会议纪要，一键下载完整档案', tag: 'AI 整理', tagStyle: 'badge badge-blue' },
  // 2026-09-04：工具下载（离线工具包 zip；列表由后端扫描下载目录，无文件时页面为空提示）
  { id: 'download', path: '/tools/download', icon: Package, title: '工具下载', desc: '离线工具包下载，运维放入工具包即自动出现', tag: '工具包', tagStyle: 'badge badge-blue' },
]

function ToolsIndex() {
  const navigate = useNavigate()
  // 2026-09-01（黑名单改造）：本团队定制化工具禁用清单（null=未配置全部可用）
  const [blocked, setBlocked] = useState<string[] | null>(null)
  // 2026-08-25：HTTPS 入口端口（部署机配置；http 下点「会议纪要」自动跳转 https 供浏览器录音）
  const [httpsPort, setHttpsPort] = useState<number | null>(null)

  useEffect(() => {
    client.get('/tools/access').then((r) => {
      setBlocked(r.data?.blocked ?? null)
      setHttpsPort(r.data?.https_port ?? null)
    }).catch(() => setBlocked(null))
  }, [])

  const isDenied = (id: string) => blocked != null && blocked.includes(id)

  // 2026-08-25（用户要求）：会议纪要卡片在 http 协议下点击 → 自动切 https 端口（录音 secure context；
  // 开发机 https_port=None 不跳转；cookie 无 Secure 属性，https 同 host 正常携带登录态）
  const openCard = (t: { id: string; path?: string; disabled?: boolean }) => {
    if (t.disabled || !t.path) return
    if (t.id === 'meeting' && window.location.protocol === 'http:' && httpsPort && window.location.hostname !== 'localhost') {
      window.location.href = `https://${window.location.hostname}:${httpsPort}${t.path}`
      return
    }
    navigate(t.path)
  }

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>定制化工具</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>AI 批量处理工具（知识库浏览 / 会议纪要），点击卡片进入</p>
      <div className="row-list">
        {TOOLS.map((t) => {
          const denied = isDenied(t.id)
          const disabled = t.disabled || denied
          return (
            <div key={t.path || t.id}
              className={`row-item ${disabled ? 'disabled' : 'hoverable'}`}
              onClick={() => openCard(t)}>
              <span className="row-icon"><Icon as={t.icon} size={17} /></span>
              <div className="row-main">
                <div className="row-title">
                  {t.title}
                  {denied && <span className="badge badge-gray">本团队已停用</span>}
                </div>
                <div className="row-desc">{t.desc}</div>
              </div>
              <span className="row-tail"><Icon as={CaretRight} size={14} /></span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// 2026-09-01（黑名单改造）：子路由守卫——被禁用的定制化工具显示禁用页（后端接口已同步拦截）
// 员工可执行指引：不引导管理后台路径（员工无权限），改为找团队管理员/反馈
function UnauthorizedTool({ name }: { name: string }) {
  return (
    <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--text-3)' }}>
      <div style={{ fontSize: 40, marginBottom: 12 }}><Icon as={Lock} size={40} /></div>
      <div style={{ fontSize: 15, color: 'var(--text-2)', marginBottom: 6 }}>「{name}」已被本团队停用</div>
      <div style={{ fontSize: 12 }}>如需使用，请联系团队管理员开通，或通过「反馈」提交申请</div>
    </div>
  )
}

// 走查：权限结果**模块级缓存**——原实现每次导航都 setLoaded(false) 重查一遍，期间整页被
// "权限检查中…"占位（/tools 各子工具之间切换会卸载重挂子页面并闪占位）。
// 缓存后：回来立刻渲染，后台静默刷新（权限变更仍会在下一次响应里生效）。
let _blockedCache: string[] | null = null
let _blockedLoaded = false

export default function ToolsPage() {
  const location = useLocation()
  const [blocked, setBlocked] = useState<string[] | null>(_blockedCache)
  const [loaded, setLoaded] = useState(_blockedLoaded)

  useEffect(() => {
    client.get('/tools/access').then((r) => {
      _blockedCache = r.data?.blocked ?? null
      _blockedLoaded = true
      setBlocked(_blockedCache)
      setLoaded(true)
    }).catch(() => setLoaded(true))   // 失败保留缓存值；从未成功过则按"不限制"渲染（原行为）
  }, [location.pathname])

  // /tools 精确匹配 → 卡片首页；/tools/* 子路由 → 渲染工具页（含权限守卫）
  if (location.pathname === '/tools') return <ToolsIndex />
  const tool = TOOLS.find((t) => t.path && location.pathname.startsWith(t.path))
  // C2（2026-08-12）：加载完成前渲染 loading 占位，不放行 Outlet——原 blocked===null 与
  // 「不限制」共用语义，未授权用户会先看到真实工具页再闪成未授权页
  if (!loaded) {
    return <div style={{ textAlign: 'center', padding: '80px 0', color: 'var(--text-3)', fontSize: 13 }}>权限检查中…</div>
  }
  if (tool && blocked != null && blocked.includes(tool.id)) {
    return <UnauthorizedTool name={tool.title} />
  }
  return <Outlet />
}
