// 功能页：卡片路由（四期重构：默认功能=工具层直接勾选；团队功能=SKILL.md 技能文件）
// /skills → 卡片首页（默认功能卡 → /skills/default；团队功能卡 → /skills/dept）
// 默认功能：10 个工具按分组勾选 + 保存（按用户持久化，QA 功能栏同源同步）
// 团队功能：团队技能（SKILL.md，团队管理员上传/启停/删除；员工只读）
import { useEffect, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { Button, Checkbox, Popconfirm, Select, Switch, message } from 'antd'
import { Buildings, CaretRight, Globe, PuzzlePiece } from '@phosphor-icons/react'
import DraggableModal from '../components/DraggableModal'
import Icon, { iconFromKey } from '../components/Icon'
import client, { errMessage } from '../api/client'
import { adminApi } from '../api/admin'
import { useAuthStore } from '../stores/authStore'
import { useQAStore } from '../stores/qaStore'

// 2026-08-14（D3 二阶段）：辅助模型配置迁移到 AI 技能卡浮窗——四档工具对应模型层
// （图片识别=vision[GLM+agnes]、图片生成=image[GLM+agnes]、记忆库=llm_aux、视频生成=video[agnes]）
const AUX_MODEL_MAP: Record<string, { label: string; kind: 'vision' | 'image' | 'llm_aux' | 'video' }> = {
  image_recognition: { label: '图片识别模型', kind: 'vision' },
  image_generation: { label: '图片生成模型', kind: 'image' },
  memory: { label: '记忆库模型', kind: 'llm_aux' },
  video_generate: { label: '视频生成模型', kind: 'video' },
}

// 统一返回链接（AntD 轻量按钮：浅背景 + 圆角，清晰可点）
function BackLink({ to, label }: { to: string; label: string }) {
  const navigate = useNavigate()
  return (
    <Button type="default" size="small" icon={<span style={{ marginRight: 2 }}>←</span>}
      onClick={() => navigate(to)}
      style={{ marginBottom: 8, borderRadius: 16, fontSize: 12, color: 'var(--brand-ink)', borderColor: 'rgba(26, 86, 219, 0.35)', background: 'var(--brand-soft)' }}>
      {label}
    </Button>
  )
}

interface ToolMeta {
  id: string
  name: string
  icon: string
  summary: string
  description: string
  user_description?: string  // 4.1：写给用户的完整介绍（弹窗展示）
  select_mode?: string       // 4.1：multi=多选（auto 包含）/ single=单选（技能类）
  status: string
  sort_order: number
  group: string
}
interface DeptSkill {
  id: number
  name: string
  description: string
  tools?: string[] | null
  status?: string
  skill_dir?: string | null   // 2026-08-20：技能文件相对路径（zip 技能非空）
  has_scripts?: boolean       // 2026-08-20：zip 技能（含脚本）标记
}

// 分组展示顺序（视觉分组，checkbox 为工具粒度）
const GROUP_ORDER = ['数据', '产出', '检索', '媒体', '记忆', '代码', '技能']

function SkillsIndex() {
  const navigate = useNavigate()
  // C8（E-09）：团队技能页放开入口（后端接口已完整实现，文案过时不再"规划中"）
  // 2026-08-21：默认AI技能卡改名"默认AI工具"（内置技能已下线）；新增"默认AI技能"卡（运维发布的全局技能）
  const cards = [
    { path: '/skills/default', icon: PuzzlePiece, title: '默认AI工具', desc: '平台内置工具，多选勾选启用，随账号保存，问答页同步', tag: '全团队', tagStyle: 'badge badge-blue' },
    { path: '/skills/dept', icon: Buildings, title: '团队AI技能', desc: '本团队 SKILL.md 技能，团队管理员上传/启停，员工可见可勾选', tag: '团队', tagStyle: 'badge badge-purple' },
    { path: '/skills/global', icon: Globe, title: '默认AI技能', desc: '运维发布的全局技能（SKILL.md/技能包），全员可用；你可自行启用，团队可整组开关', tag: '全局', tagStyle: 'badge badge-green' },
  ]
  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>AI技能管理</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>默认AI工具为平台内置；团队AI技能由团队管理员上传；默认AI技能（全局）由运维发布，全员可见</p>
      <div className="row-list">
        {cards.map((t) => (
          <div key={t.path} className="row-item hoverable" onClick={() => navigate(t.path)}>
            <span className="row-icon"><Icon as={t.icon} size={17} /></span>
            <div className="row-main">
              <div className="row-title">
                {t.title}
                <span className="badge badge-gray">{t.tag}</span>
              </div>
              <div className="row-desc">{t.desc}</div>
            </div>
            <span className="row-tail"><Icon as={CaretRight} size={14} /></span>
          </div>
        ))}
      </div>
    </div>
  )
}

