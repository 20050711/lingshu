// 管理后台 7 菜单页（真实数据，AntD 组件）
import { useEffect, useRef, useState } from 'react'
import { API_PREFIX } from '../../api/prefix'
import { Button, Collapse, Descriptions, Input, InputNumber, Popconfirm, Select, Switch, Table, Tabs, Tag, message } from 'antd'
import BackButton from '../../components/BackButton'
import DraggableModal from '../../components/DraggableModal'
import Icon, { iconFromKey } from '../../components/Icon'
import { Compass, GlobeSimple } from '@phosphor-icons/react'
import { adminApi } from '../../api/admin'
import { downloadByUrl } from '../../lib/download'

function useData<T>(fetcher: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  // loading 初始 true：避免首帧空态闪现（2026-08-12 组织页 0.3s 错误/空态修复）
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  // 2026-08-12：首次加载失败静默（返回 error 供页内展示重试态，避免进入页面弹 toast 闪现）；
  // 已有数据时刷新失败才弹 toast。
  const hasData = useRef(false)
  // M3（2026-08-12）：请求序号——并发 reload（初始未完成再 reload/连续 reload）时过期响应
  // 一律丢弃（原 last-write-wins：旧响应晚到覆盖新数据、旧失败误报 toast、旧 finally 清 loading）
  const seqRef = useRef(0)
  const load = () => {
    const seq = ++seqRef.current
    setLoading(true)
    fetcher()
      .then((d) => {
        if (seq !== seqRef.current) return
        setData(d); hasData.current = true; setError(false)
      })
      .catch(() => {
        if (seq !== seqRef.current) return
        setError(true)
        if (hasData.current) message.error('加载失败')
      })
      .finally(() => { if (seq === seqRef.current) setLoading(false) })
  }
  useEffect(load, [])
  return { data, loading, error, reload: load }
}

/* ===== 系统概览 ===== */
export function OverviewPage() {
  const { data, loading, error, reload } = useData(adminApi.overview)
  const m = data?.metrics
  // 2026-08-12：services 在响应顶层（非 metrics 内）——原从 m?.services 读 → undefined → 全部"未知"
  const svc = (data?.services ?? m?.services) as { db?: string; redis?: string; api?: string } | undefined
  // M7（2026-08-12）：渲染后端真实探活值（原硬编码「正常」；down 显示异常）
  const svcStatus = (v?: string) => (loading ? '...' : v === 'ok' ? '正常' : v === 'down' ? '异常' : '未知')
  const cards = [
    { label: '用户数', value: m?.user_count ?? '-' },
    { label: '活跃会话', value: m?.active_sessions ?? '-' },
    { label: '上次备份', value: m?.last_backup_at ? m.last_backup_at.slice(0, 16) : '无' },
  ]
  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>系统概览</h2>
      <div className="func-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)' }}>
        {cards.map((c) => (
          <div key={c.label} className="func-card">
            <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{c.label}</div>
            <div style={{ fontSize: 20, fontWeight: 600, marginTop: 6 }}>{c.value}</div>
          </div>
        ))}
      </div>
      <div className="card" style={{ padding: 16, marginTop: 16 }}>
        <Descriptions size="small" column={1} title="服务状态">
          <Descriptions.Item label="数据库">{svcStatus(svc?.db)}</Descriptions.Item>
          <Descriptions.Item label="Redis">{svcStatus(svc?.redis)}</Descriptions.Item>
          <Descriptions.Item label="API">{svcStatus(svc?.api)}</Descriptions.Item>
        </Descriptions>
        {error && (
          <div style={{ fontSize: 12, color: 'var(--error)', marginTop: 8 }}>
            概览加载失败
            <span style={{ color: 'var(--primary)', cursor: 'pointer', marginLeft: 6 }} onClick={reload}>点击重试</span>
          </div>
        )}
      </div>
    </div>
  )
}

/* ===== 用户管理 ===== */
export function UsersPage({ deptId, deptOptions }: { deptId?: string; deptOptions?: { value: string; label: string }[] }) {
  const { data, loading, error, reload } = useData(adminApi.users)
  const [newUser, setNewUser] = useState<any>(null)
  const [resetUser, setResetUser] = useState<any>(null)
  // 2026-09-01：用户级工具黑名单弹窗（技能工具细化到每个用户；勾选=禁用，与团队级并集生效）
  const [userToolsFor, setUserToolsFor] = useState<any>(null)
  const [userToolMeta, setUserToolMeta] = useState<any[]>([])
  const [userToolSel, setUserToolSel] = useState<string[]>([])
  const [savingUserTools, setSavingUserTools] = useState(false)
  // D7（2026-08-14）：会话记录查看（浮窗列该用户会话 → 点开看完整消息流）
  const [sessionsFor, setSessionsFor] = useState<any>(null)
  const [sessionList, setSessionList] = useState<any[]>([])
  const [viewingSess, setViewingSess] = useState<any>(null)
  const [viewingMsgs, setViewingMsgs] = useState<any[]>([])
  const openUserSessions = (row: any) => {
    setSessionsFor(row)
    setSessionList([])
    setViewingSess(null)
    adminApi.userSessions(row.id).then((r) => setSessionList(r.sessions || [])).catch(() => message.error('加载会话失败'))
  }
  const openSessionMsgs = (s: any) => {
    setViewingSess(s)
    setViewingMsgs([])
    adminApi.sessionMessages(s.id).then((r) => setViewingMsgs(r.messages || [])).catch(() => message.error('加载消息失败'))
  }
  // 可建账号的团队（排除运维管理 dept_root 与 CEO——CEO 为唯一预置账号，非普通团队）
  // 2026-08-12：由 DepartmentsPage 传入（原自建 useData 重复请求 /admin/departments，导致进入页面 3 个并发请求）
  // 合并页（团队管理内嵌）按团队过滤
  // #9（2026-08-12）：deptId 未选中（首帧 selected=null）时返回空——原 undefined 时展示
  // 全部用户，进入页面瞬间闪现所有团队用户
  const users = deptId ? (data?.users ?? []).filter((u: any) => u.dept_id === deptId) : []

  // 打开用户工具黑名单弹窗：加载工具元数据（含 run_script——用户级可控制）+ 该用户当前禁用配置
  // M2（2026-08-12）：目标校验——连点两个用户时，旧响应晚到不得覆盖新用户弹窗
  const userToolsTarget = useRef<number | null>(null)
  const openUserTools = (row: any) => {
    userToolsTarget.current = row.id
    setUserToolsFor(row)
    // 走查：原实现每个请求各自 `.catch(() => setXxx([]))`——加载失败时弹窗显示"全部未勾选"，
    // 而保存是整份覆盖（空勾选=传 null 清空该用户限制）⇒ 一次抖动就可能把配置改错。
    // 失败即不打开弹窗，宁可不改也不改错。
    Promise.all([adminApi.toolsMeta(), adminApi.userTools(row.id)]).then(([m, u]) => {
      if (userToolsTarget.current !== row.id) return
      setUserToolMeta(m.tools || [])
      setUserToolSel(u.tools ?? [])
    }).catch(() => {
      if (userToolsTarget.current !== row.id) return
      setUserToolsFor(null)
      message.error('工具权限加载失败，已取消打开（未做任何修改），请重试')
    })
  }
  const saveUserTools = () => {
    if (!userToolsFor) return
    setSavingUserTools(true)
    // 空勾选 = 无禁用（传 null 删除配置）
    adminApi.userToolsPut(userToolsFor.id, userToolSel.length ? userToolSel : null)
      .then(() => { message.success('用户工具权限已保存'); setUserToolsFor(null) })
      .catch(() => message.error('保存失败'))
      .finally(() => setSavingUserTools(false))
  }
  const toggleUserTool = (id: string, on: boolean) => {
    setUserToolSel(on ? [...userToolSel, id] : userToolSel.filter((x) => x !== id))
  }

  const roleTag = (r: string) => {
    const map: Record<string, [string, string]> = {
      admin: ['red', '运维管理'],
      ceo: ['blue', 'CEO'],
      dept_admin: ['purple', '团队管理员'],
      employee: ['green', '员工'],
    }
    const [color, label] = map[r] || ['default', r]
    return <Tag color={color}>{label}</Tag>
  }

  const columns = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    { title: '用户名', dataIndex: 'username' },
    { title: '角色', dataIndex: 'role', render: (r: string) => roleTag(r) },
    { title: '团队', dataIndex: 'dept_name' },
    { title: '状态', dataIndex: 'status', render: (s: string, row: any) => (
        // 预置账号（admin/ceo）禁用开关禁用——防止自锁系统（2026-08-06 事故修复）
        <Switch checked={s === 'active'} checkedChildren="启用" unCheckedChildren="禁用"
          disabled={row.role === 'admin' || row.role === 'ceo'}
          onChange={(v) => adminApi.updateUserStatus(row.id, v ? 'active' : 'disabled').then(reload).catch(() => message.error('操作失败'))} />
      ) },
    { title: '操作', render: (_: any, row: any) => (
        <span style={{ display: 'flex', gap: 6 }}>
          <Button size="small" onClick={() => setResetUser(row)}>重置密码</Button>
          {row.role !== 'admin' && (
            <Button size="small" onClick={() => openUserTools(row)} title="勾选=禁用，与团队黑名单并集生效">工具权限</Button>
          )}
          {/* D7（2026-08-14）：会话记录——浮窗看该用户会话与完整消息流（追踪日志用） */}
          <Button size="small" onClick={() => openUserSessions(row)}>会话记录</Button>
          {(row.role === 'employee' || row.role === 'dept_admin') && (
            <Button size="small" onClick={() => {
              adminApi.updateUserRole(row.id, row.role === 'dept_admin' ? 'employee' : 'dept_admin')
                .then(() => { message.success('角色已变更，该用户需重新登录生效'); reload() })
                .catch((e) => message.error(e.response?.data?.error?.message || '操作失败'))
            }}>{row.role === 'dept_admin' ? '取消管理员' : '设为团队管理员'}</Button>
          )}
          {/* 注销账号（2026-08-06）：预置账号（admin/ceo）不可注销 */}
          <Popconfirm
            title="注销该账号？"
            description={`将删除 ${row.username} 的账号与全部数据（会话/文件/记忆），不可恢复`}
            okText="注销"
            okButtonProps={{ danger: true }}
            disabled={row.role === 'admin' || row.role === 'ceo'}
            onConfirm={() => {
              adminApi.terminateUser(row.id)
                .then(() => { message.success('账号已注销'); reload() })
                .catch((e) => message.error(e.response?.data?.error?.message || '操作失败'))
            }}
          >
            <Button size="small" danger disabled={row.role === 'admin' || row.role === 'ceo'}>注销</Button>
          </Popconfirm>
        </span>
      ) },
  ]

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>用户管理</h2>
      <Button type="primary" size="small" style={{ marginBottom: 12 }} onClick={() => setNewUser(deptId ? { department_id: deptId } : {})}>+ 新增用户</Button>
      {!loading && error && (
        <div style={{ fontSize: 12, color: 'var(--error)', marginBottom: 8 }}>
          用户列表加载失败
          <span style={{ color: 'var(--primary)', cursor: 'pointer', marginLeft: 6 }} onClick={reload}>点击重试</span>
        </div>
      )}
      <Table rowKey="id" size="small" loading={loading} columns={columns} dataSource={users} pagination={{ pageSize: 10 }} />
      <DraggableModal open={!!newUser} title="新增用户" onCancel={() => setNewUser(null)} onOk={() => {
        if (!newUser.username || !newUser.password) return message.warning('请填写用户名和密码')
        // 2026-09-01：密码规则统一 8-16 位（与自助改密/reset-password 一致）
        if (newUser.password.length < 8 || newUser.password.length > 16) return message.warning('密码需 8-16 位')
        adminApi.createUser(newUser).then(() => { message.success('已创建'); setNewUser(null); reload() }).catch((e) => message.error(e.response?.data?.error?.message || e.response?.data?.detail?.message || '创建失败'))
      }}>
        <input className="login-input" placeholder="用户名" value={newUser?.username || ''} onChange={(e) => setNewUser({ ...newUser, username: e.target.value })} />
        {/* M6（2026-08-12）：密码明文 → password 输入 */}
        <input className="login-input" type="password" autoComplete="new-password" placeholder="密码" value={newUser?.password || ''} onChange={(e) => setNewUser({ ...newUser, password: e.target.value })} />
        <Select style={{ width: '100%', marginBottom: 8 }} placeholder="团队" value={newUser?.department_id || undefined}
          options={deptOptions} onChange={(v) => setNewUser({ ...newUser, department_id: v })} />
        <Select style={{ width: '100%' }} placeholder="角色" value={newUser?.role || undefined}
          options={[{ value: 'employee', label: '员工' }, { value: 'dept_admin', label: '团队管理员' }]}
          onChange={(v) => setNewUser({ ...newUser, role: v })} />
      </DraggableModal>
      <DraggableModal open={!!resetUser} title={`重置密码：${resetUser?.username}`} onCancel={() => setResetUser(null)} onOk={() => {
        if (!resetUser?.password) return message.warning('请填写新密码')
        // 2026-09-01：密码规则统一 8-16 位
        if (resetUser.password.length < 8 || resetUser.password.length > 16) return message.warning('密码需 8-16 位')
        adminApi.resetPassword(resetUser.id, resetUser.password).then(() => { message.success('已重置'); setResetUser(null) }).catch(() => message.error('操作失败'))
      }}>
        {/* M6（2026-08-12）：密码明文 → password 输入 */}
        <input className="login-input" type="password" autoComplete="new-password" placeholder="新密码" value={resetUser?.password || ''} onChange={(e) => setResetUser({ ...resetUser, password: e.target.value })} />
      </DraggableModal>
      {/* 2026-09-01：用户级工具黑名单弹窗（勾选=禁用；含 run_script 可禁；空勾选=无禁用） */}
      <DraggableModal open={!!userToolsFor} title={`工具权限：${userToolsFor?.username}（${userToolsFor?.dept_name}）`}
        confirmLoading={savingUserTools} onCancel={() => setUserToolsFor(null)} onOk={saveUserTools}>
        <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>
          勾选 = 禁用该用户 Agent 的工具（与团队黑名单并集，任一禁用即禁用）；<b>不勾选任何项并保存 = 无禁用（全部可用）</b>。
          run_script（沙盒脚本）也可在此禁用。
        </p>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
          {userToolMeta.map((t) => (
            <span key={t.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, border: '1px solid var(--border)', borderRadius: 16, padding: '3px 10px', fontSize: 12, cursor: 'pointer', userSelect: 'none', background: userToolSel.includes(t.id) ? 'var(--primary-bg)' : 'var(--surface-1)', color: userToolSel.includes(t.id) ? 'var(--primary)' : 'var(--text-1)' }}
              onClick={() => toggleUserTool(t.id, !userToolSel.includes(t.id))} title={t.summary || ''}>
              <Icon as={iconFromKey(t.icon)} /> {t.name}
            </span>
          ))}
        </div>
      </DraggableModal>
      {/* D7（2026-08-14）：会话记录浮窗——列该用户会话 → 点开会话看完整消息流 */}
      <DraggableModal open={!!sessionsFor} title={`会话记录：${sessionsFor?.username}（${sessionsFor?.dept_name}）`}
        footer={null} width={760} onCancel={() => { setSessionsFor(null); setViewingSess(null) }}>
        {!viewingSess ? (
          <div style={{ maxHeight: 480, overflow: 'auto' }}>
            {sessionList.length === 0 && <div style={{ padding: 16, fontSize: 12, color: 'var(--text-3)' }}>该用户暂无会话</div>}
            {sessionList.map((s) => (
              <div key={s.id} className="session-item" style={{ justifyContent: 'space-between' }}
                onClick={() => openSessionMsgs(s)}>
                <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {s.title || '（无标题）'}
                  <span className="badge badge-gray" style={{ marginLeft: 6, fontSize: 10 }}>{s.msg_count} 条</span>
                  {s.mode === 'complex' && <span className="badge badge-blue" style={{ marginLeft: 6, fontSize: 10 }}><Icon as={Compass} size={12} /></span>}
                </div>
                <span style={{ fontSize: 11, color: 'var(--text-3)' }}>{s.last_activity_at?.slice(0, 16) || ''}</span>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ maxHeight: 480, overflow: 'auto' }}>
            <div style={{ padding: '6px 0 10px', borderBottom: '1px solid var(--border)', marginBottom: 10, display: 'flex', justifyContent: 'space-between' }}>
              <BackButton label="返回会话列表" onClick={() => setViewingSess(null)} />
              <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{viewingSess.title || '（无标题）'} · {viewingMsgs.length} 条消息</span>
            </div>
            {viewingMsgs.length === 0 && <div style={{ padding: 16, fontSize: 12, color: 'var(--text-3)' }}>暂无消息</div>}
            {viewingMsgs.map((m: any, i: number) => (
              <div key={i} className="msg-bubble" style={{
                background: m.role === 'user' ? 'var(--primary)' : 'var(--surface-1)',
                color: m.role === 'user' ? 'var(--surface-1)' : 'var(--text-1)',
                border: m.role === 'user' ? 'none' : '1px solid var(--border)',
                marginBottom: 10, marginLeft: m.role === 'user' ? 'auto' : 0, maxWidth: '86%', width: 'fit-content',
                minWidth: 120,
              }}>
                <div style={{ fontSize: 11, opacity: 0.75, marginBottom: 4 }}>
                  {m.role === 'user' ? '用户' : 'AI'} · 第 {m.round_id ?? '-'} 轮
                  {m.created_at ? ` · ${m.created_at.slice(0, 16)}` : ''}
                </div>
                <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 13 }}>{m.content}</div>
              </div>
            ))}
          </div>
        )}
      </DraggableModal>
    </div>
  )
}

