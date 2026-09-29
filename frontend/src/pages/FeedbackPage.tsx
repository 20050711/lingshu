// 反馈中心页（/feedback）：页面内左侧边栏（提交反馈 / 我的反馈）+ 右侧内容区
// 提交反馈：页面内完整表单（类型两级/内容/截图/联系方式，比快速反馈更全面）
// 我的反馈：正规表格列表（状态/类型/内容/时间/操作），展开行看详情与 admin 回复
import { useEffect, useMemo, useRef, useState } from 'react'
import { API_PREFIX } from '../api/prefix'
import { Popconfirm, Segmented, Select, Table, Upload, message } from 'antd'
import { UploadSimple } from '@phosphor-icons/react'
import DraggableModal from '../components/DraggableModal'
import Icon from '../components/Icon'
import client from '../api/client'

// 细分反馈类型（大类 → 子类，提交格式"大类-子类"；快速反馈浮窗已移除，此为本页唯一数据源）
export const FEEDBACK_TYPES: Record<string, string[]> = {
  '功能异常': ['工具执行失败', '产出物异常（图表/文档/图片）', '功能无响应'],
  '数据问题': ['查询结果错误', '数据不完整或过期', '统计口径疑问'],
  '界面问题': ['布局或样式异常', '白屏或页面打不开', '操作不流畅'],
  '功能建议': ['新增功能需求', '现有功能改进'],
  '账号权限': ['登录问题', '权限不符'],
  '性能问题': ['响应慢', '卡顿'],
  '其他': ['其他'],
}

interface FeedbackItem {
  id: number
  content: string
  status: string
  reply?: string | null
  operator?: string | null
  feedback_type?: string | null
  page?: string | null
  screenshots?: string[] | null
  processed_at?: string | null
  created_at?: string | null
}

const STATUS_META: Record<string, { label: string; color: string }> = {
  new: { label: '待处理', color: 'blue' },
  processing: { label: '处理中', color: 'orange' },
  done: { label: '已处理', color: 'green' },
  revoked: { label: '已撤销', color: 'default' },
}
const MAX_SHOTS = 4

function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 16).replace('T', ' ') : ''
}

