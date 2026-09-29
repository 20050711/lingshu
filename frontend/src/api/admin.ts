import client from './client'
import { API_PREFIX } from './prefix'

export const adminApi = {
  overview: () => client.get('/admin/overview').then((r) => r.data),
  users: () => client.get('/admin/users').then((r) => r.data),
  createUser: (data: any) => client.post('/admin/users', data),
  updateUserStatus: (id: number, status: string) => client.put(`/admin/users/${id}/status`, { status }),
  resetPassword: (id: number, password: string) => client.post(`/admin/users/${id}/reset-password`, { password }),
  // 四期重构：设置/取消团队管理员
  updateUserRole: (id: number, role: string) => client.put(`/admin/users/${id}/role`, { role }),
  // 2026-08-07：用户级工具白名单（技能工具细化到每个用户；勾选=允许，与团队级取交集最严生效）
  userTools: (userId: number) => client.get(`/admin/users/${userId}/tools`).then((r) => r.data),
  userToolsPut: (userId: number, tools: string[] | null) => client.put(`/admin/users/${userId}/tools`, { tools }),
  // 2026-08-06：注销用户账号（删除账号与全部数据）
  terminateUser: (id: number) => client.post(`/admin/users/${id}/terminate`),
  // D7（2026-08-14）：管理账号查看用户详细会话记录
  userSessions: (userId: number) => client.get(`/admin/users/${userId}/sessions`).then((r) => r.data),
  sessionMessages: (sessionId: string) => client.get(`/admin/sessions/${sessionId}/messages`).then((r) => r.data),
  configs: () => client.get('/admin/config').then((r) => r.data),
  updateConfig: (key: string, value: string) => client.put(`/admin/config/${key}`, { value }),
  // 2026-09-16：各处上传大小上限（动态配置，项名/说明由后端下发；值为 MB）
  uploadLimits: () => client.get('/admin/upload-limits').then((r) => r.data),
  updateUploadLimits: (values: Record<string, number>) =>
    client.put('/admin/upload-limits', { values }).then((r) => r.data),
  // B4：模型候选目录（LLM 对话 / 视觉识别 / 图片生成 三栏，后端维护不硬编码）
  modelCatalog: () => client.get('/admin/model-catalog').then((r) => r.data),
  feedback: () => client.get('/admin/feedback').then((r) => r.data),
  updateFeedback: (id: number, body: { status?: string; reply?: string }) => client.put(`/admin/feedback/${id}`, body),
  // H2（2026-08-12）：反馈撤销走独立 revoke 接口——PUT status='revoked' 会被后端 Literal 校验
  // 422（合法流转无 revoked）；后端已加 admin 代撤分支（普通用户仍限本人）
  revokeFeedback: (id: number) => client.post(`/feedback/${id}/revoke`),
  logErrors: () => client.get('/admin/logs/errors').then((r) => r.data),
  auditLogs: () => client.get('/admin/audit-logs').then((r) => r.data),
  sessions: () => client.get('/admin/sessions').then((r) => r.data),
  terminateSession: (id: string) => client.post(`/admin/sessions/${id}/terminate`),
  jobs: () => client.get('/admin/jobs').then((r) => r.data),
  // 记忆库（二期 M9）
  memory: () => client.get('/admin/memory').then((r) => r.data),
  memoryActivate: (id: number) => client.post(`/admin/memory/${id}/activate`),
  memoryDelete: (id: number) => client.delete(`/admin/memory/${id}`),
  memoryEdit: (id: number, content: string) => client.put(`/admin/memory/${id}`, { content }),
  memoryThresholds: () => client.get('/admin/memory/thresholds').then((r) => r.data),
  memoryThresholdsPut: (data: any) => client.put('/admin/memory/thresholds', data),
  memoryExport: () => fetch(`${API_PREFIX}/admin/memory/export`).then((r) => (r.ok ? r.blob() : Promise.reject(new Error(`HTTP ${r.status}`)))),  // L11：cookie 自动携带
  // 四期重构：个人记忆（全员 user_memory）
  memoryUserItems: (userId: number) => client.get(`/admin/memory/users/${userId}`).then((r) => r.data),
  memoryUserAdd: (userId: number, body: { mem_type: string; content: string }) => client.post(`/admin/memory/users/${userId}`, body),
  memoryUserItemDelete: (memId: number) => client.delete(`/admin/memory/users/items/${memId}`),
  // 知识库管理（M11 补齐：admin 上传/分类/删除/列表）
  kbCategories: () => client.get('/admin/knowledge/categories').then((r) => r.data),
  kbCategoryCreate: (data: { name: string; parent_id?: number | null; dept_id?: string | null }) => client.post('/admin/knowledge/categories', data),
  kbCategoryUpdate: (id: number, data: { name: string; dept_id?: string | null }) => client.put(`/admin/knowledge/categories/${id}`, data),
  kbCategoryDelete: (id: number) => client.delete(`/admin/knowledge/categories/${id}`),
  kbDocuments: () => client.get('/admin/knowledge/documents').then((r) => r.data),
  // 2026-08-24：归属三态——department_id 指定目标团队（null=全局文档）
  kbDocumentUpload: (form: FormData, departmentId?: string | null) => {
    if (departmentId) form.append('department_id', departmentId)
    return client.post('/admin/knowledge/documents', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
  },
  kbDocumentDelete: (id: number) => client.delete(`/admin/knowledge/documents/${id}`),
  kbDocumentRecategorize: (id: number, categoryId: number | null) =>
    client.put(`/admin/knowledge/documents/${id}/category`, { category_id: categoryId }).then((r) => r.data),
  kbDocumentDetail: (id: number) => client.get(`/admin/knowledge/documents/${id}/detail`).then((r) => r.data),
  // 团队管理（三期 M12：注册/列表/工具白名单）
  deptList: () => client.get('/admin/departments').then((r) => r.data),
  // 4.1：团队选项（全量含 CEO，配置页等下拉使用——deptList 的 dept_stats 会跳过 ceo）
  deptOptions: () => client.get('/admin/dept-options').then((r) => r.data),
  deptCreate: (data: { dept_id: string; name: string }) => client.post('/admin/departments', data),
  deptDelete: (deptId: string) => client.delete(`/admin/departments/${deptId}`),
  deptTools: (deptId: string) => client.get(`/admin/departments/${deptId}/tools`).then((r) => r.data),
  deptToolsPut: (deptId: string, tools: string[]) => client.put(`/admin/departments/${deptId}/tools`, { tools }),
  // 4.1：AI 技能白名单动态元数据（不硬编码工具名，运维看中文名）+ 定制化工具白名单
  toolsMeta: () => client.get('/admin/tools-meta').then((r) => r.data),
  // 2026-08-21：技能管理（全局技能仅运维可管理；团队技能运维可代为上传/删除，启停归团队管理员）
  globalSkills: () => client.get('/skills/files', { params: { dept_id: 'global' } }).then((r) => r.data),
  deptSkills: (deptId: string) => client.get('/skills/files', { params: { dept_id: deptId } }).then((r) => r.data),
  globalSkillUpload: (form: FormData) => client.post('/skills/files', form, { headers: { 'Content-Type': 'multipart/form-data' } }),
  globalSkillToggle: (id: number, status: string) => client.put(`/skills/files/${id}`, { status }),
  globalSkillDelete: (id: number) => client.delete(`/skills/files/${id}`),
  globalSkillReupload: (id: number, form: FormData) => client.post(`/skills/files/${id}/reupload`, form, { headers: { 'Content-Type': 'multipart/form-data' } }),
  deptCustomTools: (deptId: string) => client.get(`/admin/departments/${deptId}/custom-tools`).then((r) => r.data),
  deptCustomToolsPut: (deptId: string, tools: string[]) => client.put(`/admin/departments/${deptId}/custom-tools`, { tools }),
  // 2026-08-24：定制化工具注册表（前端动态加载，替代硬编码）
  customToolsMeta: () => client.get('/admin/custom-tools').then((r) => r.data),
  // 2026-09-02：团队定制化工具白名单（/dtools 板块，默认全团队关闭；勾选=开通）
  deptCustomToolsMeta: () => client.get('/admin/dept-custom-tools').then((r) => r.data),
  deptAllowTools: (deptId: string) => client.get(`/admin/departments/${deptId}/dept-tools`).then((r) => r.data),
  deptAllowToolsPut: (deptId: string, tools: string[]) => client.put(`/admin/departments/${deptId}/dept-tools`, { tools }),
  // 数据备份（三期：导出 JSON / 一键导入覆盖）
  dataExport: () => fetch(`${API_PREFIX}/admin/data/export`).then((r) => (r.ok ? r.blob() : Promise.reject(new Error(`HTTP ${r.status}`)))),  // L11：cookie 自动携带
  dataImport: (form: FormData) => client.post('/admin/data/import', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  }).then((r) => r.data),
}
