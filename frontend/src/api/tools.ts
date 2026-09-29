// 工具集页 API（简历初筛/知识库浏览/会议纪要）
import client from './client'
import { API_PREFIX } from './prefix'
import { uploadFileDirect, type StagedFile, type UploadProgress } from '../lib/chunkUpload'

// 分片暂存凭据（/uploads/chunk|complete 上传完成后交给业务接口取件；见 lib/chunkUpload.ts）
export type { StagedFile }

// 2026-09-15：下载统一改**直链**（lib/download.ts）——原 fetch→blob + 错误体解析已随之下线
// （blob 整包进内存、无进度/续传；大 zip 顶内存是用户实测关注点）

export const toolsApi = {
  // ---- 简历初筛 ----
  createResumeBatch(files: File[]) {
    const form = new FormData()
    files.forEach((f) => form.append('files', f))
    return client.post('/tools/resume/batches', form).then((r) => r.data)
  },
  getResumeBatch(batchId: string) {
    return client.get(`/tools/resume/batches/${batchId}`).then((r) => r.data)
  },
  scoreResumeBatch(batchId: string, jd: string, weights: Record<string, number>, topK: number, auxModel?: { platform: string; model: string; thinking?: string | null } | null) {
    return client.post(`/tools/resume/batches/${batchId}/score`,
      { jd, weights, top_k: topK, aux_model: auxModel || null }).then((r) => r.data)
  },
  // 2026-09-15：大文件改直链下载（原 fetch→blob 整包进内存）
  resumeExportUrl(batchId: string) {
    return `${API_PREFIX}/tools/resume/batches/${batchId}/export`
  },
  listResumeBatches() {
    return client.get('/tools/resume/batches').then((r) => r.data)
  },
  deleteResumeBatch(batchId: string) {
    return client.delete(`/tools/resume/batches/${batchId}`).then((r) => r.data)
  },

  // ---- 知识库浏览 ----
  getKbCategories() {
    return client.get('/knowledge/categories').then((r) => r.data)
  },
  kbSearch(q: string, categoryId?: number | null) {
    return client.get('/knowledge/search', { params: { q, category_id: categoryId || undefined } }).then((r) => r.data)
  },
  getKbDocument(docId: number) {
    return client.get(`/knowledge/documents/${docId}`).then((r) => r.data)
  },
  // 三期 M14：员工上传本团队知识文档（上传即生效）
  // 2026-08-24：归属三态——scope=dept 本团队文档（仅 dept_admin/ceo），personal 个人文档（任何人）
  kbUpload(form: FormData, scope: 'dept' | 'personal' = 'dept') {
    form.append('scope', scope)
    return client.post('/knowledge/documents', form, { headers: { 'Content-Type': 'multipart/form-data' } }).then((r) => r.data)
  },
  // ---- 知识库归属三态管理（2026-08-24） ----
  kbCategoryCreate(data: { name: string; parent_id?: number | null; scope: 'dept' | 'personal' }) {
    return client.post('/knowledge/categories', data).then((r) => r.data)
  },
  kbCategoryRename(catId: number, name: string) {
    return client.put(`/knowledge/categories/${catId}`, { name }).then((r) => r.data)
  },
  kbCategoryDelete(catId: number) {
    return client.delete(`/knowledge/categories/${catId}`).then((r) => r.data)
  },
  // 文档改分类（仅改分类；团队文档→全局/本团队分类，个人文档→本人个人分类）
  kbDocRecategorize(docId: number, categoryId: number | null) {
    return client.put(`/knowledge/documents/${docId}`, { category_id: categoryId }).then((r) => r.data)
  },
  kbDocDelete(docId: number) {
    return client.delete(`/knowledge/documents/${docId}`).then((r) => r.data)
  },

  // ---- 会议纪要（2026-08-25）：录音/音频上传 → 转写 → 场景总结 → zip 下载 ----
  // sysFile：双轨录音的线上轨（系统声音），可空=单轨
  // 2026-09-10：>50MB 走分片——file/sysFile 传 null，改传 staged（单请求大 body 会撞
  // 本机 nginx 600m / 部署机 portproxy >650MB）
  uploadMeeting(file: File | null, title: string, sysFile?: File,
                opts: { staged?: StagedFile; stagedSys?: StagedFile; onProgress?: (p: UploadProgress) => void } = {}) {
    const form = new FormData()
    if (file) form.append('file', file)
    if (sysFile) form.append('sys_file', sysFile)
    if (opts.staged) form.append('staged_file', JSON.stringify([opts.staged]))
    if (opts.stagedSys) form.append('staged_sys_file', JSON.stringify([opts.stagedSys]))
    if (title.trim()) form.append('title', title.trim())
    return uploadFileDirect('/tools/meetings', form, { onProgress: opts.onProgress })
  },
  getMeeting(meetingId: string) {
    return client.get(`/tools/meetings/${meetingId}`).then((r) => r.data)
  },
  summarizeMeeting(meetingId: string, scene: string, customPrompt: string,
                   auxModel?: { platform: string; model: string; thinking?: string | null } | null) {
    return client.post(`/tools/meetings/${meetingId}/summarize`,
      { scene, custom_prompt: customPrompt, aux_model: auxModel || null }).then((r) => r.data)
  },
  // 2026-08-25：下载转写稿 zip 包（转写完成即可下载，无需等总结；内含转写.md + 完整音频）
  meetingTranscriptUrl(meetingId: string) {
    return `${API_PREFIX}/tools/meetings/${meetingId}/transcript`
  },
  listMeetings() {
    return client.get('/tools/meetings').then((r) => r.data)
  },
  deleteMeeting(meetingId: string) {
    return client.delete(`/tools/meetings/${meetingId}`).then((r) => r.data)
  },
  // 2026-08-27：failed 记录重新转写（复用已落盘音频）
  retryMeeting(meetingId: string) {
    return client.post(`/tools/meetings/${meetingId}/retry`).then((r) => r.data)
  },
  fetchMeetingScenes() {
    return client.get('/tools/meetings/scenes').then((r) => r.data)
  },
  // 2026-09-15：大文件改直链下载
  meetingArchiveUrl(meetingId: string) {
    return `${API_PREFIX}/tools/meetings/${meetingId}/download`
  },
}