// 默认 AI 技能：工具多选勾选 / 技能单选启用 + 保存（按用户持久化）；卡片给人看的一句话说明，点击卡片查看详情
export function SkillsDefaultPage() {
  const [tools, setTools] = useState<ToolMeta[]>([])
  const [deptBlocked, setDeptBlocked] = useState<string[] | null>(null)  // 2026-09-01：本团队工具黑名单（null=无禁用）
  const activeSkills = useQAStore((s) => s.activeSkills)
  const autoSkill = useQAStore((s) => s.autoSkill)
  const setActiveSkills = useQAStore((s) => s.setActiveSkills)
  const setAutoSkill = useQAStore((s) => s.setAutoSkill)
  const saveSkillPrefs = useQAStore((s) => s.saveSkillPrefs)
  const [saving, setSaving] = useState(false)
  const [detail, setDetail] = useState<ToolMeta | null>(null)
  // 2026-08-14：辅助模型配置（技能卡浮窗内）——候选分层下发 + 用户级持久化
  // 2026-08-18：auxPrefs 扩展 thinking（deepseek 文本模型思考强度，记忆库卡生效）
  const [modelOptions, setModelOptions] = useState<Record<string, any[]>>({})
  const [auxPrefs, setAuxPrefs] = useState<Record<string, { platform: string; model: string; thinking?: string | null }>>({})

  useEffect(() => {
    client.get('/skills').then((r) => { setTools(r.data.tools || []); setDeptBlocked(r.data.dept_blocked ?? null) }).catch(() => {})
    // C5（E-06）：直达本页也加载已保存的技能偏好——原仅 QA 页调用，直达时 store 为默认 5 项，
    // 改一项保存即整组覆盖用户原配置
    useQAStore.getState().loadSkillPrefs().catch(() => {})
    client.get('/models/options').then((r) => setModelOptions(r.data || {})).catch(() => {})
    client.get('/models/aux-preferences').then((r) => setAuxPrefs(r.data.overrides || {})).catch(() => {})
  }, [])

  // C16（2026-08-12）：函数式更新 + 查重——原闭包读取 activeSkills 连点可入队过期更新
  // （同一 id 重复入数组）
  const toggle = (id: string, on: boolean) => {
    setActiveSkills((prev) => (on
      ? (prev.includes(id) ? prev : [...prev, id])
      : prev.filter((x) => x !== id)))
  }
  // 2026-09-01（黑名单改造）：团队黑名单命中 → 置灰（run_script 个人级兜底豁免）
  const isDenied = (id: string) => deptBlocked != null && deptBlocked.includes(id) && id !== 'run_script'

  const save = () => {
    setSaving(true)
    saveSkillPrefs()
      .then(() => message.success('已保存'))
      .catch(() => message.error('保存失败'))
      .finally(() => setSaving(false))
  }

  const groups = GROUP_ORDER
    .map((g) => ({ group: g, items: tools.filter((t) => t.group === g) }))
    .filter((g) => g.items.length > 0)

  return (
    <div>
      <BackLink to="/skills" label="返回" />
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>默认AI工具</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>平台内置工具多选勾选；保存后问答页同步（按账号持久化）· 点击卡片查看详情</p>
      {groups.map((g) => (
        <div key={g.group} style={{ marginBottom: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-2)', marginBottom: 8 }}>{g.group}</div>
          <div className="row-list">
            {g.items.map((s) => {
              // 2026-08-21：单选技能机制已下线（内置技能下线后无 single 工具）——统一 Checkbox 多选
              const denied = isDenied(s.id)
              const on = autoSkill || activeSkills.includes(s.id)
              return (
                <div key={s.id} className={`row-item ${denied ? 'disabled' : 'hoverable'}`}
                  onClick={() => setDetail(s)} title="点击查看详情">
                  {/* 图标名来自后端工具注册表（语义名，见 ICON_KEYS）——这里映射成 Phosphor 组件 */}
                  <span className="row-icon"><Icon as={iconFromKey(s.icon)} size={17} /></span>
                  <div className="row-main">
                    <div className="row-title">
                      {s.name}
                      {denied && <span className="badge badge-gray">本团队已停用</span>}
                    </div>
                    <div className="row-desc">{s.summary}</div>
                  </div>
                  <span className="row-tail" onClick={(e) => e.stopPropagation()}>
                    <Checkbox checked={on} disabled={autoSkill || denied}
                      title={autoSkill ? '自动选择已启用' : '勾选启用该功能'}
                      onChange={(e) => toggle(s.id, e.target.checked)}>
                      {on ? '已启用' : '启用'}
                    </Checkbox>
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      ))}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginTop: 12 }}>
        <Checkbox checked={autoSkill} onChange={(e) => setAutoSkill(e.target.checked)}>
          自动选择：自动启用全部技能
        </Checkbox>
        <button className="toolbar-btn" style={{ padding: '6px 18px' }} onClick={save} disabled={saving}>
          {saving ? '保存中…' : '保存'}
        </button>
      </div>
      <DraggableModal open={!!detail} footer={null} onCancel={() => setDetail(null)}
        title={detail ? <><Icon as={iconFromKey(detail.icon)} size={18} /> {detail.name}</> : ''}>
        {detail && (
          <div>
            <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>分组：{detail.group}</p>
            {/* 4.1：弹窗展示 user_description（写给用户的介绍）；description 是给 Agent 的，不再展示 */}
            <p style={{ fontSize: 13, lineHeight: 1.8, color: 'var(--text-1)' }}>{detail.user_description || detail.summary}</p>
            {/* 2026-08-14：辅助模型配置（仅四档工具卡展示；antd Select 无原生弹层黑闪） */}
            {AUX_MODEL_MAP[detail.id] && (
              <div style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 12 }}>
                <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}>{AUX_MODEL_MAP[detail.id].label}</div>
                <Select size="small" style={{ width: '100%' }}
                  value={auxPrefs[detail.id] ? `${auxPrefs[detail.id].platform}|${auxPrefs[detail.id].model}` : ''}
                  onChange={async (v) => {
                    const next: Record<string, { platform: string; model: string; thinking?: string | null }> = { ...auxPrefs }
                    if (!v) delete next[detail.id]
                    else {
                      const [platform, model] = String(v).split('|')
                      next[detail.id] = { platform, model, thinking: null }
                    }
                    try {
                      await client.put('/models/aux-preferences', { overrides: next })
                      setAuxPrefs(next)
                      message.success('模型配置已保存')
                    } catch (e: any) {
                      message.error(e.response?.data?.error?.message || '保存失败')
                    }
                  }}
                  options={[
                    { value: '', label: '默认（团队配置档）' },
                    ...(modelOptions[AUX_MODEL_MAP[detail.id].kind] || []).map((m: any) => ({
                      value: `${m.platform}|${m.model}`, label: `${m.model}（${m.platform}）`,
                    })),
                  ]} />
                {/* 2026-08-18：思考强度（deepseek 文本模型生效；记忆库等 llm_aux 档） */}
                {AUX_MODEL_MAP[detail.id].kind === 'llm_aux' && auxPrefs[detail.id]?.platform === 'deepseek' && (
                  <Select size="small" style={{ width: '100%', marginTop: 6 }}
                    value={auxPrefs[detail.id].thinking === null || auxPrefs[detail.id].thinking === undefined ? 'default' : auxPrefs[detail.id].thinking}
                    onChange={async (v) => {
                      const next = { ...auxPrefs }
                      const cur = next[detail.id]
                      if (!cur) return
                      next[detail.id] = { ...cur, thinking: v === 'default' ? null : String(v) }
                      try {
                        await client.put('/models/aux-preferences', { overrides: next })
                        setAuxPrefs(next)
                        message.success('思考强度已保存')
                      } catch (e: any) {
                        message.error(e.response?.data?.error?.message || '保存失败')
                      }
                    }}
                    options={[
                      { value: 'default', label: '思考：默认（跟随配置）' },
                      ...(modelOptions.thinking || []).filter((t: any) => t.key !== null).map((t: any) => ({
                        value: String(t.key), label: `思考：${t.label}`,
                      })),
                    ]} />
                )}
              </div>
            )}
          </div>
        )}
      </DraggableModal>
    </div>
  )
}