/* ===== 提交反馈（页面内完整表单）===== */
function SubmitForm({ onSubmitted }: { onSubmitted: () => void }) {
  const [cat, setCat] = useState('功能异常')
  const [sub, setSub] = useState(FEEDBACK_TYPES['功能异常'][0])
  const [content, setContent] = useState('')
  const [contact, setContact] = useState('')
  const [fileList, setFileList] = useState<any[]>([])
  const [sending, setSending] = useState(false)

  const switchCat = (c: string) => { setCat(c); setSub(FEEDBACK_TYPES[c][0]) }

  const submit = async () => {
    if (!content.trim()) return message.warning('请填写反馈内容')
    if (content.trim().length < 5) return message.warning('反馈内容至少 5 个字')
    setSending(true)
    try {
      const fd = new FormData()
      fd.append('feedback_type', `${cat}-${sub}`)
      // C19（2026-08-12）：移除 page 死字段（本页恒为 /feedback，无复现价值）
      fd.append('content', content.trim())
      if (contact.trim()) fd.append('contact', contact.trim())
      fileList.forEach((f) => fd.append('screenshots', f.originFileObj || f))
      await client.post('/feedback', fd, { headers: { 'Content-Type': 'multipart/form-data' } })
      message.success('反馈已提交，管理员会尽快处理')
      setContent(''); setContact(''); setFileList([])
      onSubmitted()
    } catch {
      message.error('提交失败，请稍后重试')
    } finally {
      setSending(false)
    }
  }

  return (
    <div style={{ maxWidth: 640 }}>
      <h3 style={{ fontSize: 16, fontWeight: 600, color: 'var(--text-1)', margin: '0 0 4px' }}>提交反馈</h3>
      <p style={{ fontSize: 12, color: 'var(--text-3)', margin: '0 0 20px' }}>问题、建议或数据疑问都可以告诉我们，提交后可在"我的反馈"中查看处理进度</p>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 6 }}>反馈类型 <span style={{ color: 'var(--error)' }}>*</span></div>
        {/* AntD Select（替代原生 select——规避 Windows Chrome 原生下拉黑色 flash 渲染问题） */}
        <div style={{ display: 'flex', gap: 8 }}>
          <Select style={{ width: 160 }} value={cat}
            options={Object.keys(FEEDBACK_TYPES).map((c) => ({ value: c, label: c }))}
            onChange={switchCat} />
          <Select style={{ flex: 1 }} value={sub}
            options={(FEEDBACK_TYPES[cat] || []).map((s) => ({ value: s, label: s }))}
            onChange={setSub} />
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 6 }}>
          反馈内容 <span style={{ color: 'var(--error)' }}>*</span>
          <span style={{ float: 'right', color: 'var(--text-3)', fontSize: 11 }}>{content.length}/500</span>
        </div>
        <textarea
          className="chat-input"
          rows={5}
          maxLength={500}
          placeholder={'请描述问题或建议（按「功能/页面 → 操作步骤 → 实际现象 → 期望结果」描述，便于快速定位）'}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          style={{ width: '100%' }}
        />
        {/* 复现引导提示词：帮助运维复现问题 */}
        <div style={{ fontSize: 12, color: 'var(--text-2)', lineHeight: 1.9, marginTop: 8, background: 'var(--surface-inset)', borderRadius: 8, padding: '10px 14px', border: '1px solid var(--border)' }}>
          <div style={{ fontWeight: 600, color: 'var(--text-2)', marginBottom: 2 }}>为帮助平台快速定位问题，建议按以下格式描述：</div>
          <div>① 在哪个页面/功能：如「智能助手 - 生成图表」　② 怎么操作的：如「上传 Excel 后提问，点击生成」　③ 实际现象：如「一直转圈无结果 / 报错：xxx」　④ 期望结果：如「正常生成柱状图」</div>
          <div style={{ color: 'var(--text-3)', marginTop: 2 }}>附上截图更佳（最多 4 张），问题类反馈请尽量保留操作前后的页面状态</div>
        </div>
      </div>

      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 6 }}>截图（可选，最多 4 张）</div>
        <Upload
          listType="picture"
          fileList={fileList}
          beforeUpload={() => false}
          maxCount={MAX_SHOTS}
          accept="image/png,image/jpeg,image/gif,image/webp"
          onChange={({ fileList: fl }) => setFileList(fl.slice(0, MAX_SHOTS))}
        >
          <span style={{ fontSize: 12, color: 'var(--brand-ink)', cursor: 'pointer' }}><Icon as={UploadSimple} size={12} /> 选择截图</span>
        </Upload>
      </div>

      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 13, color: 'var(--text-2)', marginBottom: 6 }}>联系方式（可选）</div>
        <input className="login-input" placeholder="电话 / 邮箱，便于我们联系你" value={contact}
          onChange={(e) => setContact(e.target.value)} style={{ width: '100%', color: 'var(--text-1)' }} />
      </div>

      <button className="btn-primary" onClick={submit} disabled={sending} style={{ opacity: sending ? 0.6 : 1, padding: '8px 28px' }}>
        {sending ? '提交中…' : '提交反馈'}
      </button>
    </div>
  )
}

