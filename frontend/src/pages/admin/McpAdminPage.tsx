// 运维：AI 外部工具（MCP）管理（2026-09-03 /admin/mcp）
// 运维全局启停/登记 url/连接测试；员工启停在其个人页（McpPage），本页不管
import { useEffect, useState } from 'react'
import { Button, Input, message, Modal, Popconfirm, Select, Switch, Table, Tag } from 'antd'
import { mcpApi } from '../../api/mcp'
import Icon, { iconFromKey } from '../../components/Icon'

interface McpTool {
  id: string
  name: string
  icon?: string | null
  description?: string | null
  url?: string | null
  status: string
  sort_order?: number
  /** 2026-09-10：平台网关托管（进程由后端自动拉起，无 url）——仍应显示启停开关/连接测试 */
  platform?: boolean
}

const STATUS_MAP: Record<string, { text: string; color: string }> = {
  active: { text: '启用', color: 'green' },
  disabled: { text: '停用', color: 'default' },
  planning: { text: '规划中', color: 'orange' },
}

export default function McpAdminPage() {
  const [tools, setTools] = useState<McpTool[]>([])
  const [loading, setLoading] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)
  const [edit, setEdit] = useState<McpTool | null>(null)
  const [creating, setCreating] = useState(false)
  const [form, setForm] = useState({ id: '', name: '', icon: 'tool', description: '', url: '' })
  // 2026-09-18：图标可选值由后端下发（ICON_KEYS）——原先是自由文本框，运维填 emoji 就从这条路
  // 漏到员工页上（前端只认语义名）。下拉既堵住入口，也避免前端再存一份清单。
  const [iconKeys, setIconKeys] = useState<string[]>([])

  const load = () => {
    setLoading(true)
    mcpApi.adminList().then((d) => setTools(d.tools || [])).catch(() => message.error('加载失败'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [])
  useEffect(() => {
    mcpApi.iconKeys().then((d) => setIconKeys(d.keys || [])).catch(() => setIconKeys(['tool']))
  }, [])

  const toggle = async (t: McpTool, active: boolean) => {
    try {
      if (active && !t.url && !t.platform) { message.warning('请先配置 MCP server url'); return }
      await mcpApi.adminUpdate(t.id, { status: active ? 'active' : 'disabled' })
      message.success(active ? '已启用（员工可在 AI外部工具页自行勾选）' : '已停用')
      load()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '操作失败')
    }
  }

  const test = async (t: McpTool) => {
    setTesting(t.id)
    try {
      const r = await mcpApi.adminTest(t.id)
      const s = r.server || {}
      Modal.info({ title: `连接正常：${s.name || t.name} v${s.version || ''}`,
        content: `MCP 工具：${(r.tools || []).join('、') || '（无）'}` })
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '连接失败')
    } finally { setTesting(null) }
  }

  const submit = async () => {
    try {
      if (edit) {
        await mcpApi.adminUpdate(edit.id, { name: form.name, icon: form.icon, description: form.description, url: form.url || undefined })
      } else {
        if (!form.id.trim() || !form.name.trim()) { message.warning('id 与名称必填'); return }
        await mcpApi.adminCreate({ id: form.id.trim(), name: form.name.trim(), icon: form.icon, description: form.description, url: form.url || undefined })
      }
      message.success('已保存')
      setEdit(null); setCreating(false)
      load()
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '保存失败')
    }
  }

  const columns = [
    { title: '工具', dataIndex: 'name', width: 150, render: (_: string, r: McpTool) => (
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
        <Icon as={iconFromKey(r.icon)} /> {r.name}
      </span>
    ) },
    { title: '状态', dataIndex: 'status', width: 90, render: (v: string) => (
      <Tag color={STATUS_MAP[v]?.color}>{STATUS_MAP[v]?.text}</Tag>
    ) },
    { title: 'MCP server', dataIndex: 'url', ellipsis: true, render: (v: string | null | undefined, r: McpTool) =>
      v ? <span style={{ fontSize: 12, fontFamily: 'monospace' }}>{v}</span>
        : r.platform ? <span style={{ fontSize: 12, color: 'var(--text-2)' }}>平台网关托管（进程自动拉起）</span>
          : <span style={{ color: 'var(--text-3)' }}>未接入</span> },
    { title: '说明', dataIndex: 'description', ellipsis: true },
    { title: '运维操作', width: 300, render: (_: unknown, r: McpTool) => (
      <>
        {(r.url || r.platform) && (
          <>
            <Switch size="small" checked={r.status === 'active'} onChange={(c) => toggle(r, c)}
              checkedChildren="启用" unCheckedChildren="停用" />
            <Button size="small" type="link" loading={testing === r.id} onClick={() => test(r)}>测试连接</Button>
          </>
        )}
        <Button size="small" type="link" onClick={() => { setEdit(r); setForm({ id: r.id, name: r.name, icon: r.icon || 'tool', description: r.description || '', url: r.url || '' }) }}>编辑</Button>
        <Popconfirm title={`删除外部工具 ${r.id}？`} onConfirm={() => mcpApi.adminDelete(r.id).then(() => { message.success('已删除'); load() }).catch(() => message.error('删除失败'))}>
          <Button size="small" type="link" danger>删除</Button>
        </Popconfirm>
      </>
    ) },
  ]

  return (
    <div>
      <h2 style={{ fontSize: 18, marginBottom: 4 }}>AI 外部工具（MCP）</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>
        登记/启停外部 MCP server（运维全局闸门）。启用后员工在「AI 外部工具」页自行勾选，智能助手才可调用。
        连接地址为 MCP Streamable HTTP（如 http://127.0.0.1:5556/mcp）
      </p>
      <Button type="primary" style={{ marginBottom: 12 }} onClick={() => { setCreating(true); setEdit(null); setForm({ id: '', name: '', icon: 'tool', description: '', url: '' }) }}>登记新外部工具</Button>
      <div className="card" style={{ padding: 12 }}>
        <Table size="small" rowKey="id" loading={loading} dataSource={tools} columns={columns as any} pagination={false} />
      </div>

      <Modal title={edit ? `编辑：${edit.id}` : '登记新外部工具'} open={creating || !!edit}
        onCancel={() => { setCreating(false); setEdit(null) }} onOk={submit} okText="保存" width={520}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 90, fontSize: 12 }}>工具 id *</span>
            <Input placeholder="唯一标识，如 browser" value={form.id} disabled={!!edit}
              onChange={(e) => setForm({ ...form, id: e.target.value })} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 90, fontSize: 12 }}>名称 *</span>
            <Input placeholder="如：浏览器自动化" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 90, fontSize: 12 }}>图标</span>
            <Select style={{ width: 150 }} value={form.icon} onChange={(v) => setForm({ ...form, icon: v })}
              options={iconKeys.map((k) => ({
                value: k,
                label: <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                  <Icon as={iconFromKey(k)} /> {k}
                </span>,
              }))} />
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ width: 90, fontSize: 12 }}>MCP url</span>
            <Input placeholder="http://127.0.0.1:5556/mcp" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
            <span style={{ width: 90, fontSize: 12, paddingTop: 4 }}>说明</span>
            <Input.TextArea rows={2} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
        </div>
      </Modal>
    </div>
  )
}
