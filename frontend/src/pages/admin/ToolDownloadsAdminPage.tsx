// 运维「工具下载」管理（2026-09-04）：上传离线工具包 zip + 标题/简介，员工端 /tools/downloads 展示下载
// 结构对齐 FeishuBotTool：卡片列表 + 上传/编辑 Modal + 启用开关 + 删除
// 2026-09-10：>50MB 工具包走分片上传 + 三段进度（工具包上限 500MB，单请求大 body 会撞
// 本机 nginx 600m / 部署机 portproxy 650MB，见 lib/chunkUpload.ts）
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Card, Form, Input, message, Modal, Popconfirm, Switch, Table, Upload } from 'antd'
import { Plus, UploadSimple } from '@phosphor-icons/react'
import { errMessage } from '../../api/client'
import client from '../../api/client'
import UploadProgress from '../../components/UploadProgress'
import Icon from '../../components/Icon'
import {
  CHUNK_THRESHOLD, ChunkedUploadError, isAborted, resumeChunkedUpload, uploadFileChunked,
  uploadFileDirect, type ChunkedUploadResume, type UploadProgress as UploadProg,
} from '../../lib/chunkUpload'

interface ToolFile {
  id: string
  title: string
  description?: string | null
  filename: string
  size: number
  enabled: boolean
  sort_order: number
  created_at: string
}