/* ===== 我的反馈（正规表格列表 + 展开行详情）===== */
function MyFeedback() {
  const [items, setItems] = useState<FeedbackItem[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('all')
  const [preview, setPreview] = useState<string | null>(null)
  const [shotUrls, setShotUrls] = useState<Record<string, string>>({})
  // 截图加载失败的 key：原来失败留白，看不出是"没有图"还是"没加载出来"
  const [shotErrs, setShotErrs] = useState<Record<string, boolean>>({})
  // L12：卸载时 revoke 全部截图 blob（原只增不减，内存持续增长）
  const shotUrlsRef = useRef(shotUrls)
  shotUrlsRef.current = shotUrls
  useEffect(() => {
    return () => { Object.values(shotUrlsRef.current).forEach((u) => URL.revokeObjectURL(u)) }
  }, [])

  const load = () => {
    setLoading(true)
    client.get('/feedback/my').then((r) => setItems(r.data?.items || [])).catch(() => message.error('加载失败'))
      .finally(() => setLoading(false))
  }
  useEffect(load, [])

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const it of items) c[it.status] = (c[it.status] || 0) + 1
    return c
  }, [items])
  const list = filter === 'all' ? items : items.filter((i) => i.status === filter)

  const revoke = (id: number) => {
    client.post(`/feedback/${id}/revoke`).then(() => { message.success('已撤销'); load() })
      .catch((e) => message.error(e.response?.data?.error?.message || '撤销失败'))
  }

  // C5（2026-08-12）：in-flight 去重 + 由 onExpand 事件触发（原 expandedRowRender 渲染期
  // 直接调用 → setState/重复请求）
  const shotInFlight = useRef<Set<string>>(new Set())
  const loadShot = (name: string, fid: number) => {
    if (shotUrls[name] || shotInFlight.current.has(name)) return
    shotInFlight.current.add(name)
    fetch(`${API_PREFIX}/feedback/photo?fid=${fid}&name=${encodeURIComponent(name)}`)  // L11：cookie 自动携带
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((b) => setShotUrls((u) => ({ ...u, [name]: URL.createObjectURL(b) })))
      .catch(() => setShotErrs((m) => ({ ...m, [name]: true })))
      .finally(() => shotInFlight.current.delete(name))
  }

  const columns = [
    {
      title: '状态', dataIndex: 'status', width: 90,
      render: (s: string) => {
        const m = STATUS_META[s] || STATUS_META.new
        return <span style={{ color: m.color === 'blue' ? 'var(--brand-ink)' : m.color === 'orange' ? 'var(--warning)' : m.color === 'green' ? 'var(--success)' : 'var(--text-3)', fontSize: 12 }}>● {m.label}</span>
      },
    },
    { title: '类型', dataIndex: 'feedback_type', width: 170, render: (v: string | null) => v || '其他' },
    { title: '内容', dataIndex: 'content', ellipsis: true, render: (v: string) => <span style={{ fontSize: 13, color: 'var(--text-1)' }}>{v}</span> },
    { title: '提交时间', dataIndex: 'created_at', width: 140, render: (v: string | null) => <span style={{ color: 'var(--text-2)', fontSize: 12 }}>{fmtTime(v)}</span> },
    {
      title: '操作', width: 100, render: (_: any, r: FeedbackItem) => (
        (r.status === 'new' || r.status === 'processing') ? (
          <Popconfirm title="撤销这条反馈？管理员将不再处理。" okText="撤销" cancelText="保留" onConfirm={() => revoke(r.id)}>
            <span style={{ fontSize: 12, color: 'var(--error)', cursor: 'pointer' }}>撤销</span>
          </Popconfirm>
        ) : (r.reply ? <span style={{ fontSize: 12, color: 'var(--success)' }}>已回复</span> : null)
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 16 }}>
        <h3 style={{ fontSize: 16, fontWeight: 600, color: 'var(--text-1)', margin: 0 }}>我的反馈</h3>
        <Segmented
          value={filter}
          onChange={(v) => setFilter(String(v))}
          options={[
            { label: `全部 ${items.length}`, value: 'all' },
            { label: `待处理 ${counts.new || 0}`, value: 'new' },
            { label: `处理中 ${counts.processing || 0}`, value: 'processing' },
            { label: `已处理 ${counts.done || 0}`, value: 'done' },
            { label: `已撤销 ${counts.revoked || 0}`, value: 'revoked' },
          ]}
        />
      </div>
      <Table
        rowKey="id" size="middle" loading={loading} dataSource={list} columns={columns}
        pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
        expandable={{
          // C5（2026-08-12）：展开事件触发截图加载（渲染期不发起 fetch）
          onExpand: (expanded, r) => { if (expanded) (r.screenshots || []).forEach((s) => loadShot(s, r.id)) },
          expandedRowRender: (r: FeedbackItem) => (
            <div style={{ padding: '4px 8px' }}>
              <div style={{ fontSize: 13, color: 'var(--text-1)', lineHeight: 1.8, whiteSpace: 'pre-wrap' }}>{r.content}</div>
              {r.page && <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 4 }}>来源页面：{r.page}</div>}
              {r.screenshots && r.screenshots.length > 0 && (
                <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
                  {r.screenshots.map((s, i) => {
                    const url = shotUrls[s]
                    if (!url) return shotErrs[s]
                      ? <span key={i} style={{ fontSize: 12, color: 'var(--text-3)' }}>图片加载失败</span>
                      : <span key={i} style={{ width: 64, height: 64, background: 'var(--surface-2)', borderRadius: 6, display: 'inline-block' }} />
                    return (
                      <img key={i} src={url} alt={`截图${i + 1}`}
                        onClick={() => setPreview(url)}
                        style={{ width: 64, height: 64, objectFit: 'cover', borderRadius: 6, border: '1px solid var(--border)', cursor: 'zoom-in' }} />
                    )
                  })}
                </div>
              )}
              {r.reply && (
                <div style={{ marginTop: 12, background: 'var(--brand-soft)', borderRadius: 8, padding: '10px 14px', borderLeft: '3px solid #1a56db' }}>
                  <div style={{ fontSize: 12, color: 'var(--brand-ink)', fontWeight: 600, marginBottom: 4 }}>
                    admin 回复{r.operator ? ` · ${r.operator}` : ''}
                    {r.processed_at ? <span style={{ color: 'var(--text-3)', fontWeight: 400, marginLeft: 8 }}>{fmtTime(r.processed_at)}</span> : null}
                  </div>
                  <div style={{ fontSize: 13, color: 'var(--text-1)', lineHeight: 1.7, whiteSpace: 'pre-wrap' }}>{r.reply}</div>
                </div>
              )}
            </div>
          ),
        }}
      />
      <DraggableModal open={!!preview} footer={null} onCancel={() => setPreview(null)} width={640}>
        {preview && <img src={preview} alt="预览" style={{ width: '100%', borderRadius: 8 }} />}
      </DraggableModal>
    </div>
  )
}

