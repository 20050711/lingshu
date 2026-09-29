// 知识库浏览工具（M7）：左侧分类树 + 搜索 + 文档列表 + 内容预览 Modal
// 2026-08-24 归属三态重构：分类树分两区（团队/全局知识库 + 👤 我的个人知识库）；
// 文档表加来源列；行操作按角色与归属（dept 分类/团队文档仅 dept_admin/ceo 可管，个人分类/个人文档全员可管）；
// 上传 Modal 按角色显隐 scope 单选（dept 仅团队管理员，employee 固定 personal）
import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Dropdown, Input, Modal, Popconfirm, Radio, Select, Space, Table, Tag, Tree, message } from 'antd'
import { Books, Info, User } from '@phosphor-icons/react'
import PageHeader from '../../components/PageHeader'
import Icon from '../../components/Icon'
import { errMessage } from '../../api/client'
import { toolsApi } from '../../api/tools'
import { useAuthStore } from '../../stores/authStore'
import DraggableModal from '../../components/DraggableModal'  // C18（2026-08-12）：从文件尾移到头部（残留 import）

interface Category { id: number; name: string; parent_id: number | null; user_id: number | null; dept_id: string | null }
interface Doc {
  id: number
  title: string
  summary?: string
  category_id?: number | null
  file_type?: string
  file_size?: number
  updated_at?: string
  user_id: number | null          // 非空=本人个人文档（接口仅返回本人可见数据）
  department_id: string | null    // 非空=本团队文档；均空=全局文档（业务端只读，归运维管理）
}