/* 2026-09-17：数据同步页（SyncPage）随数据查询线下线删除 */

/* ===== 个人记忆页签（四期重构：全员个人记忆 /admin/memory/users/*）===== */
function UserMemoryTab() {
  const users = useData(adminApi.users)
  const [userId, setUserId] = useState<number | null>(null)
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  // 添加记忆表单（AntD Modal，替代浏览器 prompt）
  const [addOpen, setAddOpen] = useState(false)
  const [addForm, setAddForm] = useState<{ mem_type: string; content: string }>({ mem_type: 'knowledge', content: '' })

  // M2（2026-08-12）：目标校验——快速切换用户时旧响应晚到不得覆盖新用户列表
  const loadTarget = useRef<number | null>(null)
  const load = (uid: number) => {
    loadTarget.current = uid
    setLoading(true)
    adminApi.memoryUserItems(uid)
      .then((d) => { if (loadTarget.current === uid) setItems(d.items ?? []) })
      .catch(() => { if (loadTarget.current === uid) message.error('加载失败') })
      .finally(() => { if (loadTarget.current === uid) setLoading(false) })
  }
  useEffect(() => {
    const first = (users.data?.users ?? [])[0]
    if (first) { setUserId(first.id); load(first.id) }
  }, [users.data])
  // 4.1：运维管理（admin）纯运维无个人记忆——下拉不显示，避免误配
  const userOptions = (users.data?.users ?? [])
    .filter((u: any) => u.role !== 'admin')
    .map((u: any) => ({ value: u.id, label: `${u.username}（${u.dept_name}）` }))

  const addMem = () => {
    if (!addForm.content.trim() || !userId) return
    adminApi.memoryUserAdd(userId, { mem_type: addForm.mem_type, content: addForm.content.trim() })
      .then(() => { message.success('已添加'); setAddOpen(false); setAddForm({ mem_type: 'knowledge', content: '' }); load(userId) })
      .catch(() => message.error('操作失败'))
  }
  const delMem = async (memId: number) => {
    // M5（2026-08-12）：失败不再 unhandled rejection——提示且不刷新
    try {
      await adminApi.memoryUserItemDelete(memId)
      message.success('已删除'); load(userId!)
    } catch {
      message.error('删除失败')
    }
  }

  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
        <Select style={{ width: 260 }} placeholder="选择用户" value={userId ?? undefined}
          options={userOptions} onChange={(v) => { setUserId(v); load(v) }} />
        <Button size="small" type="primary" onClick={() => setAddOpen(true)} disabled={!userId}>+ 添加记忆</Button>
      </div>
      <Table size="small" rowKey="id" loading={loading} dataSource={items} pagination={{ pageSize: 10 }}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 60 },
          { title: '类型', dataIndex: 'mem_type', width: 100, render: (v: string) => (
            <Tag color={v === 'profile' ? 'blue' : v === 'workflow' ? 'purple' : 'green'}>
              {v === 'profile' ? '画像' : v === 'workflow' ? '流程' : '知识'}
            </Tag>
          ) },
          { title: '内容', dataIndex: 'content', ellipsis: true },
          { title: '创建时间', dataIndex: 'created_at', width: 150, render: (v: string) => v ? v.slice(0, 16) : '-' },
          { title: '操作', width: 80, render: (_: any, r: any) => (
            <Popconfirm title="确认删除该个人记忆？" onConfirm={() => delMem(r.id)}>
              <Button size="small" type="link" danger>删除</Button>
            </Popconfirm>
          ) },
        ]}
      />
      <DraggableModal open={addOpen} title={`添加个人记忆：${userOptions.find((u: any) => u.value === userId)?.label ?? ''}`}
        onCancel={() => setAddOpen(false)} onOk={addMem} okText="添加">
        <div style={{ marginBottom: 8 }}>
          <Select style={{ width: 160 }} value={addForm.mem_type}
            options={[{ value: 'knowledge', label: '知识' }, { value: 'profile', label: '画像' }, { value: 'workflow', label: '流程' }]}
            onChange={(v) => setAddForm((s) => ({ ...s, mem_type: v }))} />
        </div>
        <Input.TextArea rows={3} placeholder="记忆内容" value={addForm.content}
          onChange={(e) => setAddForm((s) => ({ ...s, content: e.target.value }))} />
      </DraggableModal>
    </div>
  )
}

/* ===== 记忆库管理（二期 M9 + 四期重构：团队共识 + 个人记忆 + 阈值配置）===== */
export function MemoryPage() {
  const [items, setItems] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [thresholds, setThresholds] = useState<any>(null)
  const [editContent, setEditContent] = useState<{ id: number; content: string } | null>(null)
  // 阈值配置 Modal（替代浏览器 prompt）
  const [thOpen, setThOpen] = useState(false)
  const [thForm, setThForm] = useState<any>(null)

  const load = () => {
    setLoading(true)
    adminApi.memory()
      .then((d) => setItems(d.items))
      .catch(() => message.error('加载失败'))
      .finally(() => setLoading(false))
    adminApi.memoryThresholds().then((d) => setThresholds(d.thresholds)).catch(() => {})
  }
  useEffect(load, [])

  // M5（2026-08-12）：删除/激活/编辑失败统一提示，不再 unhandled rejection
  const activate = async (id: number) => {
    try {
      await adminApi.memoryActivate(id)
      message.success('已激活'); load()
    } catch { message.error('激活失败') }
  }
  const del = async (id: number) => {
    try {
      await adminApi.memoryDelete(id)
      message.success('已删除'); load()
    } catch { message.error('删除失败') }
  }
  const saveEdit = async () => {
    if (!editContent) return
    try {
      await adminApi.memoryEdit(editContent.id, editContent.content)
      message.success('已保存'); setEditContent(null); load()
    } catch { message.error('保存失败') }
  }
  const exportJson = async () => {
    const blob = await adminApi.memoryExport()
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob); a.download = 'memory_export.json'; a.click()
    // C8（2026-08-12）：延迟 revoke
    setTimeout(() => URL.revokeObjectURL(a.href), 1000)
  }

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>记忆库管理</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>团队共识记忆（候选-激活） / 全员个人记忆 / 共识阈值配置</p>

      <Tabs
        items={[
          {
            key: 'dept',
            label: '团队共识记忆',
            children: (<>
              {thresholds && (
                <div className="card" style={{ padding: 16, marginBottom: 16, fontSize: 12 }}>
                  <strong>共识阈值：</strong>
                  <span style={{ marginLeft: 8 }}>广度 ≥{thresholds.breadth_min_clients} 端 · 强度 ≥{thresholds.strength_min_rounds} 轮/{thresholds.strength_min_clients} 端 · 窗口 {thresholds.window_days} 天 · 候选保留 {thresholds.candidate_ttl_days} 天</span>
                  <Button size="small" type="link" onClick={() => { setThForm({ ...thresholds }); setThOpen(true) }}>调整阈值</Button>
                  <Button size="small" type="link" onClick={exportJson}>导出 JSON</Button>
                </div>
              )}
              <div className="card" style={{ padding: 16 }}>
                <Table
                  size="small" rowKey="id" loading={loading} dataSource={items} pagination={{ pageSize: 10 }}
                  columns={[
                    { title: '状态', dataIndex: 'status', width: 80, render: (v: string) => (
                      <Tag color={v === 'active' ? 'green' : v === 'candidate' ? 'orange' : 'default'}>{v === 'candidate' ? '候选' : v === 'active' ? '已激活' : '已替换'}</Tag>
                    ) },
                    { title: '团队', dataIndex: 'dept_id', width: 80 },
                    { title: '内容', dataIndex: 'content', ellipsis: true },
                    { title: '提出端数', dataIndex: 'client_ids', width: 80, render: (v: string[]) => v?.length ?? 0 },
                    { title: '轮次', dataIndex: 'round_count', width: 60 },
                    { title: '激活时间', dataIndex: 'activated_at', width: 130, render: (v: string) => v ? v.slice(0, 16) : '-' },
                    { title: '操作', width: 200, render: (_: any, r: any) => (
                      <>
                        {r.status === 'candidate' && <Button size="small" type="link" onClick={() => activate(r.id)}>强制激活</Button>}
                        <Button size="small" type="link" onClick={() => setEditContent({ id: r.id, content: r.content })}>编辑</Button>
                        {/* M8（2026-08-12）：破坏性操作加确认（与其他删除一致） */}
                        <Popconfirm title="确认删除该条共识记忆？" onConfirm={() => del(r.id)}>
                          <Button size="small" type="link" danger>删除</Button>
                        </Popconfirm>
                      </>
                    ) },
                  ]}
                />
              </div>
            </>),
          },
          { key: 'user', label: '个人记忆', children: <UserMemoryTab /> },
        ]}
      />

      <DraggableModal open={!!editContent} title="编辑记忆内容" onOk={saveEdit} onCancel={() => setEditContent(null)}>
        <Input.TextArea rows={4} value={editContent?.content} onChange={(e) => setEditContent((v) => v ? { ...v, content: e.target.value } : v)} />
      </DraggableModal>
      <DraggableModal open={thOpen} title="共识阈值配置" onCancel={() => setThOpen(false)} onOk={() => {
        if (!thForm) return
        adminApi.memoryThresholdsPut({
          breadth_min_clients: Number(thForm.breadth_min_clients),
          strength_min_rounds: Number(thForm.strength_min_rounds),
          strength_min_clients: Number(thForm.strength_min_clients),
          window_days: Number(thForm.window_days),
          candidate_ttl_days: Number(thForm.candidate_ttl_days),
        }).then(() => { message.success('阈值已更新'); setThOpen(false); load() }).catch(() => message.error('保存失败'))
      }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {[
            ['breadth_min_clients', '广度共识（不同客户端数 ≥）'],
            ['strength_min_rounds', '强度共识（累计轮次 ≥）'],
            ['strength_min_clients', '强度共识（客户端数 ≥）'],
            ['window_days', '激活窗口（天内）'],
            ['candidate_ttl_days', '候选保留（天）'],
          ].map(([key, label]) => (
            <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <span style={{ width: 190, fontSize: 12, color: 'var(--text-2)' }}>{label}</span>
              <input className="login-input" style={{ width: 100, marginBottom: 0 }} type="number" min={1}
                value={thForm?.[key] ?? ''} onChange={(e) => setThForm((v: any) => ({ ...v, [key]: e.target.value }))} />
            </div>
          ))}
        </div>
      </DraggableModal>
    </div>
  )
}

