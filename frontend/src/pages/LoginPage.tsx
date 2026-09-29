import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Select, message } from 'antd'
import client, { errMessage, getClientId } from '../api/client'
import { useAuthStore } from '../stores/authStore'

// 登录页：居中卡片 + 动态验证码（错 3 次后展开），对照原型 pg-login
export default function LoginPage() {
  const navigate = useNavigate()
  const { login, logout } = useAuthStore()

  // B6（2026-08-12，用户侧 6 修复）：登录页挂载——
  // 原无条件 logout() 按用户吊销 token_version（L21），已登录用户（多标签/误入 /login）
  // 会被直接踢下线、其他标签 401。修复：本地已有登录态 → 不吊销 token，跳回对应首页；
  // 仅显式点「退出登录」（authStore.logout 由退出按钮触发）才走服务端吊销（L11/L21 全端掉线约定不变）。
  useEffect(() => {
    const raw = localStorage.getItem('user')
    if (raw) {
      try {
        const u = JSON.parse(raw)
        if (u?.role === 'admin') navigate('/admin', { replace: true })
        else if (u?.role === 'ceo') navigate('/home', { replace: true })  // 2026-08-17：CEO 看板未开放，登录进首页
        else navigate('/home', { replace: true })
        return
      } catch { /* 缓存损坏：走正常登出清理 */ }
    }
    logout()
  }, [])
  // 四期重构：登录三要素（团队下拉 + 账号 + 密码），账号团队内唯一
  const [departments, setDepartments] = useState<{ dept_id: string; name: string; preset_username?: string | null }[]>([])
  const [deptId, setDeptId] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showCaptcha, setShowCaptcha] = useState(false)
  const [captchaId, setCaptchaId] = useState('')
  const [captchaImg, setCaptchaImg] = useState('')
  const [captchaText, setCaptchaText] = useState('')

  // 预置账号团队——选团队后账号自动填充锁定（免输账号，只输密码）
  // 2026-09-01：账号名动态取（后端 /auth/departments preset_username）——ceo/admin 可自助改名，
  // 硬编码 'ceo'/'admin' 会锁旧名无法登录；改名后接口返回新名，此处锁的即新名
  const fixedUser = departments.find((d) => d.dept_id === deptId)?.preset_username || ''

  // 团队下拉数据源（公开接口，无需登录）；排序：ceo 置顶、dept_root（运维）置底、其余按名称拼音
  useEffect(() => {
    client.get('/auth/departments').then((r) => {
      const depts = (r.data?.departments || []).map((d: any) => ({
        dept_id: d.dept_id, name: d.name, preset_username: d.preset_username ?? null,
      }))
      const sorted = [...depts].sort((a, b) => {
        if (a.dept_id === 'ceo') return -1
        if (b.dept_id === 'ceo') return 1
        if (a.dept_id === 'dept_root') return 1
        if (b.dept_id === 'dept_root') return -1
        return a.name.localeCompare(b.name, 'zh-Hans-CN')
      })
      setDepartments(sorted)
      if (sorted.length > 0) setDeptId(sorted[0].dept_id)
    }).catch(() => {})
  }, [])

  // 选到预置账号团队 → 自动带出账号（动态 preset_username）；切到普通团队 → 清空用户名
  useEffect(() => {
    if (fixedUser) setUsername(fixedUser)
    else setUsername('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deptId, fixedUser])

  // 刷新验证码失败原来无 catch：图片没变、零反馈，用户以为点了没反应
  const loadCaptcha = async () => {
    try {
      const resp = await client.get('/auth/captcha')
      setCaptchaId(resp.data.captcha_id)
      setCaptchaImg(resp.data.captcha_image)
      setShowCaptcha(true)
    } catch (e) { message.error(errMessage(e) || '验证码刷新失败') }
  }

  const handleLogin = async () => {
    const realUser = fixedUser || username
    if (!deptId || !realUser || !password) {
      setError(fixedUser ? '请输入密码' : '请选择团队并输入账号和密码')
      return
    }
    setLoading(true)
    setError('')
    try {
      const resp = await client.post('/auth/login', {
        department_id: deptId,
        username: realUser,
        password,
        captcha_id: showCaptcha ? captchaId : undefined,
        captcha_text: showCaptcha ? captchaText : undefined,
      })
      getClientId()
      login(resp.data.user)  // L11：token 改 httpOnly cookie（登录响应不再返回 access_token）
      // B6：登录（含换账号）清会话指向——进入 QA 页自动新建空会话（历史会话仍在列表可切换）
      localStorage.removeItem('qa_current_session')
      if (resp.data.user.role === 'admin') navigate('/admin')
      else if (resp.data.user.role === 'ceo') navigate('/home')  // 2026-08-17：CEO 看板未开放，登录进首页
      else navigate('/home')
    } catch (e: any) {
      // C6（E-07）：验证码按后端 401 响应判断展开——后端仅在失败 ≥3 次后携带
      // captcha_id/captcha_image（原每次 401 都 loadCaptcha，前 2 次失败强填验证码与后端阈值不符）
      const d = e?.response?.data
      if (e?.response?.status === 401 && d?.captcha_id) {
        setCaptchaId(d.captcha_id)
        setCaptchaImg(d.captcha_image)
        setShowCaptcha(true)
      }
      setError(errMessage(e))
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      {/* 品牌区：光晕 + 价值主张（窄屏隐藏） */}
      <aside className="login-aside">
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <img
            src="/logo.jpg"
            alt="灵枢"
            className="login-brand-logo"
            style={{ width: 68, height: 68, borderRadius: 18, objectFit: 'cover', boxShadow: '0 12px 32px -14px rgba(0,0,0,0.7), inset 0 1px 0 rgba(255,255,255,0.15)' }}
          />
          <span style={{ fontWeight: 680, fontSize: 30, letterSpacing: '0.06em' }}>灵枢</span>
        </div>
        <div>
          <span className="eyebrow">Agent Platform</span>
          <h1 style={{ fontSize: 'clamp(30px, 3.2vw, 46px)', lineHeight: 1.18, letterSpacing: '-0.02em', fontWeight: 680, margin: '18px 0 16px' }}>
            把复杂任务
            <br />
            交给智能体
          </h1>
          <p style={{ fontSize: 14, lineHeight: 2, color: 'var(--text-2)', maxWidth: '46ch' }}>
            多工具自主调度 · 内核级沙盒执行 · 知识库与长期记忆
            <br />
            以自然语言描述需求，智能体自主规划、调度工具并校验结果，交付图表、文档与报告。
          </p>
        </div>
        <div style={{ fontSize: 12, color: 'var(--text-3)' }}>© 2026 灵枢 · 智能体平台</div>
      </aside>

      <div className="login-form-side">
      <div className="login-card">
        {/* 窄屏（品牌区隐藏）时，卡片内自带品牌行 */}
        <div className="login-card-brand">
          <img src="/logo.jpg" alt="灵枢" style={{ width: 48, height: 48, borderRadius: 13, objectFit: 'cover' }} />
          <span style={{ fontWeight: 680, fontSize: 22, letterSpacing: '0.06em' }}>灵枢</span>
        </div>
        <div style={{ marginBottom: 24 }}>
          <h1 style={{ fontSize: 19, margin: '0 0 6px', color: 'var(--text-1)', fontWeight: 650, letterSpacing: '-0.01em' }}>登录</h1>
          <p style={{ fontSize: 12.5, color: 'var(--text-3)' }}>选择团队并使用你的账号登录</p>
        </div>
        {/* 原生 select 下拉弹层为 OS 渲染（Linux 深色主题黑色弹层，CSS 无法控制）——换 AntD Select，样式可控 */}
        <Select
          value={deptId}
          onChange={(v) => setDeptId(v)}
          style={{ width: '100%', height: 40, marginBottom: 12 }}
          options={departments.map((d) => ({ value: d.dept_id, label: d.name }))}
        />
        <input
          className="login-input"
          placeholder={fixedUser ? '账号已自动带出' : '请输入账号'}
          value={fixedUser || username}
          disabled={!!fixedUser}
          onChange={(e) => setUsername(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
          style={fixedUser ? { background: 'var(--surface-2)', color: 'var(--text-2)' } : undefined}
        />
        <input
          className="login-input"
          type="password"
          placeholder="请输入密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && handleLogin()}
        />
        {showCaptcha && (
          <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center' }}>
            <input
              className="login-input"
              placeholder="验证码"
              value={captchaText}
              onChange={(e) => setCaptchaText(e.target.value)}
              style={{ flex: 1, marginBottom: 0 }}
            />
            <img
              src={`data:image/png;base64,${captchaImg}`}
              alt="captcha"
              style={{ height: 40, cursor: 'pointer', borderRadius: 4 }}
              onClick={loadCaptcha}
              title="点击换一张"
            />
          </div>
        )}
        {error && (
          <div style={{ color: 'var(--error)', fontSize: 12, marginBottom: 10 }}>{error}</div>
        )}
        <button
          className="login-btn"
          disabled={loading}
          onClick={handleLogin}
          style={{ opacity: loading ? 0.6 : 1 }}
        >
          {loading ? '登录中...' : '登 录'}
        </button>
        {/* 2026-09-01：提示文案随账号设置功能调整（登录后自助改密，默认密码找运维） */}
        <p style={{ fontSize: 11, color: 'var(--text-3)', textAlign: 'center', marginTop: 14, lineHeight: 1.9 }}>
          登录后请尽快修改账号密码<br />
          联系管理员获取默认账号密码
        </p>
      </div>
      </div>
    </div>
  )
}
