// 工具下载（2026-09-04）：离线工具包 zip 下载（列表由后端 tool_downloads 表管理，运维后台上传）
// 2026-09-04：布局对齐运维后台（Card + Table）；员工端仅浏览 + 下载
import { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Card, Table, Tooltip } from 'antd'
import { DownloadSimple, FileZip, Toolbox, Warning } from '@phosphor-icons/react'
import client from '../api/client'
import { downloadByUrl } from '../lib/download'
import { API_PREFIX } from '../api/prefix'
import PageHeader from '../components/PageHeader'
import PageLoading from '../components/PageLoading'
import DraggableModal from '../components/DraggableModal'
import Icon from '../components/Icon'

interface ToolFile {
  id: string
  title?: string
  description?: string
  filename: string
  size: number
  mtime: number
}

const fmtSize = (n: number) => {
  if (!n) return '-'
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024).toFixed(0)} KB`
}

export default function ToolsDownloadPage() {
  const [files, setFiles] = useState<ToolFile[]>([])
  const [viewing, setViewing] = useState<ToolFile | null>(null)  // 2026-09-04：简介过长 → 行「查看」浮窗展示全文
  const [loading, setLoading] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    client.get('/tools/downloads').then((r) => setFiles(r.data?.files || []))
      .catch(() => { /* 目录未配置时为空 */ })
      .finally(() => setLoading(false))
  }, [])
  useEffect(() => { load() }, [load])

  // 2026-09-15：改直链下载（流式落盘/原生进度/可续传，不占 JS 内存）——原 axios blob 整包进内存
  const download = (f: ToolFile) => {
    downloadByUrl(`${API_PREFIX}/tools/downloads/${encodeURIComponent(f.filename)}`, f.filename)
  }

  // 2026-09-17（用户报「两页布局不一样」）：简介列原先**没设宽度**，描述一长就把整行撑变形
  // （两机数据不同 → 观感不同）。改为固定列宽 + 简介两行截断（悬浮看全文、点「查看」看完整）。
  const ellipsis1: React.CSSProperties = {
    display: 'inline-block', maxWidth: '100%', overflow: 'hidden',
    textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom',
  }
  const clamp2: React.CSSProperties = {
    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
    overflow: 'hidden', fontSize: 12, lineHeight: 1.6, color: 'var(--text-2)',
  }

  const columns = [
    {
      title: '名称', dataIndex: 'title', width: 180,
      render: (v?: string, f?: ToolFile) => (
        <Tooltip title={v || f?.filename}>
          <span style={{ ...ellipsis1, fontWeight: 600 }}>{v || f?.filename}</span>
        </Tooltip>
      ),
    },
    {
      title: '简介', dataIndex: 'description',
      render: (v?: string) => v
        ? (
          <Tooltip title={v.length > 40 ? v : ''} placement="topLeft">
            <div style={clamp2}>{v}</div>
          </Tooltip>
        )
        : <span style={{ color: 'var(--text-3)' }}>-</span>,
    },
    {
      title: '文件', dataIndex: 'filename', width: 240,
      render: (v: string, f: ToolFile) => (
        <Tooltip title={v}>
          <span style={{ ...ellipsis1, fontSize: 12 }}>
            {v}<span style={{ color: 'var(--text-3)' }}>（{fmtSize(f.size)}）</span>
          </span>
        </Tooltip>
      ),
    },
    {
      title: '更新时间', dataIndex: 'mtime', width: 110,
      render: (v: number) => v ? (
        <Tooltip title={new Date(v * 1000).toLocaleString()}>
          <span style={{ fontSize: 12, color: 'var(--text-3)' }}>{new Date(v * 1000).toLocaleDateString()}</span>
        </Tooltip>
      ) : '-',
    },
    {
      title: '操作', key: 'actions', width: 130,
      render: (_: unknown, f: ToolFile) => (
        <>
          <Button size="small" type="link" style={{ padding: 0, marginRight: 10 }} onClick={() => setViewing(f)}>查看</Button>
          <Button size="small" type="primary" icon={<Icon as={DownloadSimple} />}
            onClick={() => download(f)}>下载</Button>
        </>
      ),
    },
  ]

  return (
    <div>
      <PageHeader title={<><Icon as={Toolbox} size={18} /> 工具下载</>} backLabel="返回定制化工具"
        sub={`离线工具包下载（运维后台上传管理——工具包与简介说明）`} />
      <div style={{ maxWidth: 1124, marginInline: 'auto' }}>
        {/* 工具包使用须知：管理员统一维护，内部使用；下载与安装问题走反馈 */}
        <Alert type="warning" showIcon style={{ marginBottom: 12 }}
          message={<b style={{ fontSize: 16 }}><Icon as={Warning} /> 使用须知</b>}
          description={
            <div style={{ fontSize: 13.5, lineHeight: 1.9 }}>
              工具包由管理员统一维护，仅供内部办公使用，请勿对外传播或用于商业用途。
              <br />
              下载前请核对名称与版本；安装与使用中如遇问题，可通过「反馈」提交，我们会尽快跟进处理。
            </div>
          } />
        <PageLoading show={loading && files.length === 0} rows={2} />
        <Card size="small" styles={{ body: { padding: 8 } }}>
          <Table<ToolFile> rowKey="id" size="small" loading={loading} columns={columns as any} dataSource={files}
            tableLayout="fixed" scroll={{ x: 960 }}
            pagination={false} locale={{ emptyText: '暂无工具包——运维上传后自动出现' }} />
        </Card>

        {/* 2026-09-04：工具包详情浮窗（简介全文/文件信息/下载——表格行简介过长不撑高，点「查看」看全文） */}
        <DraggableModal title={`${viewing?.title || ''} · 工具包详情`} open={!!viewing}
          onCancel={() => setViewing(null)} footer={null} width={560}>
          {viewing && (
            <div style={{ fontSize: 13, lineHeight: 1.8 }}>
              <div style={{ fontWeight: 600, marginBottom: 8 }}><Icon as={FileZip} /> {viewing.title || viewing.filename}</div>
              <div style={{ color: 'var(--text-1)', marginBottom: 12 }}>{viewing.description || '（无简介说明）'}</div>
              <div style={{ fontSize: 12, color: 'var(--text-3)', marginBottom: 14 }}>
                文件：{viewing.filename}（{fmtSize(viewing.size)}）
                {viewing.mtime ? ` · 更新于 ${new Date(viewing.mtime * 1000).toLocaleDateString()}` : ''}
              </div>
              <Button type="primary" icon={<Icon as={DownloadSimple} />}
                onClick={() => { download(viewing); setViewing(null) }}>下载</Button>
            </div>
          )}
        </DraggableModal>
      </div>
    </div>
  )
}