// 团队功能：本团队 SKILL.md 技能；dept_admin/admin 可上传/启停/删除；
// 2026-08-21：employee 可自行决定启用哪些技能（个人偏好，与团队启停取交集）
export function SkillsDeptPage() {
  const user = useAuthStore((s) => s.user)
  const [skills, setSkills] = useState<DeptSkill[]>([])
  const [uploading, setUploading] = useState(false)
  // 2026-08-20：admin 团队下拉切换（查看/管理任意团队技能；dept_admin/employee 固定本团队）
  const [depts, setDepts] = useState<{ dept_id: string; name: string }[]>([])
  const [selectedDept, setSelectedDept] = useState<string>('')
  // 2026-08-21：员工个人启用集合（dept-prefs；未配置=全启用）
  const [myEnabled, setMyEnabled] = useState<number[]>([])
  const isAdmin = user?.role === 'admin'
  const canManage = user?.role === 'dept_admin' || isAdmin
  const curDept = isAdmin && selectedDept ? selectedDept : undefined

  const load = () => {
    if (canManage) {
      // 2026-08-20：改调 /skills/files（/skills 对 admin 403 且响应无 status 字段致禁用技能显示启用）
      const params = curDept ? { dept_id: curDept } : undefined
      client.get('/skills/files', { params }).then((r) => {
        const ds: DeptSkill[] = r.data.skills || []
        setSkills(ds.map((d) => ({ ...d, status: d.status ?? 'active' })))
      // 管理员切「查看团队」失败会停在旧团队的数据上：提示（未选团队的首次加载不提示，免噪音）
      }).catch((e) => { if (curDept) message.error(errMessage(e) || '团队技能加载失败') })
    } else {
      // 2026-08-21：员工视图走 /skills/dept-prefs（含个人启用集合）
      client.get('/skills/dept-prefs').then((r) => {
        setSkills(r.data.skills || [])
        setMyEnabled(r.data.enabled_ids || [])
      }).catch(() => {})
    }
  }
  useEffect(() => {
    if (isAdmin) {
      adminApi.deptList().then((r: { departments: { dept_id: string; name: string }[] }) => {
        const list = r.departments || []
        setDepts(list)
        setSelectedDept((d) => d || list[0]?.dept_id || '')
      }).catch(() => {})
    }
  }, [isAdmin])
  useEffect(load, [curDept, canManage])

  // 2026-08-21：员工切换个人启用（乐观更新 + PUT 保存；失败回滚）
  const toggleMy = async (s: DeptSkill, on: boolean) => {
    const prev = myEnabled
    const next = on ? [...prev, s.id] : prev.filter((id) => id !== s.id)
    setMyEnabled(next)
    try {
      await client.put('/skills/dept-prefs', { enabled_ids: next })
      message.success(on ? `已启用「${s.name}」` : `已停用「${s.name}」`)
    } catch (e: any) {
      setMyEnabled(prev)
      message.error(e.response?.data?.error?.message || '保存失败')
    }
  }

  const upload = async (file: File) => {
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      if (curDept) form.append('dept_id', curDept)
      await client.post('/skills/files', form, { headers: { 'Content-Type': 'multipart/form-data' } })
      message.success('技能已上传生效')
      load()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '上传失败')
    } finally {
      setUploading(false)
    }
  }
  // C17（2026-08-12）：启停/删除失败不再 unhandled rejection——提示且不刷新
  const toggle = async (s: DeptSkill, on: boolean) => {
    try {
      await client.put(`/skills/files/${s.id}`, { status: on ? 'active' : 'disabled' })
      message.success('已更新')
      load()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '操作失败')
    }
  }
  const del = async (s: DeptSkill) => {
    try {
      await client.delete(`/skills/files/${s.id}`)
      message.success('已删除')
      load()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '删除失败')
    }
  }

  return (
    <div>
      <BackLink to="/skills" label="返回" />
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>团队AI技能</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
        {/* 2026-09-01：格式规格仅管理员可见（员工无上传入口，无需内部术语） */}
        {canManage
          ? '团队技能：SKILL.md（纯指令）或技能包 zip（SKILL.md + 脚本，zip 根须含 SKILL.md，脚本任意目录）；团队管理员可上传/启停/删除'
          : '团队技能由团队管理员发布；你可自行决定启用哪些技能（团队禁用的技能无法启用）'}
      </p>
      {isAdmin && (
        <div style={{ marginBottom: 12 }}>
          <span style={{ fontSize: 12, color: 'var(--text-2)', marginRight: 8 }}>查看团队：</span>
          <Select size="small" style={{ width: 180 }} value={selectedDept || undefined}
            options={depts.map((d) => ({ value: d.dept_id, label: `${d.name}（${d.dept_id}）` }))}
            onChange={(v) => setSelectedDept(v)} />
        </div>
      )}
      {canManage && (
        <div style={{ marginBottom: 12 }}>
          {/* 2026-08-20：去掉 display:'inline-block' 内联覆盖——toolbar-btn 的 inline-flex+align-items:center 居中文字 */}
          <label className="toolbar-btn" style={{ cursor: 'pointer', opacity: uploading ? 0.6 : 1 }}>
            {uploading ? '上传中…' : '+ 上传 SKILL.md / 技能包(.zip)'}
            <input type="file" accept=".md,text/markdown,.zip,application/zip" hidden
              onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = '' }} />
          </label>
        </div>
      )}
      {skills.length === 0 ? (
        <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-3)', background: 'var(--surface-inset)', border: '1px dashed var(--border)' }}>
          <div style={{ fontSize: 32, marginBottom: 8 }}><Icon as={Buildings} size={32} /></div>
          {/* 2026-09-01：按角色分流——员工版只说可执行的路径（无上传入口/无 SKILL 文件概念） */}
          <div>{canManage
            ? '本团队暂无技能——上传 SKILL 文件后即可启用'
            : '本团队暂无技能；如需定制本团队专属技能，可在「反馈」中向管理员提交需求'}</div>
        </div>
      ) : (
        <div className="row-list">
          {skills.map((s) => (
            <div key={s.id} className="row-item">
              <span className="row-icon"><Icon as={PuzzlePiece} size={17} /></span>
              <div className="row-main">
                <div className="row-title">
                  {s.name}
                  {s.has_scripts && <span className="badge badge-green" title="含脚本，agent 可用 run_script 执行">脚本</span>}
                </div>
                <div className="row-desc" title={s.description}>{s.description}</div>
                {s.tools && s.tools.length > 0 && (
                  <div style={{ fontSize: 11, color: 'var(--text-3)', marginTop: 2 }}>可用工具：{s.tools.join('、')}</div>
                )}
              </div>
              <span className="row-tail">
                {canManage ? (
                  <>
                    <Switch size="small" checked={s.status !== 'disabled'} onChange={(v) => toggle(s, v)} />
                    <Popconfirm title="确认删除该技能？" onConfirm={() => del(s)}>
                      <span style={{ fontSize: 12, color: 'var(--error)', cursor: 'pointer' }}>删除</span>
                    </Popconfirm>
                  </>
                ) : (
                  <>
                    <Switch size="small" checked={myEnabled.includes(s.id)}
                      disabled={s.status === 'disabled'}
                      onChange={(v) => toggleMy(s, v)} />
                    <span style={{ fontSize: 11, color: 'var(--text-3)' }}>我的启用</span>
                  </>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// 默认AI技能（全局技能，运维发布）：仅运维可上传/修改；三层交集生效（运维启停 ∩ 团队开关 ∩ 员工个人）
// - employee：个人启停（dept_enabled=false 或 status!=active 置灰不可开）
// - dept_admin/ceo：本团队整组开关（"全部允许"= 恢复未配置状态）
// - admin：不进本页（RequireAuth 重定向 /admin），管理在 admin/global-skills
interface GlobalSkill {
  id: number
  name: string
  description: string
  status: string
  has_scripts?: boolean
  dept_enabled?: boolean
}

export function SkillsGlobalPage() {
  const user = useAuthStore((s) => s.user)
  const canManage = user?.role === 'dept_admin' || user?.role === 'ceo'
  const [skills, setSkills] = useState<GlobalSkill[]>([])
  // 启用集合视角色而定：员工=我的启用；dept_admin/ceo=团队开关；configured=false = 未配置（全部允许）
  const [enabledIds, setEnabledIds] = useState<number[]>([])
  const [configured, setConfigured] = useState(false)

  const load = () => {
    // 技能列表（含 status/dept_enabled）两个视图共用
    client.get('/skills/global-prefs').then((r) => setSkills(r.data.skills || [])).catch(() => {})
    if (canManage) {
      client.get('/skills/global-dept').then((r) => {
        setEnabledIds(r.data.enabled_ids || [])
        setConfigured(r.data.configured ?? false)
      }).catch(() => {})
    } else {
      client.get('/skills/global-prefs').then((r) => {
        setEnabledIds(r.data.enabled_ids || [])
        setConfigured(r.data.configured ?? false)
      }).catch(() => {})
    }
  }
  useEffect(load, [canManage])

  // 全部可用技能 id（运维 active 且团队允许）——"未配置=全部允许"的开关基准
  const allUsable = skills.filter((s) => s.status === 'active' && s.dept_enabled !== false).map((s) => s.id)
  const isOn = (s: GlobalSkill) => {
    if (s.status !== 'active' || s.dept_enabled === false) return false
    return configured ? enabledIds.includes(s.id) : true
  }
  const disabledReason = (s: GlobalSkill): string | null => {
    if (s.status !== 'active') return '已由运维停用'
    if (s.dept_enabled === false) return '本团队已关闭'
    return null
  }

  const toggle = async (s: GlobalSkill, on: boolean) => {
    // 未配置时从"全部允许"切为显式清单：以全部可用项为基准
    const base = configured ? enabledIds : allUsable
    const next = on ? [...base, s.id] : base.filter((id) => id !== s.id)
    setEnabledIds(next)
    setConfigured(true)
    try {
      if (canManage) {
        await client.put('/skills/global-dept', { enabled_ids: [...next].sort((a, b) => a - b) })
        message.success(on ? `本团队已启用「${s.name}」` : `本团队已停用「${s.name}」`)
      } else {
        await client.put('/skills/global-prefs', { enabled_ids: [...next].sort((a, b) => a - b) })
        message.success(on ? `已启用「${s.name}」` : `已停用「${s.name}」`)
      }
    } catch (e: any) {
      load()
      message.error(e.response?.data?.error?.message || '保存失败')
    }
  }

  const resetDept = async () => {
    try {
      await client.put('/skills/global-dept', { enabled_ids: null })
      setConfigured(false)
      setEnabledIds([])
      message.success('已恢复：本团队全部允许')
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '操作失败')
    }
  }

  return (
    <div>
      <BackLink to="/skills" label="返回" />
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>默认AI技能</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
        运维发布的全局技能（SKILL.md/技能包），全员可见；{canManage
          ? '团队管理员可整组开关本团队可用性（仅运维可上传/修改技能文件，约 1 分钟内生效）'
          : '你可自行决定启用哪些技能（运维停用或团队关闭的技能无法启用）'}
      </p>
      {canManage && (
        <div style={{ marginBottom: 12 }}>
          <button className="toolbar-btn" style={{ padding: '4px 14px' }} onClick={resetDept} disabled={!configured}>
            全部允许
          </button>
          <span style={{ fontSize: 11, color: 'var(--text-3)', marginLeft: 8 }}>{configured ? '当前为自定义清单' : '当前未配置=全部允许'}</span>
        </div>
      )}
      {skills.length === 0 ? (
        <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-3)', background: 'var(--surface-inset)', border: '1px dashed var(--border)' }}>
          <div style={{ fontSize: 32, marginBottom: 8 }}><Icon as={Globe} size={32} /></div>
          <div>暂无全局技能——由管理员在管理后台发布</div>
        </div>
      ) : (
        <div className="row-list">
          {skills.map((s) => {
            const reason = disabledReason(s)
            const denied = !!reason
            return (
              <div key={s.id} className={`row-item ${denied ? 'disabled' : ''}`}>
                <span className="row-icon"><Icon as={PuzzlePiece} size={17} /></span>
                <div className="row-main">
                  <div className="row-title">
                    {s.name}
                    {s.has_scripts && <span className="badge badge-blue" title="含脚本，agent 可用 run_script 执行">脚本</span>}
                    {reason && <span className="badge badge-gray">{reason}</span>}
                  </div>
                  <div className="row-desc" title={s.description}>{s.description}</div>
                </div>
                <span className="row-tail">
                  <Switch size="small" checked={isOn(s)} disabled={denied} onChange={(v) => toggle(s, v)} />
                </span>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default function SkillsPage() {
  const location = useLocation()
  // /skills 精确匹配 → 卡片首页；/skills/* 子路由 → 渲染子页
  if (location.pathname !== '/skills') return <Outlet />
  return <SkillsIndex />
}
