// 简历初筛工具（M7）：批量上传 → JD + 7 维度权重 → LLM 评分排序 TOP K → ZIP 导出
import { useCallback, useEffect, useRef, useState } from 'react'
import { API_PREFIX } from '../../api/prefix'
import {
  Button, Input, InputNumber, List, message, Popconfirm, Select, Slider, Space, Table, Upload,
} from 'antd'
import { CloudArrowUp, FileText } from '@phosphor-icons/react'
import Icon from '../../components/Icon'
import { errMessage } from '../../api/client'
import { toolsApi } from '../../api/tools'
import { downloadByUrl } from '../../lib/download'
import PageHeader from '../../components/PageHeader'
import DraggableModal from '../../components/DraggableModal'

// 2026-09-04：评分完成自动弹浮窗去重（同一批只自动弹一次；手动打开不受限）
const _resumePopped = new Set<string>()
// 2026-09-04（同类排查）：用户手动打开过该批次 → 完成态不再自动弹
const _resumeViewedManually = new Set<string>()

const { TextArea } = Input
const DIMS = [
  { key: 'professional', label: '专业技能' },
  { key: 'experience', label: '工作经验' },
  { key: 'education', label: '教育背景' },
  { key: 'project', label: '项目经历' },
  { key: 'communication', label: '沟通能力' },
  { key: 'teamwork', label: '团队协作' },
  { key: 'learning', label: '学习能力' },
]
const MAX_FILES = 20

const isFinal = (s: string) => ['done', 'failed'].includes(s)

