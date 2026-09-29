// AI 外部工具页（/mcp，2026-09-03 起真实接入）
// 权限模型：运维（admin，运维后台管理）全局启停 → 员工在本页多选启用自己需要的
// 工具（显式启用制，默认关闭）；勾选后智能助手才可调用对应 MCP 工具
import { useEffect, useState } from 'react'
import { Button, Checkbox, message } from 'antd'
import client from '../api/client'
import Icon, { iconFromKey } from '../components/Icon'

interface Mcp {
  id: string
  name: string
  icon: string
  description: string
  status: string
  url?: string | null
}

const STATUS_TAG: Record<string, { text: string; color: string }> = {
  active: { text: '已启用（全局）', color: 'green' },
  disabled: { text: '运维停用', color: 'default' },
  planning: { text: '规划中', color: 'default' },
}

export default function McpPage() {
  const [tools, setTools] = useState<Mcp[]>([])
  const [myEnabled, setMyEnabled] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [saving, setSaving] = useState(false)
  const [dirty, setDirty] = useState(false)

  const load = () => {
    setLoading(true)
    setError(false)
    client.get('/mcp/tools').then((r) => {
      setTools(r.data.tools || [])
      setMyEnabled(r.data.my_enabled || [])
    }).catch(() => setError(true)).finally(() => setLoading(false))
  }
  useEffect(load, [])

  const activeTools = tools.filter((t) => t.status === 'active')
  const others = tools.filter((t) => t.status !== 'active')

  const toggle = (id: string, checked: boolean) => {
    setMyEnabled((s) => checked ? [...s, id] : s.filter((x) => x !== id))
    setDirty(true)
  }
  const selectAll = () => {
    setMyEnabled(activeTools.map((t) => t.id))
    setDirty(true)
  }
  const clearAll = () => {
    setMyEnabled([])
    setDirty(true)
  }
  const save = async () => {
    setSaving(true)
    try {
      await client.put('/mcp/tools/prefs', { tool_ids: myEnabled })
      message.success('已保存——启用后智能助手即可调用这些外部工具')
      setDirty(false)
    } catch (e: any) {
      message.error(e.response?.data?.error?.message || '保存失败')
    } finally { setSaving(false) }
  }

  return (
    <div>
      <h2 style={{ fontSize: 19, marginBottom: 6, fontWeight: 650, letterSpacing: '-0.01em' }}>MCP 工具</h2>
      <p style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 16 }}>
        通过 MCP 协议接入的外部工具。管理员负责全局启停；勾选自己需要启用的工具（默认关闭），
        保存后智能助手在对话中即可调用
      </p>
      {loading && <p style={{ fontSize: 12, color: 'var(--text-3)' }}>加载中…</p>}
      {!loading && error && (
        <p style={{ fontSize: 12, color: 'var(--error)' }}>
          工具列表加载失败
          <span style={{ color: 'var(--primary)', cursor: 'pointer', marginLeft: 6 }} onClick={load}>点击重试</span>
        </p>
      )}
      {!loading && !error && tools.length === 0 && (
        <p style={{ fontSize: 12, color: 'var(--text-3)' }}>暂无外部工具</p>
      )}

      {!loading && !error && (
        <>
          {/* 可启用区：运维已启用的工具（多选） */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
            <b style={{ fontSize: 14 }}>可用工具（{activeTools.length}）</b>
            <Button size="small" onClick={selectAll} disabled={activeTools.length === 0}>全部启用</Button>
            <Button size="small" onClick={clearAll}>全部停用</Button>
            <Button size="small" type="primary" loading={saving} disabled={!dirty} onClick={save}>保存设置</Button>
            {dirty && <span style={{ fontSize: 12, color: 'var(--warning)' }}>有未保存的修改</span>}
          </div>
          <div className="row-list">
            {activeTools.map((t) => {
              const on = myEnabled.includes(t.id)
              return (
                <div key={t.id} className="row-item">
                  {/* 图标名来自后端（语义名，见 ICON_KEYS）——这里映射成 Phosphor 组件 */}
                  <span className="row-icon"><Icon as={iconFromKey(t.icon)} size={17} /></span>
                  <div className="row-main">
                    <div className="row-title">{t.name}</div>
                    <div className="row-desc">{t.description}</div>
                  </div>
                  <span className="row-tail">
                    <Checkbox checked={on} onChange={(e) => toggle(t.id, e.target.checked)}>
                      {on ? '已启用' : '启用'}
                    </Checkbox>
                  </span>
                </div>
              )
            })}
            {activeTools.length === 0 && (
              <div className="row-item" style={{ justifyContent: 'center', color: 'var(--text-3)', fontSize: 12.5 }}>
                暂无已启用的外部工具——管理员在「MCP 工具」后台登记并启用后会出现在这里
              </div>
            )}
          </div>

          {/* 其他状态区 */}
          {others.length > 0 && (
            <>
              <h3 style={{ fontSize: 14, margin: '24px 0 8px' }}>其他</h3>
              <div className="row-list">
                {others.map((t) => {
                  const st = STATUS_TAG[t.status] || { text: t.status, color: 'default' }
                  return (
                    <div key={t.id} className="row-item disabled">
                      <span className="row-icon"><Icon as={iconFromKey(t.icon)} size={17} /></span>
                      <div className="row-main">
                        <div className="row-title">{t.name}</div>
                        <div className="row-desc">{t.description}</div>
                      </div>
                      <span className="row-tail"><span className="badge badge-gray">{st.text}</span></span>
                    </div>
                  )
                })}
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}