/* ===== 配置管理（四期重构：运维友好表单，不展示 JSON key；B4：模型分层）===== */
const ROLE_NAME: Record<string, string> = { employee: '员工', ceo: 'CEO' }
// B4：模型分层栏（LLM 对话 / LLM 辅助任务 / 定制化工具 / 视觉识别 / 图片生成 / 视频生成）
// 2026-09-02：llm_tools=定制化工具独立档（video/resume/meeting）
const MODEL_KINDS = ['llm', 'llm_aux', 'llm_tools', 'vision', 'image', 'video'] as const

export function ConfigPage() {
  const configs = useData(adminApi.configs)
  // 2026-09-16：上传大小限制（动态配置）——草稿只存改动项，保存即生效
  const upLimits = useData<any>(adminApi.uploadLimits)
  const [limitDraft, setLimitDraft] = useState<Record<string, number>>({})
  const [limitSaving, setLimitSaving] = useState(false)
  const saveLimits = () => {
    const changed = Object.entries(limitDraft).filter(([k, v]) => {
      const it = (upLimits.data?.items ?? []).find((x: any) => x.key === k)
      return it && v !== it.value
    })
    if (!changed.length) { message.info('没有改动'); return }
    setLimitSaving(true)
    adminApi.updateUploadLimits(Object.fromEntries(changed))
      .then(() => { message.success('已保存，立即生效'); setLimitDraft({}); upLimits.reload() })
      .catch((e: any) => message.error(e?.response?.data?.error?.message ?? '保存失败'))
      .finally(() => setLimitSaving(false))
  }
  // 4.1：团队下拉改用 deptOptions（全量含 CEO——deptList 的 dept_stats 会跳过 ceo）
  const depts = useData(adminApi.deptOptions)
  // B4：模型候选目录（后端维护——未来加厂商/模型不改前端）
  const catalogData = useData(adminApi.modelCatalog)
  // 模型分层表单（五段结构 llm/llm_aux/vision/image/video，各含 platform/model；llm/llm_aux 另有 effort+thinking（三选项 关/低/高 2026-08-12），llm_aux 另有 usage 使用方向=任务下拉）
  const [layer, setLayer] = useState<{
    dept_id: string
    role: string
    llm: { platform: string; model: string; effort: string; thinking: boolean }
    llm_aux: { platform: string; model: string; effort: string; usage: string[]; thinking: boolean }
    llm_tools: { platform: string; model: string; effort: string; usage: string[]; thinking: boolean }
    vision: { platform: string; model: string }
    image: { platform: string; model: string }
    video: { platform: string; model: string }
  }>({
    dept_id: '', role: 'employee',
    llm: { platform: '', model: '', effort: 'high', thinking: true },
    llm_aux: { platform: '', model: '', effort: 'high', usage: [], thinking: true },
    llm_tools: { platform: '', model: '', effort: 'high', usage: [], thinking: true },
    vision: { platform: '', model: '' },
    image: { platform: '', model: '' },
    video: { platform: '', model: '' },
  })
  // 高级参数编辑（未知 key，折叠区）
  const [editKey, setEditKey] = useState<string | null>(null)
  const [editVal, setEditVal] = useState('')

  const deptOptions = [
    { value: '', label: '默认（全局）' },
    ...(depts.data?.departments ?? [])
      .filter((d: any) => d.dept_id !== 'dept_root')
      .map((d: any) => ({ value: d.dept_id, label: d.name })),
  ]
  const deptName = (id: string) => (depts.data?.departments ?? []).find((d: any) => d.dept_id === id)?.name || id
  // 角色按团队动态：CEO 团队只有 CEO；其他团队为员工/团队管理员/管理员和员工（2026-09-01：
  // both=一次配置同时写入 employee+dept_admin 两段，避免两段不一致）
  const isCeoDept = layer.dept_id === 'ceo'
  // 2026-09-04（用户要求）：默认常用「管理员和员工」放第一位
  const roleOptions = isCeoDept
    ? [{ value: 'ceo', label: 'CEO' }]
    : [{ value: 'both', label: '管理员和员工' }, { value: 'employee', label: '员工' }, { value: 'dept_admin', label: '团队管理员' }]

  // B4：候选目录解析（catalog: {llm: [{platform, models}], vision: [...], image: [...]}）
  const catalog: Record<string, { platform: string; models: string[] }[]> = catalogData.data?.catalog || {}
  const kindLabels: Record<string, string> = catalogData.data?.labels || {
    llm: 'LLM 对话', vision: '图片识别模型', image: '图片生成',
  }
  // 2026-08-17（需求 2：配置下拉重构）——模型下拉跨平台分组（不单独选平台）；
  // 「不写默认模型」：加载时直接选中当前配置值（loadLayer 已解析），换模型反查平台联动
  const modelOptions = (kind: string) =>
    (catalog[kind] || []).flatMap((p: any) =>
      (p.models || []).map((m: string) => ({ value: m, platform: p.platform, label: `${m}（${p.platform}）` })))
  const platformOf = (kind: string, model: string) =>
    modelOptions(kind).find((o) => o.value === model)?.platform || ''
  // 该模型是否有思考下拉（deepseek 文本模型支持官方三档 low/high/max + 关闭；其余无思考概念）
  const hasThinking = (kind: string, model: string, platform: string) =>
    (kind === 'llm' || kind === 'llm_aux' || kind === 'llm_tools') && platform === 'deepseek' && !!model
  // B4.2：任务"使用方向"下拉选项（后端下发，key 与消费侧任务 key 一致；group 2026-09-02 拆分）
  // aux=纯辅助任务（llm_aux 栏）；tools=定制化工具任务（llm_tools 独立档栏）
  const auxTaskOptions = (catalogData.data?.aux_tasks || [])
    .filter((t: any) => t.group !== 'tools')
    .map((t: any) => ({ value: t.key, label: t.label }))
  const toolsTaskOptions = (catalogData.data?.aux_tasks || [])
    .filter((t: any) => t.group === 'tools')
    .map((t: any) => ({ value: t.key, label: t.label }))

  // 旧结构兼容：seg 直接含 model 键（{"model","effort"}）视为 llm 段
  // llm_aux.usage 使用方向：JSON 数组或 JSON 数组字符串（旧自由文本 → 空数组）
  const parseUsage = (u: any): string[] => {
    if (Array.isArray(u)) return u.map(String)
    if (typeof u === 'string') {
      try { const arr = JSON.parse(u); return Array.isArray(arr) ? arr.map(String) : [] } catch { return [] }
    }
    return []
  }
  const parseSeg = (seg: any) => {
    const base = { platform: '', model: '', effort: 'high' }
    const auxBase = { ...base, usage: [] as string[], thinking: true }
    if (!seg || typeof seg !== 'object') return {
      llm: { ...base, thinking: true }, llm_aux: { ...auxBase }, llm_tools: { ...auxBase },
      vision: { platform: '', model: '' }, image: { platform: '', model: '' }, video: { platform: '', model: '' },
    }
    // 旧结构分支需透传 thinking（2026-08-13 回归保护发现：原分支丢 thinking → model_layer.ceo
    // 等旧结构段 DB 里 thinking:false 时 UI 却显示「高」，且一旦保存会把 thinking 覆写成 true）
    const llm = seg.llm || (seg.model ? { platform: seg.platform || '', model: seg.model, effort: seg.effort || 'high', thinking: seg.thinking } : { ...base })
    return {
      // thinking：缺省（undefined）视为开启（保持现状）；显式 false 才关闭
      llm: { platform: llm.platform || '', model: llm.model || '', effort: llm.effort || 'high', thinking: llm.thinking !== false },
      llm_aux: { platform: seg.llm_aux?.platform || '', model: seg.llm_aux?.model || '', effort: seg.llm_aux?.effort || 'high', usage: parseUsage(seg.llm_aux?.usage), thinking: seg.llm_aux?.thinking !== false },
      llm_tools: { platform: seg.llm_tools?.platform || '', model: seg.llm_tools?.model || '', effort: seg.llm_tools?.effort || 'high', usage: parseUsage(seg.llm_tools?.usage), thinking: seg.llm_tools?.thinking !== false },
      vision: { platform: seg.vision?.platform || '', model: seg.vision?.model || '' },
      image: { platform: seg.image?.platform || '', model: seg.image?.model || '' },
      video: { platform: seg.video?.platform || '', model: seg.video?.model || '' },
    }
  }

  // 思考强度四选项（2026-08-17 官方三档适配：effort low/high/max + 关闭开关）：
  // 关=thinking:false；低=effort:low；中=effort:high；高=effort:max（均 thinking:true）
  const thinkMode = (seg: { thinking?: boolean; effort?: string }): 'off' | 'low' | 'mid' | 'high' =>
    seg.thinking === false ? 'off' : (seg.effort === 'max' ? 'high' : seg.effort === 'low' ? 'low' : 'mid')
  const THINK_OPTIONS = [
    { value: 'off', label: '关（不思考）' },
    { value: 'low', label: '低' },
    { value: 'mid', label: '中' },
    { value: 'high', label: '高（深度思考）' },
  ]
  // 思考档 → model_layer 配置（effort 官方三档 low/high/max）
  const applyThink = (kind: 'llm' | 'llm_aux' | 'llm_tools', mode: string) =>
    setLayer((s) => ({ ...s, [kind]: {
      ...s[kind],
      thinking: mode !== 'off',
      effort: mode === 'high' ? 'max' : (mode === 'low' ? 'low' : 'high'),
    } } as any))

  // C3（E-04）：角色参数化——切换角色重新加载该角色的档值
  // （原切换角色只 set role 不改段值：看到并保存的全是 employee 档 → 团队管理员档被 employee 值整体覆盖）
  const loadLayer = (deptId: string, roleOverride?: string) => {
    const key = deptId ? `model_layer.${deptId}` : 'model_layer.default'
    const role = roleOverride ?? (deptId === 'ceo' ? 'ceo' : 'employee')
    const row = (configs.data?.configs ?? []).find((c: any) => c.key === key)
    if (row) {
      try {
        const d = JSON.parse(row.value)
        // 2026-09-01：both 角色读 employee 段（保存时两段同写，展示以 employee 为准）
        const segRole = role === 'both' ? 'employee' : role
        setLayer((v) => ({ ...v, dept_id: deptId, role, ...parseSeg(d[segRole]) }))
        return
      } catch { /* 非法 JSON 走空 */ }
    }
    setLayer((v) => ({ ...v, dept_id: deptId, role, ...parseSeg(null) }))
  }

  // 2026-08-13 回归保护发现：原表单首次进入是空壳（loadLayer 只在切换团队/角色时触发），
  // 空壳下若直接选模型保存，saveKind 会以空 roleSeg 覆盖 model_layer.default 整段（丢失其他四栏）。
  // 修复：configs 加载完成后立即加载当前选择段的档值；保存后 reload 触发同 effect 回显最新值。
  // 2026-09-02（bug）：原只传 dept_id——保存后 reload 回显把 role 重置成默认「员工」，
  // 用户选好的团队管理员/管理员和员工被改掉；改为带当前 role 回显（持久化用户所选角色）
  useEffect(() => {
    if (configs.data) loadLayer(layer.dept_id, layer.role)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configs.data])

  // B4：每栏独立保存——只写该 kind 段（merge 保留其他段，防互相覆盖）
  // M1（2026-08-12）：保存串行化——原每次先 GET 全量再 PUT，快速连点两栏时两次 GET
  // 都基于旧快照，后写整体覆盖先保存的栏（互相吞）。promise 链保证后一次保存基于
  // 前一次完成后的全量（GET 时已含上次改动）。
  let saveChain: Promise<unknown> = Promise.resolve()
  const saveKind = (kind: 'llm' | 'llm_aux' | 'llm_tools' | 'vision' | 'image' | 'video') => {
    const seg = layer[kind] as { platform: string; model: string }
    if (!seg.model.trim()) return message.warning('请选择模型')
    const deptId = layer.dept_id
    const role = layer.role
    saveChain = saveChain
      .then(() => adminApi.configs())
      .then((all) => {
        const key = deptId ? `model_layer.${deptId}` : 'model_layer.default'
        const row = (all.configs ?? []).find((c: any) => c.key === key)
        let data: any = {}
        if (row) { try { data = JSON.parse(row.value) } catch { data = {} } }
        // 2026-09-01：both=管理员和员工——同时写入 employee+dept_admin 两段（防两段不一致）
        const roles = role === 'both' ? ['employee', 'dept_admin'] : [role]
        for (const r of roles) {
          const roleSeg = { ...(data[r] || {}) }
          roleSeg[kind] = seg
          data[r] = roleSeg
        }
        return adminApi.updateConfig(key, JSON.stringify(data))
      })
      .then(() => { message.success(`${kindLabels[kind]}已保存（≤60s 生效）`); configs.reload() })
      .catch(() => message.error('保存失败'))
  }

  // 友好化解析（运维无需看懂 JSON；B4 四段结构；旧结构 model 键兼容为 llm）
  const parseLayerText = (raw: string) => {
    try {
      const d = JSON.parse(raw)
      const segs = Object.entries(d).map(([r, v]: [string, any]) => {
        const llm = v?.llm?.model || v?.model || '-'
        const aux = v?.llm_aux?.model || '-'
        const tools = v?.llm_tools?.model || '-'
        const vision = v?.vision?.model || '-'
        const image = v?.image?.model || '-'
        const video = v?.video?.model || '-'
        return `${ROLE_NAME[r] || r}: 对话 ${llm} / 辅助 ${aux} / 工具 ${tools} / 视觉 ${vision} / 生成 ${image} / 视频 ${video}`
      })
      return segs.length ? segs.join('；') : '未配置'
    } catch { return '（解析失败）' }
  }
  const parseToolsText = (raw: string) => {
    try {
      const list = JSON.parse(raw)
      return Array.isArray(list) && list.length > 0 ? list.join('、') : '全部不限制'
    } catch { return '（解析失败）' }
  }
  // 2026-09-01：黑名单改造后旧 key（dept_tools.*/custom_tools.*）作废——不再进入 knownRows 友好展示，
  // 落 otherRows 高级区（显示原始 key，运维可手动清理）；knownRows 只展示新 key（dept_block/custom_block）
  const knownRows = (configs.data?.configs ?? []).filter((c: any) => c.key.startsWith('model_layer') || c.key.startsWith('dept_block') || c.key.startsWith('custom_block'))
  const otherRows = (configs.data?.configs ?? []).filter((c: any) => !c.key.startsWith('model_layer') && !c.key.startsWith('dept_block') && !c.key.startsWith('custom_block'))
  // SEC-02：沙盒联网开关（system_config sandbox_net_enabled；默认断网，admin 在线可改，60s 生效）
  const sandboxNet = (configs.data?.configs ?? []).find((c: any) => c.key === 'sandbox_net_enabled')?.value === 'true'

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>配置管理</h2>
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 4 }}>模型分层</h4>
        <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 10 }}>按「团队 × 角色」分别配置各栏模型（LLM 对话 / LLM 辅助任务 / 定制化工具模型配置 / 视觉识别 / 图片生成 / 视频生成）；未单独配置的团队使用「默认（全局）」。视频理解/简历/会议纪要等定制化工具的模型在「定制化工具模型配置」栏勾选（未配置该栏时沿用「LLM 辅助任务」栏模型）</p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', marginBottom: 12 }}>
          <Select style={{ width: 140 }} placeholder="团队" value={layer.dept_id}
            options={deptOptions} onChange={(v) => loadLayer(String(v))} />
          <Select style={{ width: 130 }} value={layer.role}
            options={roleOptions}
            onChange={(v) => loadLayer(layer.dept_id, v)} />
          {/* 2026-09-08 开发机专用：一键免费档（vite.config define __DEV_FREE_TOGGLE__；部署构建=0 → 死代码剔除）
              模型值由后端 /admin/model-catalog 的 free 字段下发，前端不硬编码 */}
          {__DEV_FREE_TOGGLE__ && !!(catalogData.data as any)?.free && (
            <button
              className="toolbar-btn"
              onClick={() => {
                const f = { platform: (catalogData.data as any).free.platform, model: (catalogData.data as any).free.model }
                setLayer((s) => ({
                  ...s,
                  llm: { ...f, effort: 'high', thinking: true },
                  llm_aux: { ...s.llm_aux, ...f, effort: 'low' },
                  llm_tools: { ...s.llm_tools, ...f, effort: 'low' },
                  vision: { ...f },  // agnes-3.0-flash 支持图像 URL 输入（图片识别同用）
                } as any))
              }}
            >
              一键免费档（{(catalogData.data as any)?.free?.model}）
            </button>
          )}
        </div>
        {MODEL_KINDS.map((kind) => {
          const seg = layer[kind] as { platform: string; model: string; effort?: string; usage?: string[]; thinking?: boolean }
          const opts = modelOptions(kind)
          const thinkable = hasThinking(kind, seg.model, seg.platform)
          return (
            <div key={kind} style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, marginBottom: 10 }}>
              <div style={{ fontWeight: 600, marginBottom: 8, fontSize: 13 }}>{kindLabels[kind] || kind}</div>
              {/* 2026-08-17（需求 2 重构）：第一个下拉=模型（跨平台分组，不写默认占位——加载即选中当前值）；
                  第二个下拉=思考强度（按模型能力显示：deepseek 文本模型三档+关，其余无思考） */}
              <div style={{ display: 'flex', gap: 10, rowGap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                <Select style={{ width: 260 }} value={seg.model || undefined} showSearch optionFilterProp="label"
                  options={opts.map((o) => ({ value: o.value, label: o.label }))}
                  onChange={(v) => {
                    const plat = platformOf(kind, v)
                    setLayer((s) => ({ ...s, [kind]: { ...s[kind], platform: plat, model: v } } as any))
                  }} />
                {thinkable && (
                  <>
                    <Select style={{ width: 150 }} value={thinkMode(seg)}
                      options={THINK_OPTIONS}
                      onChange={(v) => applyThink(kind as 'llm' | 'llm_aux', v)} />
                    <span style={{ fontSize: 12, color: 'var(--text-3)' }}>思考强度（官方三档 low/high/max + 关闭；工具调用不受思考开关影响）</span>
                  </>
                )}
                {!thinkable && seg.model && (
                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>该模型无思考强度配置</span>
                )}
                {(kind === 'llm_aux' || kind === 'llm_tools') && (
                  <Select
                    mode="multiple"
                    style={{ minWidth: 380, flex: 1 }}
                    placeholder={kind === 'llm_tools'
                      ? '使用方向（勾选该模型负责的定制化工具；不勾选=全部工具任务）'
                      : '使用方向（勾选该模型负责的辅助任务；不勾选=全部任务）'}
                    value={((seg.usage as string[]) || []).filter((k) =>
                      (kind === 'llm_tools' ? toolsTaskOptions : auxTaskOptions).some((o: { value: string }) => o.value === k))}
                    options={kind === 'llm_tools' ? toolsTaskOptions : auxTaskOptions}
                    onChange={(v: string[]) => {
                      const tk = kind as 'llm_aux' | 'llm_tools'
                      setLayer((s) => {
                        const cur = s[tk]
                        // 2026-09-02：llm_aux 栏保存只改辅助任务勾选——保留存量工具任务勾选
                        // （团队迁移到 llm_tools 独立档前，回退链 llm_aux.usage 仍按原语义生效）
                        const toolsHeld = tk === 'llm_aux'
                          ? ((cur.usage as string[]) || []).filter((k) => toolsTaskOptions.some((o: { value: string }) => o.value === k))
                          : []
                        return { ...s, [tk]: { ...cur, usage: [...toolsHeld, ...v] } }
                      })
                    }}
                  />
                )}
                <Button size="small" type="primary" onClick={() => saveKind(kind)}>保存</Button>
              </div>
            </div>
          )
        })}
      </div>
      {/* 上传大小限制（2026-09-16）：动态配置，管理端一处维护；项名/说明由后端下发，前端不硬编码 */}
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 4 }}>上传大小限制</h4>
        <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 10 }}>
          各处上传文件的最大体积，单位 MB（15360 = 15GB）。改完点保存<b>立即生效</b>，无需重启；正在上传的任务不受影响。
        </p>
        {(upLimits.data?.items ?? []).map((it: any) => {
          const val = limitDraft[it.key] ?? it.value
          return (
            <div key={it.key} style={{ display: 'flex', gap: 12, alignItems: 'flex-start', padding: '8px 0', borderTop: '1px solid var(--border)' }}>
              <div style={{ flex: 1, minWidth: 260 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{it.name}</div>
                <div style={{ fontSize: 12, color: 'var(--text-3)' }}>{it.desc}</div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                <InputNumber min={1} max={it.max} value={val} style={{ width: 150 }}
                  addonAfter="MB" step={100}
                  onChange={(v) => setLimitDraft((s) => ({ ...s, [it.key]: Number(v) || 0 }))} />
                <span style={{ fontSize: 12, color: 'var(--text-2)', width: 78 }}>
                  {val >= 1024 ? `≈ ${(val / 1024).toFixed(val % 1024 ? 1 : 0)} GB` : `≈ ${val} MB`}
                </span>
              </div>
            </div>
          )
        })}
        <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
          <Button type="primary" loading={limitSaving} onClick={saveLimits}
            disabled={!Object.keys(limitDraft).length}>保存</Button>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>
            {upLimits.loading ? '加载中…' : '所有页面（问答 / 知识库 / 音视频 / 会议 / 工具包）共用这一处配置'}
          </span>
        </div>
      </div>
      {/* SEC-02：沙盒联网开关（system_config，admin 在线可改，60s 生效） */}
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 4 }}>系统开关</h4>
        <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginTop: 8 }}>
          <Switch
            checked={sandboxNet}
            onChange={(v) => {
              adminApi.updateConfig('sandbox_net_enabled', v ? 'true' : 'false')
                .then(() => { message.success(`沙盒联网已${v ? '开启' : '关闭'}（≤60s 生效）`); configs.reload() })
                .catch(() => message.error('保存失败'))
            }}
          />
          <span style={{ fontSize: 13, color: 'var(--text-2)' }}>沙盒联网（run_script 可访问网络；默认关闭——安全边界，仅调试/明确需要联网的场景开启）</span>
        </div>
      </div>
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 8 }}>当前配置</h4>
        <Table rowKey="key" size="small" loading={configs.loading} pagination={false}
          columns={[
            { title: '配置项', dataIndex: 'name', width: 180 },
            { title: '当前值', dataIndex: 'valueText', ellipsis: true },
            { title: '说明', dataIndex: 'desc', ellipsis: true },
          ]}
          dataSource={knownRows.map((c: any) => {
            if (c.key.startsWith('model_layer')) {
              const deptId = c.key.replace('model_layer.', '')
              return {
                key: c.key,
                // M10（2026-08-12）：model_layer.default 时 deptName('default') 查无显示原 id → 显示「默认（全局）」
                name: `模型分层 · ${deptId && deptId !== 'default' ? deptName(deptId) : '默认（全局）'}`,
                valueText: parseLayerText(c.value),
                desc: '在上方表单中调整',
              }
            }
            if (c.key.startsWith('custom_block')) {
              const deptId = c.key.replace('custom_block.', '')
              return {
                key: c.key,
                name: `定制化工具禁用（黑名单） · ${deptName(deptId)}`,
                valueText: parseToolsText(c.value),
                desc: '在「组织与用户 → 技能/工具权限」中调整',
              }
            }
            const deptId = c.key.replace('dept_block.', '')
            return {
              key: c.key,
              name: `AI技能禁用（黑名单） · ${deptName(deptId)}`,
              valueText: parseToolsText(c.value),
              desc: '在「组织与用户 → 技能/工具权限」中调整',
            }
          })} />
      </div>
      <div className="card" style={{ padding: 16 }}>
        <Collapse ghost items={[{
          key: 'advanced',
          label: <span style={{ fontSize: 13, color: 'var(--text-2)' }}>其他参数（高级，请勿随意修改）</span>,
          children: (
            <Table rowKey="key" size="small" pagination={false}
              columns={[
                { title: '参数', dataIndex: 'key' },
                { title: '值', dataIndex: 'value', ellipsis: true },
                { title: '说明', dataIndex: 'description' },
                { title: '操作', width: 80, render: (_: any, row: any) => (
                    <Button size="small" onClick={() => { setEditKey(row.key); setEditVal(row.value || '') }}>编辑</Button>
                  ) },
              ]} dataSource={otherRows} />
          ),
        }]} />
      </div>
      <DraggableModal open={!!editKey} title={`编辑参数：${editKey}`} onCancel={() => setEditKey(null)} onOk={() => {
        if (!editKey) return
        adminApi.updateConfig(editKey, editVal).then(() => { message.success('已保存'); setEditKey(null); configs.reload() }).catch(() => message.error('保存失败'))
      }}>
        <textarea className="login-input" rows={5} value={editVal} onChange={(e) => setEditVal(e.target.value)} />
      </DraggableModal>
    </div>
  )
}