const fmtSize = (n: number) => (n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`)

export default function ToolDownloadsAdminPage() {
  const { Dragger } = Upload
  const [files, setFiles] = useState<ToolFile[]>([])
  const [loading, setLoading] = useState(false)
  const [editing, setEditing] = useState<ToolFile | null>(null)  // null=新建
  const [formOpen, setFormOpen] = useState(false)
  const [fileList, setFileList] = useState<any[]>([])
  const [form] = Form.useForm()
  // 2026-09-10：上传进度/取消/断点续传（>50MB 分片）
  const [saving, setSaving] = useState(false)
  const [prog, setProg] = useState<UploadProg | null>(null)
  const [upErr, setUpErr] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const retryRef = useRef<(() => void) | null>(null)

  const load = useCallback(() => {
    setLoading(true)
    client.get('/admin/tool-downloads').then((r) => setFiles(r.data?.files || []))
      .catch(() => message.error('列表加载失败')).finally(() => setLoading(false))
  }, [])
  useEffect(() => { load() }, [load])

  const openCreate = () => {
    setEditing(null)
    form.resetFields()
    form.setFieldsValue({ enabled: true, sort_order: 0 })
    setFileList([])
    setFormOpen(true)
  }
  const openEdit = (f: ToolFile) => {
    setEditing(f)
    form.setFieldsValue({ title: f.title, description: f.description || '', enabled: f.enabled, sort_order: f.sort_order })
    setFileList([])
    setFormOpen(true)
  }

  const submit = async (resume?: ChunkedUploadResume) => {
    const v = await form.validateFields()
    const picked = fileList[0]?.originFileObj as File | undefined
    if (!editing && !picked) { message.warning('请选择 zip 工具包文件'); return }
    setSaving(true)
    setUpErr(null)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    let retryable = false
    try {
      const fd = new FormData()
      fd.append('title', v.title || '')
      fd.append('desc', v.description || '')
      fd.append('enabled', v.enabled ? 'true' : 'false')
      fd.append('sort_order', String(v.sort_order || 0))
      if (picked && (resume || picked.size > CHUNK_THRESHOLD)) {
        // 2026-09-10：>50MB 先切片上传，再把暂存凭据交给业务接口取件
        const r = resume
          ? await resumeChunkedUpload({ ...resume, opts: { ...resume.opts, signal: ctrl.signal, onProgress: setProg } })
          : await uploadFileChunked(picked, {
              completeUrl: '/uploads/complete', signal: ctrl.signal, onProgress: setProg,
            })
        fd.append('staged_file', JSON.stringify([{
          upload_id: r.response.upload_id, file_name: r.response.file_name,
        }]))
      } else if (picked) {
        fd.append('file', picked)
      }
      const url = editing ? `/admin/tool-downloads/${editing.id}` : '/admin/tool-downloads'
      // 编辑=按 id 更新（后端只注册 PUT）；新建=POST。2026-09-10 走查问题：原实现统一 POST，
      // 编辑换文件必 405「请求失败，请稍后重试」
      await uploadFileDirect(url, fd, {
        onProgress: setProg, signal: ctrl.signal, method: editing ? 'put' : 'post',
      })
      message.success(editing ? '已保存' : '已上传')
      setFormOpen(false)
      load()
    } catch (e) {
      if (isAborted(e)) message.info('已取消上传')
      else if (e instanceof ChunkedUploadError) {
        retryable = true
        setUpErr(e.message)
        retryRef.current = () => submit(e.resume)
      } else message.error(errMessage(e))
    } finally {
      setSaving(false)
      abortRef.current = null
      if (!retryable) setProg(null)
    }
  }

  const doDelete = (f: ToolFile) => {
    client.delete(`/admin/tool-downloads/${f.id}`).then(() => { message.success('已删除'); load() })
      .catch((e) => message.error(errMessage(e)))
  }

  const toggleEnabled = (f: ToolFile, checked: boolean) => {
    const fd = new FormData()
    fd.append('title', f.title)
    fd.append('desc', f.description || '')
    fd.append('enabled', String(checked))
    fd.append('sort_order', String(f.sort_order))
    client.put(`/admin/tool-downloads/${f.id}`, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
      .then(() => load()).catch((e) => message.error(errMessage(e)))
  }

  const columns = [
    { title: '名称', dataIndex: 'title', render: (v: string) => <span style={{ fontWeight: 600 }}>{v}</span> },
    { title: '简介', dataIndex: 'description', render: (v: string | null) => v || <span style={{ color: 'var(--text-3)' }}>-</span> },
    { title: '文件', dataIndex: 'filename', render: (v: string, f: ToolFile) => <span style={{ fontSize: 12 }}>{v}<span style={{ color: 'var(--text-3)' }}>（{fmtSize(f.size)}）</span></span> },
    { title: '启用', dataIndex: 'enabled', width: 90, render: (v: boolean, f: ToolFile) => <Switch size="small" checked={v} onChange={(c) => toggleEnabled(f, c)} /> },
    {
      title: '操作', key: 'actions', width: 140,
      render: (_: unknown, f: ToolFile) => (
        <span>
          <Button size="small" type="link" style={{ padding: 0, marginRight: 8 }} onClick={() => openEdit(f)}>编辑</Button>
          <Popconfirm title={`删除「${f.title}」？`} description="将同时删除文件本体" onConfirm={() => doDelete(f)}>
            <Button size="small" type="link" danger style={{ padding: 0 }}>删除</Button>
          </Popconfirm>
        </span>
      ),
    },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
        <div>
          <h2 style={{ fontSize: 18, marginBottom: 4 }}>工具下载</h2>
          <p style={{ fontSize: 12, color: 'var(--text-3)', margin: 0 }}>上传离线工具包（zip）与简介说明，员工端「定制化工具 → 工具下载」安装使用</p>
        </div>
        <Button type="primary" icon={<Icon as={Plus} />} onClick={openCreate}>上传工具包</Button>
      </div>
      <Card size="small" styles={{ body: { padding: 8 } }}>
        <Table<ToolFile> rowKey="id" size="small" loading={loading} columns={columns as any} dataSource={files}
          pagination={false} locale={{ emptyText: '还没有工具包，点击右上角「上传工具包」开始' }} />
      </Card>

      <Modal title={editing ? `编辑「${editing.title}」` : '上传工具包'} open={formOpen}
        onCancel={() => { setFormOpen(false); setUpErr(null); setProg(null); retryRef.current = null }}
        onOk={() => submit()} okText={editing ? '保存' : '上传'} confirmLoading={saving} width={560} destroyOnClose>
        <Form form={form} layout="vertical" style={{ marginTop: 12 }}>
          <Form.Item name="title" label="工具包名称" rules={[{ required: true, message: '请输入名称' }]}>
            <Input placeholder="如：数据导出工具" maxLength={100} />
          </Form.Item>
          <Form.Item name="description" label="简介说明">
            <Input.TextArea rows={3} placeholder="如：支持 Windows 10 及以上；需管理员权限安装" maxLength={2000} />
          </Form.Item>
          <Form.Item name="sort_order" label="排序（数字越小越靠前）" initialValue={0}>
            <Input type="number" style={{ width: 160 }} />
          </Form.Item>
          <Form.Item name="enabled" label="员工端可见" valuePropName="checked" initialValue={true}>
            <Switch />
          </Form.Item>
          <Form.Item label={editing ? '更换文件（不选则保持不变）' : '工具包文件（.zip）'} required={!editing}>
            <Dragger beforeUpload={() => false} maxCount={1}
              onRemove={() => setFileList([])}
              onChange={({ fileList: fl }) => setFileList(fl.slice(-1))}
              fileList={fileList} accept=".zip">
              <p className="ant-upload-drag-icon"><Icon as={UploadSimple} size={48} className="anticon" /></p>
              <p className="ant-upload-text">点击或拖拽 zip 工具包</p>
              <p className="ant-upload-hint">单文件 ≤500MB；仅支持 zip 格式；&gt;50MB 自动分片上传（含进度，可取消）</p>
            </Dragger>
          </Form.Item>
          {/* 2026-09-10：上传进度（>50MB 分片；中断可续传） */}
          {prog && (
            <UploadProgress progress={prog} error={upErr ?? undefined}
              onRetry={upErr ? () => { setUpErr(null); retryRef.current?.() } : undefined}
              onCancel={upErr
                ? () => { setUpErr(null); setProg(null); retryRef.current = null }
                : () => abortRef.current?.abort()} />
          )}
        </Form>
      </Modal>
    </div>
  )
}