export default function FeedbackPage() {
  // 默认进入「提交反馈」（用户要求）
  const [view, setView] = useState<'submit' | 'list'>('submit')

  return (
    // 内容占满 AppShell 主区宽度（右侧不留空白）
    <div style={{ padding: '20px 16px 40px' }}>
      <h2 style={{ fontSize: 20, fontWeight: 700, color: 'var(--text-1)', margin: '0 0 14px' }}>反馈中心</h2>
      <div style={{ display: 'flex', gap: 14, minHeight: 500 }}>
        {/* 页面内左侧边栏 */}
        <div style={{ width: 140, flexShrink: 0 }}>
          <div style={{ background: 'var(--surface-1)', border: '1px solid var(--border)', borderRadius: 10, overflow: 'hidden' }}>
            {[{ key: 'submit' as const, label: '提交反馈' }, { key: 'list' as const, label: '我的反馈' }].map((m) => (
              <div key={m.key} onClick={() => setView(m.key)}
                style={{
                  padding: '12px 16px', fontSize: 13, cursor: 'pointer', userSelect: 'none',
                  color: view === m.key ? 'var(--brand-ink)' : 'var(--text-2)', fontWeight: view === m.key ? 600 : 400,
                  background: view === m.key ? 'var(--brand-soft)' : 'var(--surface-1)',
                  borderLeft: view === m.key ? '3px solid #1a56db' : '3px solid transparent',
                  transition: 'all 0.15s ease',
                }}>
                {m.label}
              </div>
            ))}
          </div>
        </div>
        {/* 右侧内容区 */}
        <div style={{ flex: 1, minWidth: 0, background: 'var(--surface-1)', border: '1px solid var(--border)', borderRadius: 10, padding: '18px 22px' }}>
          {view === 'submit'
            ? <SubmitForm onSubmitted={() => setView('list')} />
            : <MyFeedback />}
        </div>
      </div>
    </div>
  )
}