/* ===== 用户反馈管理（独立侧边栏页：细化到用户 / 点击行详情浮窗 / 操作列两按钮处理）===== */
export function FeedbackAdminPage() {
  const feedback = useData(adminApi.feedback)
  const [filter, setFilter] = useState('all')
  const [detail, setDetail] = useState<any | null>(null)        // 详情浮窗（点击行）
  const [fbReply, setFbReply] = useState<{ id: number; content: string } | null>(null)
  const [shotUrls, setShotUrls] = useState<Record<string, string>>({})
  // L12/N5：卸载时 revoke 全部截图 blob（原只增不减，内存持续增长）
  const shotUrlsRef = useRef(shotUrls)
  shotUrlsRef.current = shotUrls
  useEffect(() => {
    return () => { Object.values(shotUrlsRef.current).forEach((u) => URL.revokeObjectURL(u)) }
  }, [])

  const items = feedback.data?.items ?? []
  const list = filter === 'all' ? items : items.filter((i: any) => i.status === filter)
  const count = (s: string) => items.filter((i: any) => i.status === s).length

  // L6（2026-08-12）：in-flight 去重——渲染期/重复调用只发一次请求（原仅按结果去重，
  // 同一截图渲染多次会并发重复请求；失败重试也受 in-flight 保护）
  const shotInFlight = useRef<Set<string>>(new Set())
  const loadShot = (name: string, fid: number) => {
    const key = `${fid}:${name}`
    if (shotUrls[key] || shotInFlight.current.has(key)) return
    shotInFlight.current.add(key)
    fetch(`${API_PREFIX}/feedback/photo?fid=${fid}&name=${encodeURIComponent(name)}`)  // L11：cookie 自动携带
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((b) => setShotUrls((u) => ({ ...u, [key]: URL.createObjectURL(b) })))
      .catch(() => {})
      .finally(() => shotInFlight.current.delete(key))
  }

  const statusColor: Record<string, string> = { new: 'var(--brand-ink)', processing: 'var(--warning)', done: 'var(--success)', revoked: 'var(--text-3)' }
  const statusLabel: Record<string, string> = { new: '待处理', processing: '处理中', done: '已处理', revoked: '已撤销' }
  const fmt = (v?: string | null) => (v ? v.slice(0, 16).replace('T', ' ') : '-')

  // 三态状态更新（待处理/处理中/已处理 自己选）；撤销/变更后详情浮窗同步刷新（不再停留在旧快照）
  const setStatus = (id: number, status: string) => {
    adminApi.updateFeedback(id, { status })
      .then(() => {
        feedback.reload()
        setDetail((d: any) => (d && d.id === id ? { ...d, status } : d))
      })
      .catch(() => message.error('更新失败'))
  }
  // H2（2026-08-12）：撤销走独立 revoke 接口——PUT status='revoked' 会被后端 Literal 校验
  // 422（合法流转无 revoked）；后端 revoke 已加 admin 代撤分支
  const revokeFeedback = (id: number) => {
    adminApi.revokeFeedback(id)
      .then(() => {
        message.success('已撤销')
        feedback.reload()
        setDetail((d: any) => (d && d.id === id ? { ...d, status: 'revoked' } : d))
      })
      .catch((e) => message.error(e.response?.data?.error?.message || '撤销失败'))
  }
  // 状态三选（待处理/处理中/已处理；已撤销不参与三态）
  const STATUS_OPTIONS = [
    { value: 'new', label: '待处理' },
    { value: 'processing', label: '处理中' },
    { value: 'done', label: '已处理' },
  ]

  // 详情浮窗（点击行弹出，宽度 760 便于看截图与回复）
  const renderDetail = (r: any) => (
    <div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 12 }}>
        <Tag color="blue">{r.feedback_type || '其他'}</Tag>
        <span style={{ fontSize: 12, color: statusColor[r.status] || 'var(--text-2)' }}>● {statusLabel[r.status] || r.status}</span>
        <span style={{ fontSize: 12, color: 'var(--text-3)' }}>提交：{r.username}（{r.department_id}）· {fmt(r.created_at)}</span>
      </div>
      <div style={{ fontSize: 13, color: 'var(--text-1)', lineHeight: 1.8, whiteSpace: 'pre-wrap', marginBottom: 12 }}>{r.content}</div>
      {r.page && <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 4 }}>来源页面：{r.page}</div>}
      {r.contact && <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 4 }}>联系方式：{r.contact}</div>}
      {r.screenshots && r.screenshots.length > 0 && (
        <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
          {r.screenshots.map((s: string, i: number) => {
            const url = shotUrls[`${r.id}:${s}`]
            if (!url) { loadShot(s, r.id); return null }
            return <img key={i} src={url} alt={`截图${i + 1}`}
              style={{ maxWidth: 220, borderRadius: 8, border: '1px solid var(--border)', cursor: 'zoom-in' }}
              onClick={() => window.open(url, '_blank')} />
          })}
        </div>
      )}
      {r.reply && (
        <div style={{ background: 'var(--brand-soft)', borderRadius: 8, padding: '10px 14px', borderLeft: '3px solid #1a56db' }}>
          <div style={{ fontSize: 12, color: 'var(--brand-ink)', fontWeight: 600, marginBottom: 4 }}>
            admin 回复{r.operator ? ` · ${r.operator}` : ''}
            {r.processed_at ? <span style={{ color: 'var(--text-3)', fontWeight: 400, marginLeft: 8 }}>{fmt(r.processed_at)}</span> : null}
          </div>
          <div style={{ fontSize: 13, color: 'var(--text-1)', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{r.reply}</div>
        </div>
      )}
      {/* 浮窗内处理反馈：三态自选（待处理/处理中/已处理）+ 回复 + 撤销 */}
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 16, borderTop: '1px solid var(--border)', paddingTop: 14 }}>
        {r.status === 'revoked' ? (
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>已撤销</span>
        ) : (
          <Select size="small" style={{ width: 110 }} value={r.status}
            // M9（2026-08-12）：done 是终态（后端流转 done→set()），下拉禁用防提交必 400
            disabled={r.status === 'done'}
            options={STATUS_OPTIONS} onChange={(v) => setStatus(r.id, String(v))} />
        )}
        {/* H3（2026-08-12）：revoked 是终态，隐藏回复按钮（原无条件渲染 → 点击必 400） */}
        {r.status !== 'revoked' && (
          <Button size="small" type="primary" onClick={() => setFbReply({ id: r.id, content: r.reply || '' })}>
            {r.status === 'done' ? '修改回复' : '回复处理'}
          </Button>
        )}
        {r.status !== 'revoked' && (
          <Popconfirm title="撤销这条反馈？" okText="撤销" cancelText="保留" onConfirm={() => revokeFeedback(r.id)}>
            <Button size="small" danger>撤销</Button>
          </Popconfirm>
        )}
      </div>
    </div>
  )

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>用户反馈</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>点击任意一行查看详情并处理；状态（待处理/处理中/已处理）可自行选择，回复提交自动标记已处理</p>
      <div style={{ marginBottom: 12 }}>
        <Select size="small" style={{ width: 200 }} value={filter}
          options={[
            { value: 'all', label: `全部 ${items.length}` },
            { value: 'new', label: `待处理 ${count('new')}` },
            { value: 'processing', label: `处理中 ${count('processing')}` },
            { value: 'done', label: `已处理 ${count('done')}` },
            { value: 'revoked', label: `已撤销 ${count('revoked')}` },
          ]}
          onChange={(v) => setFilter(String(v))} />
      </div>
      <Table rowKey="id" size="small" loading={feedback.loading}
        onRow={(r: any) => ({ onClick: () => { setDetail(r); (r.screenshots || []).forEach((s: string) => loadShot(s, r.id)) }, style: { cursor: 'pointer' } })}
        columns={[
          { title: '状态', dataIndex: 'status', width: 80, render: (s: string) => <span style={{ color: statusColor[s] || 'var(--text-2)' }}>● {statusLabel[s] || s}</span> },
          { title: '类型', dataIndex: 'feedback_type', width: 150, render: (v: string | null) => v || '其他' },
          { title: '提交人', dataIndex: 'username', width: 90 },
          { title: '团队', dataIndex: 'department_id', width: 70 },
          { title: '内容', dataIndex: 'content', ellipsis: true },
          { title: '页面', dataIndex: 'page', width: 100, render: (v: string | null) => v || '-' },
          { title: '截图', dataIndex: 'screenshots', width: 110, render: (v: string[] | null, row: any) => (
              v && v.length > 0 ? (
                <span style={{ display: 'flex', gap: 4 }} onClick={(e) => e.stopPropagation()}>
                  {v.slice(0, 4).map((s, i) => {
                    const url = shotUrls[`${row.id}:${s}`]
                    if (!url) { loadShot(s, row.id); return <span key={i} style={{ width: 26, height: 26, background: 'var(--surface-2)', borderRadius: 4 }} /> }
                    return <img key={i} src={url} alt={`截图${i + 1}`} onClick={() => window.open(url, '_blank')}
                      style={{ width: 26, height: 26, objectFit: 'cover', borderRadius: 4, border: '1px solid var(--border)', cursor: 'zoom-in' }} />
                  })}
                </span>
              ) : '-'
            ) },
          { title: '提交时间', dataIndex: 'created_at', width: 125, render: (v: string | null) => <span style={{ fontSize: 12, color: 'var(--text-2)' }}>{fmt(v)}</span> },
          { title: '操作', width: 185, render: (_: any, row: any) => (
              <span style={{ display: 'flex', gap: 6, alignItems: 'center' }} onClick={(e) => e.stopPropagation()}>
                {row.status === 'revoked' ? (
                  <span style={{ fontSize: 12, color: 'var(--text-3)' }}>已撤销</span>
                ) : (
                  <Select size="small" style={{ width: 92 }} value={row.status}
                    disabled={row.status === 'done'}  // M9：done 终态禁改
                    options={STATUS_OPTIONS} onChange={(v) => setStatus(row.id, String(v))} />
                )}
                {/* H3：revoked 终态隐藏回复按钮 */}
                {row.status !== 'revoked' && (
                  <Button size="small" type="primary" ghost onClick={() => setFbReply({ id: row.id, content: row.reply || '' })}>
                    {row.status === 'done' ? '改回复' : '回复处理'}
                  </Button>
                )}
              </span>
            ) },
        ]}
        dataSource={list} pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }} />
      {/* 详情浮窗（大尺寸：内容/截图/回复/处理） */}
      <DraggableModal open={!!detail} title={detail ? `反馈详情 · ${detail.username}（${detail.feedback_type || '其他'}）` : ''}
        footer={null} width={760} onCancel={() => setDetail(null)}>
        {detail && renderDetail(detail)}
      </DraggableModal>
      {/* 回复处理浮窗（zIndex 高于详情浮窗，避免被覆盖） */}
      <DraggableModal open={!!fbReply} title="回复处理" zIndex={1100} onCancel={() => setFbReply(null)} onOk={() => {
        if (!fbReply) return
        adminApi.updateFeedback(fbReply.id, { status: 'done', reply: fbReply.content })
          .then(() => { message.success('已处理'); setFbReply(null); setDetail(null); feedback.reload() })
          .catch(() => message.error('保存失败'))
      }}>
        <Input.TextArea rows={4} placeholder="处理回复（提交后自动标记已处理，用户可在反馈中心看到）" value={fbReply?.content ?? ''}
          onChange={(e) => setFbReply((v) => v ? { ...v, content: e.target.value } : v)} />
      </DraggableModal>
    </div>
  )
}