export default function KbBrowseTool() {
  const user = useAuthStore((s) => s.user)
  const isDeptManager = user?.role === 'dept_admin' || user?.role === 'ceo'

  const [categories, setCategories] = useState<Category[]>([])
  const [docs, setDocs] = useState<Doc[]>([])
  const [q, setQ] = useState('')
  const [categoryId, setCategoryId] = useState<number | null>(null)
  const [preview, setPreview] = useState<{ title: string; content: string } | null>(null)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [upForm, setUpForm] = useState<{ file: File | null; title: string; category_id?: number; scope: 'dept' | 'personal' }>(
    { file: null, title: '', scope: isDeptManager ? 'dept' : 'personal' },
  )
  // 新建分类（createZone=dept 团队分类 / personal 个人分类；null=弹窗关闭）
  const [createZone, setCreateZone] = useState<'dept' | 'personal' | null>(null)
  const [newCatName, setNewCatName] = useState('')
  // 重命名分类 Modal
  const [renameCat, setRenameCat] = useState<Category | null>(null)
  // 文档改分类 Modal（''=不分类）
  const [recatDoc, setRecatDoc] = useState<Doc | null>(null)
  const [recatCid, setRecatCid] = useState<number | ''>('')
  // 上传类型提示：可关闭（localStorage 记忆）
  const [showTypeTip, setShowTypeTip] = useState(() => localStorage.getItem('kb_type_tip_closed') !== '1')
  const dismissTip = () => {
    setShowTypeTip(false)
    localStorage.setItem('kb_type_tip_closed', '1')
  }

  const loadCats = useCallback(() => {
    toolsApi.getKbCategories().then((d) => setCategories(d.categories)).catch(() => {})
  }, [])

  // C15（2026-08-12）：请求序号 latest-wins——快速切分类/连续搜索时旧响应晚到不得覆盖新结果
  const loadSeq = useRef(0)
  const load = useCallback(async (keyword: string, cid: number | null) => {
    const seq = ++loadSeq.current
    try {
      const data = await toolsApi.kbSearch(keyword, cid)
      if (seq === loadSeq.current) setDocs(data.documents)
    } catch (e) {
      if (seq === loadSeq.current) message.error(errMessage(e))
    }
  }, [])

  useEffect(() => {
    loadCats()
    load('', null)
  }, [loadCats, load])

  // 归属三态分区：区1=全局+本团队分类；区2=本人个人分类（接口只返回本人可见的）
  const globalCats = categories.filter((c) => c.user_id == null && c.dept_id == null)
  const deptCats = categories.filter((c) => c.user_id == null && c.dept_id != null)
  const personalCats = categories.filter((c) => c.user_id != null)

  // 平铺分类 → Tree 数据（parent_id 自引用）；manageable 分类节点挂 hover 菜单（重命名/删除）
  const buildTree = (items: Category[], manageable: (c: Category) => boolean): any[] => {
    const childrenOf = (pid: number | null) => items.filter((c) => c.parent_id === pid)
    const build = (list: Category[]): any[] => list.map((c) => ({
      key: `c-${c.id}`,
      title: manageable(c) ? (
        <Dropdown trigger={['hover']} menu={{
            items: [
              { key: 'rename', label: '重命名' },
              { key: 'delete', label: '删除' },
            ],
            onClick: ({ key, domEvent }) => {
              domEvent.stopPropagation()  // 防止冒泡触发 Tree onSelect
              if (key === 'rename') setRenameCat(c)
              else if (key === 'delete') {
                // 分类删除确认：Dropdown 菜单项内嵌 Popconfirm 不稳定（菜单先关闭），用 Modal.confirm
                Modal.confirm({
                  title: `删除分类「${c.name}」？`,
                  content: '其下文档将变为不分类（文档保留）',
                  okText: '删除', okType: 'danger', cancelText: '取消',
                  onOk: () => doDeleteCat(c),
                })
              }
            },
          }}>
          <span>{c.name}</span>
        </Dropdown>
      ) : <span>{c.name}</span>,
      children: build(childrenOf(c.id)),
    }))
    return build(items.filter((c) => c.parent_id == null))
  }

  const onSelect = (keys: any[]) => {
    const k = keys[0] as string | undefined
    const cid = k?.startsWith('c-') ? Number(k.slice(2)) : null
    setCategoryId(cid)
    load(q, cid)
  }

  // 扩展④（2026-08-12）：预览请求序号——快速点击不同文档时旧响应不得覆盖新预览
  const previewSeq = useRef(0)
  const openPreview = async (doc: Doc) => {
    const seq = ++previewSeq.current
    try {
      const d = await toolsApi.getKbDocument(doc.id)
      if (seq === previewSeq.current) setPreview({ title: d.title, content: d.content || '（无内容）' })
    } catch (e) {
      if (seq === previewSeq.current) message.error(errMessage(e))
    }
  }

  const doCreateCat = async () => {
    if (!createZone) return
    if (!newCatName.trim()) return message.warning('请输入分类名称')
    try {
      await toolsApi.kbCategoryCreate({ name: newCatName.trim(), scope: createZone })
      message.success(createZone === 'dept' ? '团队分类已创建' : '个人分类已创建')
      setCreateZone(null)
      setNewCatName('')
      loadCats()
    } catch (e) { message.error(errMessage(e)) }
  }

  const doRenameCat = async () => {
    if (!renameCat?.name.trim()) return message.warning('请输入分类名称')
    try {
      await toolsApi.kbCategoryRename(renameCat.id, renameCat.name.trim())
      message.success('已更新')
      setRenameCat(null)
      loadCats()
      load(q, categoryId)  // 文档表分类列文案同步
    } catch (e) { message.error(errMessage(e)) }
  }

  const doDeleteCat = async (c: Category) => {
    try {
      await toolsApi.kbCategoryDelete(c.id)
      message.success('分类已删除')
      loadCats()
      if (categoryId === c.id) { setCategoryId(null); load(q, null) }  // 当前选中分类被删 → 回到全部
      else load(q, categoryId)
    } catch (e) { message.error(errMessage(e)) }
  }

  const doRecat = async () => {
    if (!recatDoc) return
    try {
      await toolsApi.kbDocRecategorize(recatDoc.id, recatCid === '' ? null : Number(recatCid))
      message.success('分类已更新')
      setRecatDoc(null)
      load(q, categoryId)
    } catch (e) { message.error(errMessage(e)) }
  }

  const doDeleteDoc = async (doc: Doc) => {
    try {
      await toolsApi.kbDocDelete(doc.id)
      message.success('已删除')
      load(q, categoryId)
    } catch (e) { message.error(errMessage(e)) }
  }

  const upload = () => {
    if (!upForm.file) return message.warning('请选择文件')
    const fd = new FormData()
    fd.append('file', upForm.file)
    fd.append('title', upForm.title)
    if (upForm.category_id != null) fd.append('category_id', String(upForm.category_id))
    toolsApi.kbUpload(fd, upForm.scope)
      .then(() => {
        message.success(upForm.scope === 'dept' ? '已上传（本团队可见）' : '已上传（个人知识库）')
        setUploadOpen(false)
        setUpForm({ file: null, title: '', scope: isDeptManager ? 'dept' : 'personal' })
        load(q, categoryId)
      })
      .catch((e) => message.error(errMessage(e)))
  }

  // 上传/改分类可选分类（按归属过滤）：dept→全局+本团队分类；personal→本人个人分类
  const scopeCats = upForm.scope === 'dept' ? [...globalCats, ...deptCats] : personalCats
  const recatOptions = recatDoc?.user_id != null
    ? [{ value: '', label: '不分类' }, ...personalCats.map((c) => ({ value: c.id, label: c.name }))]
    : [{ value: '', label: '不分类' }, ...globalCats.map((c) => ({ value: c.id, label: c.name })),
       ...deptCats.map((c) => ({ value: c.id, label: c.name }))]

  const columns = [
    { title: '标题', dataIndex: 'title', render: (v: string, r: Doc) => (
      <a onClick={() => openPreview(r)} style={{ color: 'var(--brand-ink)' }}>{v}</a>
    ) },
    { title: '来源', dataIndex: 'user_id', width: 80, render: (v: number | null, r: Doc) =>
      v != null ? <Tag color="blue">个人</Tag>
        : r.department_id != null ? <Tag color="green">本团队</Tag>
          : <Tag color="purple">全局</Tag> },
    { title: '分类', dataIndex: 'category_id', width: 120, render: (v: number | null) => categories.find((c) => c.id === v)?.name || '-' },
    { title: '类型', dataIndex: 'file_type', width: 70 },
    { title: '更新时间', dataIndex: 'updated_at', width: 120, render: (v: string) => v ? v.slice(0, 10) : '-' },
    {
      title: '操作', key: 'actions', width: 130,
      render: (_: unknown, r: Doc) => {
        // 个人行（接口只返回本人可见文档 → 个人行即本人）：所有人可改分类/删除；
        // 团队行：仅 isDeptManager；全局文档业务端只读（后端 403，归运维管理）
        const canManage = r.user_id != null || (isDeptManager && r.department_id != null)
        if (!canManage) return null
        return (
          <Space size={4}>
            <Button size="small" type="link" onClick={() => { setRecatDoc(r); setRecatCid(r.category_id ?? '') }}>改分类</Button>
            <Popconfirm title="确认删除该文档？" onConfirm={() => doDeleteDoc(r)}>
              <Button size="small" type="link" danger>删除</Button>
            </Popconfirm>
          </Space>
        )
      },
    },
  ]

  return (
    <div>
      <PageHeader title={<><Icon as={Books} size={18} /> 知识库浏览</>} backLabel="返回定制化工具"
        sub="按分类浏览团队与个人知识文档，支持关键词搜索、内容预览与上传（个人知识库全员可传）" />
      {/* 上传类型提示（可关闭，localStorage 记忆） */}
      {showTypeTip && (
        <div className="card" style={{ padding: '10px 14px', marginBottom: 12, display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 12, color: 'var(--text-2)', flex: 1 }}>
            <Icon as={Info} size={12} /> 上传说明：支持 pdf/docx/pptx/txt/md/xlsx/csv；表格文件（xlsx/csv）仅生成摘要，不做全文切分；
            团队文档仅团队管理员可上传，<span style={{ color: 'var(--brand-ink)' }}>个人文档仅本人可见</span>。
          </span>
          <Button size="small" type="link" onClick={dismissTip}>我知道了</Button>
        </div>
      )}

      <div style={{ display: 'flex', gap: 16 }}>
        <div className="card" style={{ width: 250, padding: 12, height: 'fit-content' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}>团队/全局知识库</span>
            {isDeptManager && (
              <Button size="small" type="link" style={{ fontSize: 12, padding: 0 }}
                onClick={() => { setCreateZone('dept'); setNewCatName('') }}>+ 新建分类</Button>
            )}
          </div>
          <Tree
            treeData={buildTree([...globalCats, ...deptCats], (c) => c.dept_id != null)}
            onSelect={onSelect}
            defaultExpandAll
            style={{ fontSize: 12 }}
          />
          <div style={{ borderTop: '1px solid var(--border)', margin: '10px 0' }} />
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
            <span style={{ fontSize: 13, fontWeight: 600 }}><Icon as={User} size={14} /> 我的个人知识库</span>
            <Button size="small" type="link" style={{ fontSize: 12, padding: 0 }}
              onClick={() => { setCreateZone('personal'); setNewCatName('') }}>+ 新建分类</Button>
          </div>
          <Tree
            treeData={buildTree(personalCats, () => true)}
            onSelect={onSelect}
            defaultExpandAll
            style={{ fontSize: 12 }}
          />
        </div>

        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <Input.Search
              placeholder="搜索文档标题/内容关键词"
              allowClear
              onSearch={(v) => { setQ(v); load(v, categoryId) }}
              style={{ maxWidth: 400 }}
            />
            <Button type="primary" size="small" onClick={() => setUploadOpen(true)}>+ 上传文档</Button>
          </div>
          <div className="card" style={{ padding: 12 }}>
            <Table size="small" rowKey="id" dataSource={docs} columns={columns as any}
              pagination={{ pageSize: 10 }}
              locale={{ emptyText: isDeptManager
                ? '暂无文档（可在右上角上传团队或个人文档）'
                : '暂无文档（团队文档仅团队管理员可上传，可在右上角上传个人文档）' }} />
          </div>
        </div>
      </div>

      {/* 上传 Modal：dept 仅团队管理员可选；分类下拉按 scope 联动 */}
      <DraggableModal open={uploadOpen} title={isDeptManager ? '上传知识文档' : '上传个人文档'} onCancel={() => setUploadOpen(false)} onOk={upload}>
        <input type="file" accept=".pdf,.docx,.pptx,.txt,.md,.xlsx,.csv"
          onChange={(e) => setUpForm((v) => ({ ...v, file: e.target.files?.[0] || null }))} />
        <input className="login-input" placeholder="标题（留空取文件名）" value={upForm.title}
          onChange={(e) => setUpForm((v) => ({ ...v, title: e.target.value }))} />
        {isDeptManager && (
          <Radio.Group size="small" style={{ marginTop: 8 }} value={upForm.scope}
            onChange={(e) => setUpForm((s) => ({ ...s, scope: e.target.value, category_id: undefined }))}>
            <Radio.Button value="dept">团队</Radio.Button>
            <Radio.Button value="personal">个人</Radio.Button>
          </Radio.Group>
        )}
        <Select style={{ width: '100%', marginTop: 8 }} placeholder="分类（可选）" value={upForm.category_id ?? undefined}
          options={scopeCats.map((c) => ({ value: c.id, label: c.name }))}
          onChange={(v) => setUpForm((s) => ({ ...s, category_id: v }))} />
        {upForm.scope === 'personal' && (
          <div style={{ fontSize: 12, color: 'var(--text-3)', marginTop: 8 }}>个人文档仅本人可见；Agent 检索时会合并个人与团队知识</div>
        )}
      </DraggableModal>

      {/* 新建分类（区1 团队 / 区2 个人） */}
      <DraggableModal open={!!createZone} title={createZone === 'dept' ? '新建团队分类' : '新建个人分类'}
        onCancel={() => setCreateZone(null)} onOk={doCreateCat}>
        <input className="login-input" placeholder="分类名称" value={newCatName}
          onChange={(e) => setNewCatName(e.target.value)} />
      </DraggableModal>

      {/* 重命名分类 */}
      <DraggableModal open={!!renameCat} title="重命名分类" onCancel={() => setRenameCat(null)} onOk={doRenameCat}>
        <input className="login-input" value={renameCat?.name ?? ''}
          onChange={(e) => setRenameCat((v) => v ? { ...v, name: e.target.value } : v)} />
      </DraggableModal>

      {/* 文档改分类 */}
      <DraggableModal open={!!recatDoc} title={`修改「${recatDoc?.title || ''}」分类`}
        onCancel={() => setRecatDoc(null)} onOk={doRecat}>
        {/* 2026-08-27（bug 修复）：受控 Select 缺 onChange——点击选项后 recatCid 永不更新，改分类不生效 */}
        <Select style={{ width: '100%' }} value={recatCid} options={recatOptions} onChange={(v) => setRecatCid(v)} />
      </DraggableModal>

      <DraggableModal open={!!preview} title={preview?.title} footer={null} width={720} onCancel={() => setPreview(null)}>
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, maxHeight: 480, overflow: 'auto' }}>
          {preview?.content}
        </pre>
      </DraggableModal>
    </div>
  )
}