export default function ResumeTool() {
  const [files, setFiles] = useState<File[]>([])
  const [jd, setJd] = useState('')
  // D3：批次级评分辅助模型选择（2026-08-18：加 thinking 思考强度）
  const [auxModel, setAuxModel] = useState<{ platform: string; model: string; thinking?: string | null } | null>(null)
  const [modelOptions, setModelOptions] = useState<{ llm_aux: any[]; thinking: any[] }>({ llm_aux: [], thinking: [] })
  const [weights, setWeights] = useState<Record<string, number>>(
    Object.fromEntries(DIMS.map((d) => [d.key, 1])),
  )
  const [topK, setTopK] = useState(10)
  const [batch, setBatch] = useState<any>(null)
  const batchRef = useRef<any>(null)   // 轮询比对用（内容没变就不 setState）
  batchRef.current = batch
  const [loading, setLoading] = useState(false)
  // 历史记录（切页后恢复）
  const [history, setHistory] = useState<any[]>([])
  const [showHistory, setShowHistory] = useState(() => sessionStorage.getItem('__hist_resume') === '1')  // 2026-09-04：会话级持久化（切页/刷新保持；新标签页/重登默认收起）
  // 2026-09-04：历史批次详情浮窗 state
  const [viewBatch, setViewBatch] = useState<null | { batch: any; items: any[] }>(null)
  const pollRef = useRef<number | null>(null)

  // M14：卸载时停止轮询（防卸载后 interval 继续请求与 setState）
  useEffect(() => {
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [])
  // 历史记录：挂载加载一次（loadHistory 供提交/终态复用——2026-09-04）
  const loadHistory = useCallback(() => {
    toolsApi.listResumeBatches().then((d) => setHistory(d.batches || [])).catch(() => {})
  }, [])
  // 历史记录挂载加载；"有进行中批次就接管轮询"放在 fetchBatch 定义之后（见下方）
  // D3：模型候选（与 QA 浮窗同源 /models/options）
  useEffect(() => {
    fetch(`${API_PREFIX}/models/options`).then((r) => (r.ok ? r.json() : Promise.reject())).then(setModelOptions).catch(() => {})
  }, [])

  // 2026-09-04：历史批次详情浮窗（点击行/「打开」→ 浮窗查看，不再切到主区）
  const openHistoryBatch = async (id: string) => {
    _resumeViewedManually.add(id)  // 手动打开过 → 完成态不再自动弹这一批
    try {
      const data = await toolsApi.getResumeBatch(id)
      setViewBatch({ batch: data.batch, items: data.items || [] })
    } catch (e) {
      message.error(errMessage(e))
    }
  }

  // 清空历史（删除全部终态批次）
  const clearHistory = async () => {
    const finals = history.filter((h) => isFinal(h.status))
    for (const h of finals) {
      try { await toolsApi.deleteResumeBatch(h.batch_id) } catch { /* 单条失败跳过 */ }
    }
    setHistory(history.filter((h) => !isFinal(h.status)))
    message.success(`已清空 ${finals.length} 条历史记录`)
  }

  const fetchBatch = useCallback(async (id: string) => {
    try {
      const data = await toolsApi.getResumeBatch(id)
      // 走查"页面卡"：内容没变就跳过 setState（原实现每 3 秒用新对象刷新整页）
      if (JSON.stringify(data.batch) !== JSON.stringify(batchRef.current)) setBatch(data.batch)
      if (isFinal(data.batch.status) && pollRef.current) {
        window.clearInterval(pollRef.current)
        pollRef.current = null
        loadHistory()  // 2026-09-04：终态刷新历史（进度展示已移到历史行）
        // 2026-09-04：评分完成用户仍在页面（可见）→ 自动弹结果浮窗（去重；浮窗开着则原位刷新）
        if (document.visibilityState !== 'hidden' && !_resumePopped.has(id) && !_resumeViewedManually.has(id)) {
          _resumePopped.add(id)
          openHistoryBatch(id)
        }
      }
    } catch (e) {
      message.error(errMessage(e))
      if (pollRef.current) window.clearInterval(pollRef.current)
    }
  }, [loadHistory, openHistoryBatch])

  // 进行中批次接管（走查）：挂载/切页回来时，列表里若有未完成批次就**恢复轮询**——
  // 原实现只在 submit 里 window.setInterval，重新进入后进度永远停在离开时那一眼。
  useEffect(() => {
    let alive = true
    toolsApi.listResumeBatches().then((d) => {
      if (!alive) return
      const list = d.batches || []
      setHistory(list)
      const active = list.find((b: any) => !isFinal(b.status))
      if (active) {
        setBatch({ batch_id: active.batch_id, status: active.status })
        if (pollRef.current) window.clearInterval(pollRef.current)
        pollRef.current = window.setInterval(() => fetchBatch(active.batch_id), 3000)
      }
    }).catch(() => { /* 列表失败不打扰 */ })
    return () => { alive = false }
  }, [fetchBatch])

  const submit = async () => {
    if (!files.length) return message.warning('请先上传简历文件')
    setLoading(true)
    try {
      const data = await toolsApi.createResumeBatch(files)
      if (data.duplicates?.length) message.warning(`已跳过重复文件：${data.duplicates.join('、')}`)
      const scoreData = await toolsApi.scoreResumeBatch(data.batch_id, jd, weights, topK, auxModel)
      void scoreData
      setBatch({ batch_id: data.batch_id, status: 'queued' })
      setFiles([])
      setShowHistory(true)  // 2026-09-04：提交后展开历史（结果主区已移除，靠历史行+完成浮窗）
      loadHistory()
      if (pollRef.current) window.clearInterval(pollRef.current)
      pollRef.current = window.setInterval(() => fetchBatch(data.batch_id), 3000)
    } catch (e) {
      message.error(errMessage(e))
    } finally {
      setLoading(false)
    }
  }

  const exportZip = async (batchId?: string) => {
    const bid = batchId || batch?.batch_id
    if (!bid) return
    try {
      // 2026-09-15：直链下载（流式落盘/原生进度/可续传，不占 JS 内存）——原 fetch→blob
      downloadByUrl(toolsApi.resumeExportUrl(bid), `resume_rank_${bid.slice(0, 8)}.zip`)
    } catch (e) {
      message.error(errMessage(e))
    }
  }

  const columns = [
    { title: '排名', dataIndex: 'rank', width: 60, render: (v: number) => v ?? '-' },
    { title: '文件名', dataIndex: 'file_name', ellipsis: true, width: 220 },
    ...DIMS.map((d) => ({ title: d.label, dataIndex: ['dim_scores', d.key], width: 70, render: (v: number) => v ?? '-' })),
    { title: '总分', dataIndex: 'total_score', width: 70, sorter: (a: any, b: any) => (a.total_score || 0) - (b.total_score || 0) },
    {
      title: '匹配度', width: 80,
      render: (_: any, r: any) => r.total_score != null ? `${Math.round((r.total_score / 10) * 100)}%` : '-',
    },
    { title: '状态', dataIndex: 'status', width: 80, render: (v: string) => v === 'failed' ? <span style={{ color: 'var(--error)' }}>解析失败</span> : v },
  ]

  return (
    <div>
      <PageHeader title={<><Icon as={FileText} size={18} /> 简历初筛</>}
        sub="批量上传简历（pdf/docx/jpg），设定 JD 与 7 维度权重，AI 评分排序并导出 TOP K" />
      <div style={{ maxWidth: 1124, marginInline: 'auto' }}>
      {/* 2026-09-18 走查：左右分栏——左=简历素材输入 / 右=JD 与评分设定 */}
      <div className="tool-split">
        {/* 左栏：简历素材 */}
        <div className="card tool-col">
          <Upload.Dragger
            multiple
            fileList={files.map((f, i) => ({ uid: String(i), name: f.name, size: f.size, status: 'done' as const }))}
            onRemove={(f) => { setFiles((fs) => fs.filter((x) => x.name !== f.name)); return true }}
            beforeUpload={(f) => {
              const ext = f.name.split('.').pop()?.toLowerCase() || ''
              if (!['pdf', 'docx', 'jpg', 'jpeg', 'png'].includes(ext)) {
                message.error(`不支持 ${f.name}（支持 pdf/docx/jpg/png）`)
                return Upload.LIST_IGNORE
              }
              if (files.length >= MAX_FILES) {
                message.warning(`最多 ${MAX_FILES} 份简历`)
                return Upload.LIST_IGNORE
              }
              return false
            }}
            onChange={({ fileList: fl }) => setFiles(fl.filter((f) => f.originFileObj).map((f) => f.originFileObj!))}
            style={{ padding: 12 }}
          >
            <p className="ant-upload-drag-icon"><Icon as={CloudArrowUp} size={48} className="anticon" /></p>
            <p className="ant-upload-text">点击或拖拽上传简历</p>
            <p className="ant-upload-hint">pdf/docx/jpg/png，单批 ≤{MAX_FILES} 份（自动去重）</p>
          </Upload.Dragger>
        </div>

        {/* 右栏：JD 与评分设定 */}
        <div className="card tool-col">
          <div className="tool-field">
            <span className="tool-field-label">职位描述（JD）</span>
            <TextArea rows={3} placeholder="如：招聘高级数据分析师，要求精通 Python/SQL/机器学习…"
              value={jd} onChange={(e) => setJd(e.target.value)} style={{ fontSize: 12 }} />
          </div>

          {/* 2026-09-04（用户要求）：7 个权重条占比过大 → 紧凑网格；
              2026-09-18：右栏变窄，3 列改 2 列，滑块才有拖动空间 */}
          <div className="tool-field">
            <span className="tool-field-label">评分维度权重（0-5）</span>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: '2px 18px' }}>
              {DIMS.map((d) => (
                <div key={d.key}>
                  <div style={{ fontSize: 12, color: 'var(--text-2)', display: 'flex', justifyContent: 'space-between' }}>
                    <span>{d.label}</span><span style={{ color: 'var(--brand-ink)' }}>{weights[d.key]}</span>
                  </div>
                  <Slider min={0} max={5} step={1} value={weights[d.key]}
                    onChange={(v) => setWeights((w) => ({ ...w, [d.key]: v }))} />
                </div>
              ))}
            </div>
          </div>

          {/* D3：模型选择框（评分辅助模型；空=团队配置档；antd Select 替代原生弹层黑闪） */}
          <div className="tool-params">
            <div className="tool-param-row">
              <span className="tool-param-label">评分模型</span>
              <Select size="small" style={{ flex: 1, minWidth: 0 }}
                value={auxModel ? `${auxModel.platform}|${auxModel.model}` : ''}
                onChange={(v) => {
                  if (!v) { setAuxModel(null); return }
                  const [platform, model] = String(v).split('|')
                  setAuxModel({ platform, model, thinking: null })
                }}
                options={[
                  { value: '', label: '默认（团队配置档）' },
                  ...(modelOptions.llm_aux || []).map((m: any) => ({
                    value: `${m.platform}|${m.model}`, label: `${m.model}（${m.platform}）`,
                  })),
                ]} />
            </div>
            {/* 2026-08-18：思考强度（deepseek 文本模型生效） */}
            {auxModel?.platform === 'deepseek' && (
              <div className="tool-param-row">
                <span className="tool-param-label">思考强度</span>
                <Select size="small" style={{ flex: 1, minWidth: 0 }}
                  value={auxModel.thinking === null || auxModel.thinking === undefined ? 'default' : auxModel.thinking}
                  onChange={(v) => setAuxModel({ ...auxModel, thinking: v === 'default' ? null : String(v) })}
                  options={[
                    { value: 'default', label: '默认（跟随配置）' },
                    ...(modelOptions.thinking || []).filter((t: any) => t.key !== null).map((t: any) => ({
                      value: String(t.key), label: t.label,
                    })),
                  ]} />
              </div>
            )}
          </div>

          <Space>
            <span style={{ fontSize: 12, color: 'var(--text-2)' }}>TOP K（导出排名前 N 份）</span>
            <InputNumber min={1} max={20} value={topK} onChange={(v) => setTopK(v ?? 10)} size="small" />
            <Button type="primary" loading={loading} onClick={submit}>开始初筛</Button>
            {/* 2026-09-11（走查）：原「历史记录」按钮（只做 loadHistory）与下方历史区自带的
                「展开/收起」重复，移除；列表仍在提交后/挂载时自动刷新 */}
          </Space>
        </div>
      </div>

      {/* 历史记录（2026-09-04：统一「标题行 + 收起/展开 + 列表」，行点击浮窗） */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
          <strong style={{ fontSize: 14 }}>历史记录（{history.length}）</strong>
          <Space size={8}>
            {showHistory && (
              <Popconfirm title="清空历史记录？" description={`将删除全部 ${history.filter((h) => isFinal(h.status)).length} 条已完成批次记录`}
                onConfirm={clearHistory}>
                <Button size="small" type="link" danger disabled={!history.some((h) => isFinal(h.status))}>清空历史</Button>
              </Popconfirm>
            )}
            <Button size="small" type="link" onClick={() => setShowHistory((v) => { const nxt = !v; sessionStorage.setItem('__hist_resume', nxt ? '1' : '0'); return nxt })}>{showHistory ? '收起' : '展开'}</Button>
          </Space>
        </div>
        {showHistory && (
          <List size="small" dataSource={history} locale={{ emptyText: '暂无历史记录' }}
            renderItem={(h) => (
              <List.Item style={{ cursor: 'pointer' }} onClick={() => openHistoryBatch(h.batch_id)}
                actions={[
                  <Button key="open" size="small" type="link" onClick={(e) => { e.stopPropagation(); openHistoryBatch(h.batch_id) }}>查看</Button>,
                  // 2026-09-11（同类修复，会议走查）：Popconfirm 确认按钮在 portal 里，点击仍沿
                  // React 树冒泡到整行 onClick——必须在外层 span（React 树父节点）拦截
                  <span key="del" onClick={(e) => e.stopPropagation()}>
                    <Popconfirm title="删除该批次记录？" onConfirm={async () => {
                      try { await toolsApi.deleteResumeBatch(h.batch_id); setHistory((hs) => hs.filter((x) => x.batch_id !== h.batch_id)) }
                      catch (e) { message.error(errMessage(e)) }
                    }}>
                      <Button size="small" type="link" danger>删除</Button>
                    </Popconfirm>
                  </span>,
                ]}>
                <List.Item.Meta
                  title={<span>{h.batch_id.slice(0, 8)} · {h.done_count}/{h.item_count} 份完成 · {h.status}</span>}
                  description={h.created_at?.slice(0, 16) || ''}
                />
              </List.Item>
            )} />
        )}
      </div>

      {/* 2026-09-04：历史批次详情浮窗（主区结果 Table 已移除——评分结果全在浮窗查看/导出） */}
      <DraggableModal title={`批次详情 ${viewBatch?.batch?.batch_id?.slice(0, 8) ?? ''}`}
        open={!!viewBatch} onCancel={() => setViewBatch(null)} footer={null} width={880}>
        {viewBatch && (
          <div style={{ fontSize: 12 }}>
            <Space size={8} style={{ marginBottom: 12, flexWrap: 'wrap' }}>
              <span style={{ color: 'var(--text-3)' }}>{viewBatch.batch?.created_at?.slice(0, 16) || ''} · {viewBatch.items.length} 份</span>
              {viewBatch.items.length > 0 && <span>{viewBatch.items.filter((i: any) => i.total_score != null).length}/{viewBatch.items.length} 已评分</span>}
            </Space>
            {viewBatch.items.length > 0 && (
              <div style={{ textAlign: 'right', marginBottom: 8 }}>
                <Button size="small" type="primary" onClick={() => exportZip(viewBatch.batch.batch_id)}>导出 ZIP</Button>
              </div>
            )}
            {/* 2026-09-04（用户要求）：鼠标滚轮横滚明细——包一层容器处理（antd Table 无 onWheel prop） */}
            <div onWheel={(e: any) => {
              const t = e.nativeEvent?.target as HTMLElement | null
              const body = (t?.closest('.ant-table-container') || t)?.querySelector?.('.ant-table-body') as HTMLElement | null
              if (body) { body.scrollLeft += e.deltaY; e.preventDefault() }
            }}>
              <Table size="small" rowKey="item_id" dataSource={viewBatch.items} columns={columns as any}
                pagination={false} scroll={{ x: 880 }}
                style={{ maxHeight: 420, overflow: 'auto' }}
                expandable={{
                  // 2026-09-04（用户反馈 bug）：批次详情浮窗迁移时遗漏评语展开——补回 AI 文本分析
                  expandedRowRender: (r: any) => (
                    <div style={{ fontSize: 12, color: 'var(--text-2)', whiteSpace: 'pre-wrap' }}>
                      <strong>AI 评语：</strong>{r.comment || '（无）'}
                      {r.error && <div style={{ color: 'var(--error)', marginTop: 4 }}>错误：{r.error}</div>}
                    </div>
                  ),
                }} />
            </div>
          </div>
        )}
      </DraggableModal>
      </div>
    </div>
  )
}