/* ===== 数据备份（三期：导出 JSON / 一键导入覆盖）===== */
export function DataBackupPage() {
  const [importFile, setImportFile] = useState<File | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [lastStats, setLastStats] = useState<any>(null)

  const doExport = async () => {
    setBusy(true)
    try {
      // 2026-09-15：改直链下载（全量数据可大，原 fetch→blob 整包进内存）
      downloadByUrl(`${API_PREFIX}/admin/data/export`,
        `platform_backup_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '_')}.json`)
      message.success('已导出（含账号/会话/知识库/团队业务数据；含密码哈希，请妥善保管）')
    } catch {
      message.error('导出失败')
    } finally {
      setBusy(false)
    }
  }

  const doImport = async () => {
    if (!importFile) return
    setBusy(true)
    const fd = new FormData()
    fd.append('file', importFile)
    try {
      const r = await adminApi.dataImport(fd)
      setLastStats(r.stats)
      message.success(`导入完成：全局 ${Object.keys(r.stats.global ?? {}).length} 表、团队 ${Object.keys(r.stats.depts ?? {}).length} 个（导入前已备份到 /data/backups）`)
      setConfirmOpen(false)
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || e.response?.data?.detail?.message || '导入失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>数据备份</h2>
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 8 }}>导出平台全量数据</h4>
        <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
          包含：全局库全部表（账号/会话/消息/图表/知识库/记忆/配置/审计等）+ 各团队库业务数据。
          <span style={{ color: 'var(--warning)' }}> 注意：含账号密码哈希与配置信息，文件请妥善保管。</span>
        </p>
        <Button type="primary" size="small" loading={busy} onClick={doExport}>导出 JSON 备份</Button>
      </div>
      <div className="card" style={{ padding: 16 }}>
        <h4 style={{ marginBottom: 8 }}>一键导入覆盖</h4>
        <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
          <span style={{ color: 'var(--error)' }}>危险操作：</span>导入将用文件内容覆盖当前全部数据（账号/会话/知识库/团队业务数据）。
          导入前系统会自动 pg_dump 备份当前数据到 /data/backups。
        </p>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <input type="file" accept=".json" onChange={(e) => setImportFile(e.target.files?.[0] || null)} />
          <Button type="primary" danger size="small" disabled={!importFile} onClick={() => setConfirmOpen(true)}>
            导入并覆盖
          </Button>
        </div>
        {lastStats && (
          <div style={{ fontSize: 12, marginTop: 12, color: 'var(--success)' }}>
            上次导入：全局 {Object.keys(lastStats.global ?? {}).length} 表 / 团队 {Object.keys(lastStats.depts ?? {}).length} 个 / 备份 {lastStats.backup_files?.join(', ') ?? '-'}
          </div>
        )}
      </div>
      <DraggableModal open={confirmOpen} title="确认导入覆盖？" okText="确认覆盖" okButtonProps={{ danger: true }}
        onCancel={() => setConfirmOpen(false)} onOk={doImport}>
        <p>将用「{importFile?.name}」覆盖当前全部数据：账号、会话、知识库、配置、各团队业务数据。</p>
        <p style={{ color: 'var(--error)' }}>此操作不可撤销（导入前会自动备份，备份在 /data/backups）。</p>
      </DraggableModal>
    </div>
  )
}

/* ===== 日志与监控 ===== */
export function LogsPage() {
  const errors = useData(adminApi.logErrors)
  const audits = useData(adminApi.auditLogs)

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>日志与监控</h2>
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 8 }}>错误日志（最近 100 条）</h4>
        {/* L1（2026-08-12）：先判 error——原失败态 data=null 也命中「暂无错误」假象 */}
        {errors.loading ? <p>加载中...</p> : errors.error ? (
          <p style={{ color: 'var(--error)' }}>加载失败
            <span style={{ color: 'var(--primary)', cursor: 'pointer', marginLeft: 6 }} onClick={errors.reload}>点击重试</span>
          </p>
        ) : (errors.data?.errors ?? []).length === 0 ? <p style={{ color: 'var(--text-3)' }}>暂无错误</p> : (
          <div style={{ maxHeight: 240, overflow: 'auto', fontSize: 12, fontFamily: 'monospace', background: 'var(--surface-inset)', padding: 10, borderRadius: 6 }}>
            {(errors.data?.errors ?? []).map((l: string, i: number) => <div key={i} style={{ marginBottom: 2 }}>{l}</div>)}
          </div>
        )}
      </div>
      <div className="card" style={{ padding: 16 }}>
        <h4 style={{ marginBottom: 8 }}>审计日志</h4>
        <Table rowKey="id" size="small" loading={audits.loading} pagination={{ pageSize: 8 }}
          columns={[
            { title: '操作人', dataIndex: 'operator', width: 90 },
            { title: '动作', dataIndex: 'action', width: 150 },
            { title: '对象', dataIndex: 'target', width: 160 },
            { title: '时间', dataIndex: 'created_at', render: (t: string) => t ? t.slice(0, 16) : '-' },
          ]} dataSource={audits.data?.items ?? []} />
      </div>
    </div>
  )
}

/* ===== 团队管理（三期 M12：注册=建库+建账号通道，工具白名单）===== */
// 4.1：白名单 UI 动态渲染（/admin/tools-meta 中文名/图标/分组，不硬编码工具名）；
// AI 技能白名单只管默认技能（multi）；团队技能（单选）不归运维管；
// 定制化工具白名单 = 简历初筛/知识库浏览/会议纪要
export function DepartmentsPage() {
  const depts = useData(adminApi.deptList)
  const [selected, setSelected] = useState<string | null>(null)  // 2026-08-07：左侧团队列表选中
  const [newDept, setNewDept] = useState<any>(null)              // 注册新团队（弹窗）
  const [toolsFor, setToolsFor] = useState<any>(null)            // 当前编辑黑名单的团队
  const [toolMeta, setToolMeta] = useState<any[]>([])            // 4.1：平台工具动态元数据
  const [customToolMeta, setCustomToolMeta] = useState<any[]>([]) // 2026-08-24：定制化工具注册表（动态加载）
  const [toolSel, setToolSel] = useState<string[]>([])           // AI 技能禁用勾选（黑名单）
  const [customSel, setCustomSel] = useState<string[]>([])       // 2026-09-01：定制化工具禁用勾选（黑名单；勾选=禁用）
  // 2026-09-02：团队定制化工具白名单（/dtools 板块，默认全团队关闭；勾选=开通）
  const [deptToolMeta, setDeptToolMeta] = useState<any[]>([])    // 团队定制化工具注册表
  const [deptAllowSel, setDeptAllowSel] = useState<string[]>([]) // 团队定制化工具开通勾选（白名单）
  const [savingTools, setSavingTools] = useState(false)

  // 默认选中第一个团队
  useEffect(() => {
    const list = depts.data?.departments ?? []
    if (!selected && list.length) setSelected(list[0].dept_id)
  }, [depts.data]) // eslint-disable-line react-hooks/exhaustive-deps
  const selectedDept = (depts.data?.departments ?? []).find((d: any) => d.dept_id === selected) || null
  // 2026-08-12：UsersPage 的可建账号团队下拉（排除 dept_root/CEO）由父级传入，消除重复请求
  const userDeptOptions = (depts.data?.departments ?? [])
    .filter((d: any) => d.dept_id !== 'dept_root' && d.dept_id !== 'ceo')
    .map((d: any) => ({ value: d.dept_id, label: d.name }))

  const register = () => {
    if (!/^[a-z][a-z0-9_]{1,29}$/.test(newDept.dept_id)) return message.warning('团队标识：小写字母开头，2-30 位小写字母/数字/下划线')
    if (!newDept.name.trim()) return message.warning('请输入团队名称')
    adminApi.deptCreate(newDept).then(() => { message.success('已注册（含数据库）'); setNewDept(null); depts.reload() })
      .catch((e) => message.error(e.response?.data?.error?.message || e.response?.data?.detail?.message || '注册失败'))
  }

  // 4.1：打开工具权限弹窗——加载动态元数据 + 技能白名单/工具黑名单当前值
  // M2（2026-08-12）：目标校验——快速切换团队时旧响应晚到不得覆盖新弹窗
  const toolsTarget = useRef<string | null>(null)
  const openTools = (d: any) => {
    toolsTarget.current = d.dept_id
    setToolsFor(d)
    // 走查：同 openUserTools——六个请求原本各自静默置空，任一失败就会把"全未勾选"显示出来，
    // 保存是整份覆盖 ⇒ 一次抖动就把团队技能/工具/定制工具白名单全清。失败即不打开弹窗。
    Promise.all([
      adminApi.toolsMeta(), adminApi.customToolsMeta(), adminApi.deptTools(d.dept_id),
      adminApi.deptCustomTools(d.dept_id),
      // 2026-09-02：团队定制化工具白名单（/dtools 板块）
      adminApi.deptCustomToolsMeta(), adminApi.deptAllowTools(d.dept_id),
    ]).then(([tm, ctm, dt, dct, dctm, dat]) => {
      if (toolsTarget.current !== d.dept_id) return
      setToolMeta(tm.tools || [])
      setCustomToolMeta(ctm.tools || [])
      setToolSel(dt.tools ?? [])
      setCustomSel(dct.tools ?? [])
      setDeptToolMeta(dctm.tools || [])
      setDeptAllowSel(dat.tools ?? [])
    }).catch(() => {
      if (toolsTarget.current !== d.dept_id) return
      setToolsFor(null)
      message.error('工具权限加载失败，已取消打开（未做任何修改），请重试')
    })
  }
  const saveTools = () => {
    if (!toolsFor) return
    setSavingTools(true)
    Promise.all([
      adminApi.deptToolsPut(toolsFor.dept_id, toolSel),
      adminApi.deptCustomToolsPut(toolsFor.dept_id, customSel),
      adminApi.deptAllowToolsPut(toolsFor.dept_id, deptAllowSel),
    ]).then(() => { message.success('已保存'); setToolsFor(null); depts.reload() })
      .catch(() => message.error('保存失败'))
      .finally(() => setSavingTools(false))
  }
  // 2026-09-01：AI 技能黑名单覆盖全部默认技能（multi + 单选 skill）；run_script 个人级兜底豁免不展示
  const whitelistTools = toolMeta.filter((t) => t.id !== 'run_script')
  const toggleSel = (id: string, on: boolean) => {
    setToolSel(on ? [...toolSel, id] : toolSel.filter((x) => x !== id))
  }

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>组织与用户</h2>
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        {/* 2026-08-07：左侧团队列表（点击切换，右侧管理该团队用户） */}
        <div className="card" style={{ width: 220, flexShrink: 0, padding: 8 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 8px 8px' }}>
            <span style={{ fontWeight: 600, fontSize: 13 }}>团队</span>
            <Button size="small" type="text" style={{ fontSize: 11 }} onClick={() => setNewDept({ dept_id: '', name: '' })}>+ 注册</Button>
          </div>
          {depts.data?.departments?.map((d: any) => (
            <div key={d.dept_id} onClick={() => setSelected(d.dept_id)}
              style={{ padding: '8px 10px', borderRadius: 6, cursor: 'pointer', marginBottom: 2,
                background: selected === d.dept_id ? 'var(--primary-bg)' : 'transparent',
                color: selected === d.dept_id ? 'var(--primary)' : 'var(--text-1)' }}>
              <div style={{ fontSize: 13, fontWeight: 500 }}>{d.name}</div>
              <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {d.dept_id}{d.table_count != null ? ` · ${d.table_count} 表 · ${d.row_count ?? '-'} 行` : ''}
              </div>
            </div>
          ))}
          {!depts.loading && depts.error && (
            <div style={{ padding: 8, fontSize: 12, color: 'var(--error)' }}>
              团队加载失败
              <span style={{ color: 'var(--primary)', cursor: 'pointer', marginLeft: 6 }} onClick={depts.reload}>点击重试</span>
            </div>
          )}
          {!depts.loading && !depts.error && !(depts.data?.departments?.length) && (
            <div style={{ padding: 8, fontSize: 12, color: 'var(--text-3)' }}>暂无团队，点"+ 注册"创建</div>
          )}
        </div>
        {/* 右侧：团队详情 + 用户管理（含用户级工具权限） */}
        <div style={{ flex: 1, minWidth: 0 }}>
          {selectedDept && (
            <div style={{ display: 'flex', gap: 8, marginBottom: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <span style={{ fontWeight: 600 }}>{selectedDept.name}（{selectedDept.dept_id}）</span>
              {selectedDept.dept_id !== 'dept_root' && (
                <Button size="small" onClick={() => openTools(selectedDept)}>技能/工具权限</Button>
              )}
              {/* B4（2026-08-18）：删除团队（级联删全部用户与数据；平台团队保护） */}
              {selectedDept.dept_id !== 'dept_root' && selectedDept.dept_id !== 'ceo' && (
                <Popconfirm
                  title={`删除团队「${selectedDept.name}」？`}
                  description="将级联删除该团队全部用户、会话、知识库与独立数据库，不可恢复！"
                  okText="确认删除"
                  okButtonProps={{ danger: true }}
                  onConfirm={async () => {
                    try {
                      await adminApi.deptDelete(selectedDept.dept_id)
                      message.success('团队已删除')
                      setSelected(null)
                      depts.reload()
                    } catch (e: any) {
                      message.error(e.response?.data?.error?.message || '删除失败')
                    }
                  }}
                >
                  <Button size="small" danger>删除团队</Button>
                </Popconfirm>
              )}
            </div>
          )}
          <UsersPage deptId={selected ?? undefined} deptOptions={userDeptOptions} />
        </div>
      </div>
      {/* 注册新团队弹窗 */}
      <DraggableModal open={!!newDept} title="注册新团队（自动创建该团队的独立数据库）" onCancel={() => setNewDept(null)} onOk={register}>
        <input className="login-input" placeholder="团队编码（小写英文，如 sales）" value={newDept?.dept_id || ''}
          onChange={(e) => setNewDept((v: any) => ({ ...v, dept_id: e.target.value }))} />
        <input className="login-input" placeholder="团队名称（如 销售部）" value={newDept?.name || ''}
          onChange={(e) => setNewDept((v: any) => ({ ...v, name: e.target.value }))} />
      </DraggableModal>
      {/* 4.1：白名单弹窗——AI 技能（默认技能多选）+ 定制化工具；动态元数据中文名，不硬编码 */}
      <DraggableModal open={!!toolsFor} title={`团队权限：${toolsFor?.name}（${toolsFor?.dept_id}）`}
        confirmLoading={savingTools} onCancel={() => setToolsFor(null)} onOk={saveTools}>
        <div style={{ marginBottom: 12 }}>
          {/* 2026-09-01（黑名单改造）：勾选=禁用（默认全开防漏配 403）；未配置=全部可用 */}
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>AI 技能禁用（黑名单）</div>
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>
            勾选 = 禁用该团队 Agent 的默认技能（含技能卡单选类）；不勾选任何项并保存 = 全部可用。
            用户级可在用户管理里进一步禁用（并集生效）。
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {whitelistTools.map((t) => (
              <span key={t.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, border: '1px solid var(--border)', borderRadius: 16, padding: '3px 10px', fontSize: 12, cursor: 'pointer', userSelect: 'none', background: toolSel.includes(t.id) ? 'var(--primary-bg)' : 'var(--surface-1)', color: toolSel.includes(t.id) ? 'var(--primary)' : 'var(--text-1)' }}
                onClick={() => toggleSel(t.id, !toolSel.includes(t.id))} title={t.summary || ''}>
                <Icon as={iconFromKey(t.icon)} /> {t.name}
              </span>
            ))}
          </div>
        </div>
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12 }}>
          {/* 2026-09-01（黑名单改造）：勾选=禁用（默认全开防漏勾 403）；未配置=全部可用 */}
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>定制化工具禁用（黑名单）</div>
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>
            勾选 = 禁用该团队的定制化工具（未配置 = 全部可用）；不勾选任何项并保存 = 全部可用。
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {/* 2026-08-24：动态加载（后端 /admin/custom-tools 注册表，新增工具免改前端） */}
            {customToolMeta.map((t: any) => (
              <span key={t.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, border: '1px solid var(--border)', borderRadius: 16, padding: '3px 10px', fontSize: 12, cursor: 'pointer', userSelect: 'none', background: customSel.includes(t.id) ? 'var(--primary-bg)' : 'var(--surface-1)', color: customSel.includes(t.id) ? 'var(--primary)' : 'var(--text-1)' }}
                onClick={() => setCustomSel((s) => s.includes(t.id) ? s.filter((x) => x !== t.id) : [...s, t.id])}>
                {t.name}
              </span>
            ))}
            {customToolMeta.length === 0 && <span style={{ fontSize: 12, color: 'var(--text-3)' }}>加载中…</span>}
          </div>
        </div>
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 12, marginTop: 12 }}>
          {/* 2026-09-02：团队定制化工具白名单（/dtools 板块）——勾选=开通（默认全团队关闭） */}
          <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 4 }}>团队定制化工具开通（白名单）</div>
          <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 8 }}>
            勾选 = 开通该团队的团队定制化工具（简历初筛等）；不勾选任何项并保存 = 全部关闭（默认态）。
          </p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {deptToolMeta.map((t: any) => (
              <span key={t.id} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, border: '1px solid var(--border)', borderRadius: 16, padding: '3px 10px', fontSize: 12, cursor: 'pointer', userSelect: 'none', background: deptAllowSel.includes(t.id) ? 'var(--primary-bg)' : 'var(--surface-1)', color: deptAllowSel.includes(t.id) ? 'var(--primary)' : 'var(--text-1)' }}
                onClick={() => setDeptAllowSel((s) => s.includes(t.id) ? s.filter((x) => x !== t.id) : [...s, t.id])}>
                {t.name}
              </span>
            ))}
            {deptToolMeta.length === 0 && <span style={{ fontSize: 12, color: 'var(--text-3)' }}>加载中…</span>}
          </div>
        </div>
      </DraggableModal>
    </div>
  )
}

/* ===== 知识库管理（2026-08-24 团队化重构：左侧团队列表 → 右侧该团队分类+文档管理）===== */

// admin 文档改分类表单（归属三态：全局文档→全局分类；团队文档→全局+本团队分类；个人文档不显示入口）
function RecategorizeForm({ doc, cats, onDone }: { doc: any; cats: any[]; onDone: () => void }) {
  const [cid, setCid] = useState<number | null>(doc.category_id ?? null)
  const options = (cats || []).filter((c: any) =>
    c.user_id == null && (doc.department_id == null ? c.dept_id == null : c.dept_id == null || c.dept_id === doc.department_id))
  return (
    <div>
      <Select style={{ width: '100%' }} value={cid ?? ''}
        onChange={(v) => setCid(v ? Number(v) : null)}
        options={[
          { value: '', label: '不分类' },
          ...options.map((c: any) => ({ value: c.id, label: `${c.name}${c.dept_id ? `（${c.dept_id}）` : '（全局）'}` })),
        ]} />
      <div style={{ marginTop: 12, textAlign: 'right' }}>
        <Button size="small" onClick={onDone}>取消</Button>
        <Button size="small" type="primary" style={{ marginLeft: 8 }} onClick={() =>
          adminApi.kbDocumentRecategorize(doc.id, cid).then(() => { message.success('已更新'); onDone() })
            .catch((e) => message.error(e.response?.data?.error?.message || '更新失败'))}>保存</Button>
      </div>
    </div>
  )
}

export function KbPage() {
  const docs = useData(adminApi.kbDocuments)
  const cats = useData(adminApi.kbCategories)
  // 团队列表（左侧导航；'all'=全部总览 / 'global'=全局分类文档 / dept_id=该团队）
  const [depts, setDepts] = useState<{ dept_id: string; name: string }[]>([])
  useEffect(() => {
    adminApi.deptOptions().then((r) => setDepts(r.departments || [])).catch(() => {})
  }, [])
  const [selected, setSelected] = useState<string>('all')
  const [uploadOpen, setUploadOpen] = useState(false)
  const [newCat, setNewCat] = useState('')
  const [upForm, setUpForm] = useState<{ file: File | null; title: string; category_id?: number; department_id: string | null }>({ file: null, title: '', department_id: null })
  // 改分类 Modal（admin 端；个人文档不可改）
  const [reCat, setReCat] = useState<any>(null)
  // 重命名分类 Modal（替代浏览器 prompt）
  const [renameCat, setRenameCat] = useState<{ id: number; name: string; dept_id: string | null } | null>(null)
  // 文档详情（点击行查看内容与上传人）
  const [detail, setDetail] = useState<any>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  // M2（2026-08-12）：目标校验——快速点击两个文档时旧响应晚到不得覆盖新详情
  const detailTarget = useRef<number | null>(null)
  const openDetail = (row: any) => {
    detailTarget.current = row.id
    setDetailLoading(true)
    setDetail(null)
    adminApi.kbDocumentDetail(row.id)
      .then((d) => { if (detailTarget.current === row.id) setDetail(d) })
      .catch((e) => { if (detailTarget.current === row.id) message.error(e.response?.data?.error?.message || '详情加载失败') })
      .finally(() => { if (detailTarget.current === row.id) setDetailLoading(false) })
  }

  const upload = () => {
    if (!upForm.file) return message.warning('请选择文件')
    const fd = new FormData()
    fd.append('file', upForm.file)
    fd.append('title', upForm.title)
    if (upForm.category_id != null) fd.append('category_id', String(upForm.category_id))
    adminApi.kbDocumentUpload(fd, upForm.department_id)
      .then(() => {
        message.success('已上传')
        setUploadOpen(false)
        setUpForm({ file: null, title: '', department_id: null })
        docs.reload(); cats.reload()
      })
      .catch((e) => message.error(e.response?.data?.error?.message || '上传失败'))
  }

  const createCat = () => {
    if (!newCat.trim()) return message.warning('请输入分类名称')
    // 2026-08-24：分类归属随当前视图——团队视图建团队分类，全局视图建全局分类
    const deptId = selected === 'global' || selected === 'all' ? null : selected
    adminApi.kbCategoryCreate({ name: newCat.trim(), dept_id: deptId })
      .then(() => { message.success('分类已创建'); setNewCat(''); cats.reload() })
      .catch((e) => message.error(e.response?.data?.error?.message || '创建失败'))
  }

  // 2026-08-24：视图过滤——'all' 全量（含个人文档）/ 'global' 全局 / 团队（不含个人文档）
  const viewCats = (cats.data?.categories ?? []).filter((c: any) =>
    selected === 'all' ? true : selected === 'global' ? c.dept_id == null : c.dept_id === selected)
  const viewDocs = (docs.data?.documents ?? []).filter((d: any) => {
    if (selected === 'all') return true
    if (selected === 'global') return d.user_id == null && d.department_id == null
    return d.user_id == null && d.department_id === selected
  })

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>知识库管理</h2>
      {/* 2026-08-24：团队化——左侧团队列表（全局/全部/各团队），右侧管理该视图分类与文档 */}
      <div style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <div className="card" style={{ width: 200, flexShrink: 0, padding: 8 }}>
          <div style={{ padding: '4px 8px 8px', fontWeight: 600, fontSize: 13 }}>团队知识库</div>
          {[{ id: 'all', label: '全部（总览）' }, { id: 'global', label: <><Icon as={GlobeSimple} size={14} /> 全局</> },
            ...depts.filter((d) => d.dept_id !== 'dept_root').map((d) => ({ id: d.dept_id, label: `${d.name}（${d.dept_id}）` }))]
            .map((v) => (
              <div key={v.id} onClick={() => { setSelected(v.id); setUploadOpen(false) }}
                style={{ padding: '8px 10px', borderRadius: 6, cursor: 'pointer', marginBottom: 2,
                  background: selected === v.id ? 'var(--primary-bg)' : 'transparent',
                  color: selected === v.id ? 'var(--primary)' : 'var(--text-1)', fontSize: 13 }}>
                {v.label}
              </div>
            ))}
        </div>
      <div style={{ flex: 1, minWidth: 0 }}>
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
          <h4 style={{ marginBottom: 0 }}>分类（{viewCats.length}）
            {selected === 'global' && <Tag style={{ marginLeft: 8, fontSize: 10 }}>全局分类</Tag>}
            {selected !== 'all' && selected !== 'global' && <Tag color="green" style={{ marginLeft: 8, fontSize: 10 }}>{selected}</Tag>}
          </h4>
          {selected !== 'all' && (
            <div style={{ display: 'flex', gap: 8 }}>
              <input className="login-input" style={{ width: 180, marginBottom: 0 }} placeholder="新分类名称"
                value={newCat} onChange={(e) => setNewCat(e.target.value)} />
              <Button size="small" type="primary" onClick={createCat}>新建分类</Button>
            </div>
          )}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 10 }}>
          {viewCats.map((c: any) => {
            const docCount = viewDocs.filter((d: any) => d.category_id === c.id).length
            return (
              <div key={c.id} className="cat-card">
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                  <span style={{ fontWeight: 600, fontSize: 13 }}>{c.name}</span>
                  <span>
                    {/* 2026-08-24：分类归属三态——dept_id 非空=团队分类，空=全局分类 */}
                    {c.dept_id
                      ? <Tag color="green" style={{ margin: 0, fontSize: 10 }}>{c.dept_id}</Tag>
                      : <Tag style={{ margin: 0, fontSize: 10 }}>全局</Tag>}
                    {c.parent_id && <Tag style={{ margin: 0, fontSize: 10, marginLeft: 4 }}>子分类</Tag>}
                  </span>
                </div>
                <div style={{ fontSize: 11, color: 'var(--text-3)', margin: '6px 0 10px' }}>{docCount} 篇文档</div>
                <div style={{ display: 'flex', gap: 4 }}>
                  <Button size="small" type="link" style={{ fontSize: 11, padding: '0 4px' }}
                    onClick={() => setRenameCat({ id: c.id, name: c.name, dept_id: c.dept_id ?? null })}>重命名</Button>
                  <Popconfirm title={`删除分类「${c.name}」？（其下文档将变为未分类）`} onConfirm={() =>
                    // L4（2026-08-12）：分类变更后文档表同步刷新（分类列/计数依赖 docs.data）
                    adminApi.kbCategoryDelete(c.id).then(() => { message.success('已删除'); cats.reload(); docs.reload() })
                      .catch((e) => message.error(e.response?.data?.error?.message || '删除失败'))}>
                    <Button size="small" type="link" danger style={{ fontSize: 11, padding: '0 4px' }}>删除</Button>
                  </Popconfirm>
                </div>
              </div>
            )
          })}
          {viewCats.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--text-3)', padding: 12 }}>暂无分类，先新建一个吧</div>
          )}
        </div>
      </div>
      <div className="card" style={{ padding: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <h4 style={{ marginBottom: 0 }}>文档（{viewDocs.length}）</h4>
          <Button size="small" type="primary" onClick={() => {
            // 2026-08-24：上传目标团队随当前视图（全部视图默认全局）
            setUpForm((u) => ({ ...u, department_id: selected === 'global' || selected === 'all' ? null : selected }))
            setUploadOpen(true)
          }}>+ 上传文档</Button>
        </div>
        <Table rowKey="id" size="small" loading={docs.loading} dataSource={viewDocs} pagination={{ pageSize: 10 }}
          onRow={(row: any) => ({ onClick: () => openDetail(row), style: { cursor: 'pointer' } })}
          columns={[
            { title: '标题', dataIndex: 'title', ellipsis: true },
            // 2026-08-24：来源列（归属三态）——user_id 非空=个人文档（admin 只读+删），否则按团队/全局
            { title: '来源', dataIndex: 'user_id', width: 70, render: (v: number | null, row: any) =>
              v != null ? <Tag color="blue">个人</Tag>
                : row.department_id ? <Tag color="green">团队</Tag> : <Tag color="purple">全局</Tag> },
            { title: '分类', dataIndex: 'category_name', width: 100, render: (v: string) => v || '-' },
            { title: '团队', dataIndex: 'department_id', width: 90, render: (v: string | null, row: any) =>
              row.user_id != null ? '个人' : (v || '全局') },
            { title: '类型', dataIndex: 'file_type', width: 60 },
            { title: '上传人', dataIndex: 'uploader', width: 90, render: (v: string | null) => v || '-' },
            { title: '摘要', dataIndex: 'summary', ellipsis: true },
            { title: '上传时间', dataIndex: 'created_at', width: 130, render: (t: string) => (t || '').slice(0, 16) },
            { title: '操作', width: 150, render: (_: any, row: any) => (
                <span onClick={(e) => e.stopPropagation()}>
                  {/* 2026-08-24：admin 文档改分类（个人文档 admin 只读+删，不显示） */}
                  {row.user_id == null && (
                    <Button size="small" type="link" style={{ fontSize: 11, padding: '0 4px' }} onClick={() => setReCat(row)}>改分类</Button>
                  )}
                  <Popconfirm title="确认删除该文档？" onConfirm={() =>
                    adminApi.kbDocumentDelete(row.id).then(() => { message.success('已删除'); docs.reload() }).catch(() => message.error('删除失败'))}>
                    <Button size="small" type="link" danger style={{ fontSize: 11, padding: '0 4px' }}>删除</Button>
                  </Popconfirm>
                </span>
              ) },
          ]} />
      </div>

      {/* 文档详情（点击行）：元信息 + 上传人 + 分段内容 */}
      <DraggableModal open={!!detail || detailLoading} title={detail?.title || '文档详情'} footer={null} width={760}
        onCancel={() => setDetail(null)}>
        {detailLoading && <p style={{ fontSize: 12, color: 'var(--text-3)' }}>加载中…</p>}
        {detail && (
          <div style={{ fontSize: 12 }}>
            <div style={{ color: 'var(--text-2)', marginBottom: 10, lineHeight: 1.8 }}>
              上传人：<b>{detail.uploader || '-'}</b>（id {detail.uploaded_by ?? '-'}） · 团队：{detail.department_id || '全局'} ·
              类型：{detail.file_type || '-'} · {detail.chunk_count} 段 · {detail.created_at?.slice(0, 16)}
            </div>
            {detail.summary && (
              <div style={{ background: 'var(--brand-soft)', borderRadius: 6, padding: '8px 12px', marginBottom: 10 }}>
                摘要：{detail.summary}
              </div>
            )}
            <pre style={{ whiteSpace: 'pre-wrap', maxHeight: 420, overflow: 'auto', background: 'var(--surface-inset)', borderRadius: 6, padding: 12, lineHeight: 1.7 }}>
              {detail.content || '（无内容）'}
            </pre>
          </div>
        )}
      </DraggableModal>

      <DraggableModal open={uploadOpen} title="上传知识文档" onCancel={() => setUploadOpen(false)} onOk={upload}>
        <input type="file" accept=".pdf,.docx,.pptx,.txt,.md,.xlsx,.csv"
          onChange={(e) => setUpForm((v) => ({ ...v, file: e.target.files?.[0] || null }))} />
        <input className="login-input" placeholder="标题（留空取文件名）" value={upForm.title}
          onChange={(e) => setUpForm((v) => ({ ...v, title: e.target.value }))} />
        {/* 2026-08-24：归属三态——目标团队（null=全局文档）；打开时默认当前视图（全部视图默认全局） */}
        <Select style={{ width: '100%', marginTop: 8 }} placeholder="目标团队（默认全局）"
          value={upForm.department_id ?? 'global'}
          onChange={(v) => setUpForm((u) => ({ ...u, department_id: v === 'global' ? null : v, category_id: undefined }))}
          options={[
            { value: 'global', label: '全局（所有团队可见）' },
            ...depts.filter((d) => d.dept_id !== 'dept_root').map((d) => ({ value: d.dept_id, label: `${d.name}（${d.dept_id}）` })),
          ]} />
        <Select style={{ width: '100%', marginTop: 8 }} value={upForm.category_id ?? ''}
          onChange={(v) => setUpForm((u) => ({ ...u, category_id: v ? Number(v) : undefined }))}
          options={[
            { value: '', label: '不分类' },
            ...(cats.data?.categories ?? [])
              .filter((c: any) => c.dept_id == null || c.dept_id === upForm.department_id)
              .map((c: any) => ({ value: c.id, label: c.name })),
          ]} />
      </DraggableModal>
      {/* 2026-08-24：文档改分类（admin 端；个人文档不可改） */}
      <DraggableModal open={!!reCat} title={`改分类：${reCat?.title || ''}`} footer={null} width={420}
        onCancel={() => setReCat(null)}>
        {reCat && <RecategorizeForm doc={reCat} cats={cats.data?.categories ?? []} onDone={() => { setReCat(null); docs.reload() }} />}
      </DraggableModal>
      <DraggableModal open={!!renameCat} title="重命名分类" onCancel={() => setRenameCat(null)} onOk={() => {
        if (!renameCat?.name.trim()) return message.warning('请输入分类名称')
        adminApi.kbCategoryUpdate(renameCat.id, { name: renameCat.name.trim(), dept_id: renameCat.dept_id })
          .then(() => { message.success('已更新'); setRenameCat(null); cats.reload(); docs.reload() })  // L4：文档表分类列同步刷新
          .catch((e) => message.error(e.response?.data?.error?.message || '更新失败'))
      }}>
        <input className="login-input" value={renameCat?.name ?? ''} onChange={(e) => setRenameCat((v) => v ? { ...v, name: e.target.value } : v)} />
      </DraggableModal>
      </div>
      </div>
    </div>
  )
}

/* ===== 技能管理（运维：全局技能发布/启停/删除 + 团队技能代传/删除；团队启停归团队管理员）===== */
export function GlobalSkillsPage() {
  const [tab, setTab] = useState('global')
  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>技能管理</h2>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'global', label: '默认技能（全局）', children: <GlobalSkillsTab /> },
        { key: 'dept', label: '团队技能', children: <DeptSkillsTab /> },
      ]} />
    </div>
  )
}

/* 全局技能 tab：发布/启停/删除/重传（员工侧 /skills/global 个人启停） */
function GlobalSkillsTab() {
  const list = useData(adminApi.globalSkills)
  const [uploading, setUploading] = useState(false)
  const [reupFor, setReupFor] = useState<number | null>(null)  // 重传目标技能 id

  const upload = async (file: File, skillId?: number) => {
    if (skillId) setReupFor(skillId)
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('scope', 'global')
      if (skillId) await adminApi.globalSkillReupload(skillId, form)
      else await adminApi.globalSkillUpload(form)
      message.success(skillId ? '技能文件已更新' : '全局技能已发布')
      list.reload()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || (skillId ? '更新失败' : '发布失败'))
    } finally {
      setUploading(false)
      setReupFor(null)
    }
  }

  const toggle = async (s: any, on: boolean) => {
    try {
      await adminApi.globalSkillToggle(s.id, on ? 'active' : 'disabled')
      message.success('已更新')
      list.reload()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '操作失败')
    }
  }
  const del = async (s: any) => {
    try {
      await adminApi.globalSkillDelete(s.id)
      message.success('已删除')
      list.reload()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '删除失败')
    }
  }

  return (
    <div>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
        全局技能（默认AI技能）：全员可见，员工可自行启用、团队管理员可整组开关；仅运维可上传/修改。
        格式：SKILL.md 开头须含 <code>---</code> frontmatter（<code>name</code> + <code>description</code> 必填，description 是 AI 判断何时用本技能的说明），正文为执行指令；或技能包 zip（根含同格式 SKILL.md + 脚本）。
      </p>
      <div style={{ marginBottom: 12 }}>
        {/* 2026-08-21：沿用团队技能上传同款 toolbar-btn + 隐藏 input */}
        <label className="toolbar-btn" style={{ cursor: 'pointer', opacity: uploading ? 0.6 : 1 }}>
          {uploading ? (reupFor ? '更新中…' : '发布中…') : '+ 发布 SKILL.md / 技能包(.zip)'}
          <input type="file" accept=".md,text/markdown,.zip,application/zip" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = '' }} />
        </label>
      </div>
      {list.loading ? null : (list.data?.skills?.length ?? 0) === 0 ? (
        <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--text-3)', background: 'var(--surface-inset)', border: '1px dashed var(--border)' }}>
          <div style={{ fontSize: 32, marginBottom: 8 }}><Icon as={GlobeSimple} size={32} /></div>
          <div>暂无全局技能——发布后全平台员工可见（可在对应团队停用）</div>
        </div>
      ) : (
        <div className="skill-grid">
          {(list.data?.skills ?? []).map((s: any) => (
            <div key={s.id} className="skill-card">
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontWeight: 600 }}>{s.name}</span>
                <span className="badge badge-green">全局</span>
                {s.has_scripts && <span className="badge badge-blue" title="含脚本，agent 可用 run_script 执行">脚本</span>}
                <Switch size="small" checked={s.status !== 'disabled'} onChange={(v) => toggle(s, v)} />
                <label className="toolbar-btn" style={{ cursor: 'pointer', fontSize: 12, padding: '2px 10px' }}>
                  重传
                  <input type="file" accept=".md,text/markdown,.zip,application/zip" hidden
                    onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f, s.id); e.target.value = '' }} />
                </label>
                <Popconfirm title="确认删除该全局技能？" onConfirm={() => del(s)}>
                  <span style={{ fontSize: 12, color: 'var(--error)', cursor: 'pointer' }}>删除</span>
                </Popconfirm>
              </div>
              <p style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7, marginBottom: 8 }}>{s.description}</p>
              <div style={{ fontSize: 11, color: 'var(--text-3)' }}>
                {s.skill_dir ? `目录 /data/skills/${s.skill_dir}/` : '纯指令技能（无文件）'}
                {s.created_at ? ` · 发布于 ${s.created_at.slice(0, 10)}` : ''}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* 团队技能 tab：运维可代为上传/删除任意团队技能（启停由团队管理员在业务区管理，此处仅展示状态） */
function DeptSkillsTab() {
  const [depts, setDepts] = useState<{ dept_id: string; name: string }[]>([])
  const [cur, setCur] = useState<string>('')
  const [skills, setSkills] = useState<any[]>([])
  const [uploading, setUploading] = useState(false)

  useEffect(() => {
    adminApi.deptList().then((r: { departments: { dept_id: string; name: string }[] }) => {
      const list = r.departments || []
      setDepts(list)
      setCur((c) => c || list[0]?.dept_id || '')
    }).catch(() => message.error('加载团队失败'))
  }, [])
  useEffect(() => {
    if (!cur) return
    adminApi.deptSkills(cur).then((r) => setSkills(r.skills || [])).catch(() => setSkills([]))
  }, [cur])

  const upload = async (file: File) => {
    setUploading(true)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('dept_id', cur)
      await adminApi.globalSkillUpload(form)  // POST /skills/files（scope 默认 dept）
      message.success('已上传到该团队')
      adminApi.deptSkills(cur).then((r) => setSkills(r.skills || [])).catch(() => {})
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '上传失败')
    } finally {
      setUploading(false)
    }
  }
  const del = async (s: any) => {
    try {
      await adminApi.globalSkillDelete(s.id)
      message.success('已删除')
      adminApi.deptSkills(cur).then((r) => setSkills(r.skills || [])).catch(() => {})
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '删除失败')
    }
  }

  return (
    <div>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 12 }}>
        运维可代团队上传/删除技能（SKILL.md 或技能包 zip，zip 根须含 SKILL.md）；启停由团队管理员在业务区管理，此处仅展示状态。
        SKILL.md 开头须含 <code>---</code> frontmatter（<code>name</code> + <code>description</code> 必填）
      </p>
      <div style={{ marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 12, color: 'var(--text-2)' }}>团队：</span>
        <Select size="small" style={{ width: 200 }} value={cur || undefined}
          options={depts.map((d) => ({ value: d.dept_id, label: `${d.name}（${d.dept_id}）` }))}
          onChange={(v) => setCur(v)} />
        <label className="toolbar-btn" style={{ cursor: 'pointer', opacity: uploading ? 0.6 : 1 }}>
          {uploading ? '上传中…' : '+ 上传到该团队'}
          <input type="file" accept=".md,text/markdown,.zip,application/zip" hidden
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f); e.target.value = '' }} />
        </label>
      </div>
      {skills.length === 0 ? (
        <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-3)', fontSize: 12 }}>该团队暂无技能</div>
      ) : (
        <div className="skill-grid">
          {skills.map((s: any) => (
            <div key={s.id} className="skill-card">
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontWeight: 600 }}>{s.name}</span>
                <span className={s.status === 'disabled' ? 'badge badge-gray' : 'badge badge-purple'}>
                  {s.status === 'disabled' ? '已停用' : '已启用'}
                </span>
                {s.has_scripts && <span className="badge badge-green">脚本</span>}
                <Popconfirm title="确认删除该技能？" onConfirm={() => del(s)}>
                  <span style={{ fontSize: 12, color: 'var(--error)', cursor: 'pointer' }}>删除</span>
                </Popconfirm>
              </div>
              <p style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.7 }}>{s.description}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ===== 系统维护 ===== */
export function MaintenancePage() {
  const sessions = useData(adminApi.sessions)
  const jobs = useData(adminApi.jobs)

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 16 }}>系统维护</h2>
      <div className="card" style={{ padding: 16, marginBottom: 16 }}>
        <h4 style={{ marginBottom: 8 }}>活跃会话</h4>
        <Table rowKey="id" size="small" loading={sessions.loading} pagination={{ pageSize: 8 }}
          columns={[
            { title: '标题', dataIndex: 'title', ellipsis: true },
            { title: '团队', dataIndex: 'dept_id', width: 80 },
            { title: '只读', dataIndex: 'is_readonly', width: 70, render: (r: boolean) => (r ? <Tag color="orange">是</Tag> : '-') },
            { title: '最后活动', dataIndex: 'last_activity_at', width: 150, render: (t: string) => t ? t.slice(0, 16) : '-' },
            { title: '操作', width: 90, render: (_: any, row: any) => (
                <Popconfirm title="确认终止该会话？" onConfirm={() => adminApi.terminateSession(row.id).then(sessions.reload).catch(() => message.error('终止失败'))}>
                  <Button size="small" danger>终止</Button>
                </Popconfirm>
              ) },
          ]} dataSource={sessions.data?.sessions ?? []} />
      </div>
      <div className="card" style={{ padding: 16 }}>
        <h4 style={{ marginBottom: 8 }}>定时任务</h4>
        <Table rowKey="name" size="small" loading={jobs.loading} pagination={false}
          columns={[
            { title: '任务', dataIndex: 'name' },
            { title: '调度时间', dataIndex: 'schedule', width: 120 },
            { title: '状态', dataIndex: 'status', width: 100, render: (s: string) => (
                // L2（2026-08-12）：按状态映射颜色（原恒绿）
                <Tag color={s === 'running' || s === 'enabled' ? 'green' : s === 'disabled' || s === 'error' ? 'red' : 'orange'}>{s}</Tag>
              ) },
          ]} dataSource={jobs.data?.jobs ?? []} />
      </div>
    </div>
  )
}
