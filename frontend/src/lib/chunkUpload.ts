// 大文件分片上传公共层（2026-09-10 通用化）
//
// 为什么切片：单个请求的大 body 有三层门槛——本机 nginx client_max_body_size 600m、
// 部署机 Windows portproxy 对 >~650MB 长 body 转发失败（400 空体）、后端业务上限。
// 统一做法：>50MB 切片，每片一个请求，任何一层都绕开；服务端逐片齐备校验 + 流式合并
// + 位级校验（总大小 + MD5），见 backend/app/services/upload_chunks.py。
//
// 进度三段（用户 2026-09-10 要求：浏览器侧切片/校验也要可见）：
//   校验（浏览器读整包算 MD5，0-15%）→ 分片上传（15-90%，第 i/N 片）→ 服务端合并校验（90-100%）
import SparkMD5 from 'spark-md5'
import client from '../api/client'

export const CHUNK_SIZE = 50 * 1024 * 1024      // 对齐后端 upload_chunk_max_mb=64

/** 并发片数（2026-09-16）。
 *
 * 背景：实测「客户端 → 部署机」这条网络路径只有 4~6 MB/s（同机自环 16MB/s），单条 TCP 连接受
 * RTT（3~62ms 抖动）限制打不满带宽——exe 侧实测 3 片并发 3.8MB/s > 单片 2.1MB/s。
 * 服务端分片接口本就按并发安全写（不同 index 各写各的文件、`tmp.replace` 原子落盘），
 * 单 worker 下 2 路并行无压力；complete 仍是单请求（服务端另有互斥）。
 * 本机自环上传看不出差别（已是内存级），但无害。
 */
export const CHUNK_PARALLEL = 2
export const CHUNK_THRESHOLD = CHUNK_SIZE       // 超过才切片（小文件直传，省一次 MD5 读盘）
const CHUNK_TIMEOUT = 10 * 60 * 1000            // 单片：慢链路下 50MB 会超过 axios 默认 60s
const COMPLETE_TIMEOUT = 60 * 60 * 1000         // 合并+校验+入库

export type UploadPhase = 'hash' | 'upload' | 'finalize' | 'done'

export interface UploadProgress {
  phase: UploadPhase
  percent: number
  text: string
}

export interface ChunkedUploadResult {
  uploadId: string
  md5: string
  totalChunks: number
  response: any
}

/** 分片暂存凭据（/uploads/complete 返回，交业务接口取件消费） */
export interface StagedFile {
  upload_id: string
  file_name: string
}

/** 续传断点（上传中断时随 ChunkedUploadError 抛给页面，重试=从 nextIndex 续传） */
export interface ChunkedUploadResume {
  file: File
  uploadId: string
  md5: string              // 空串=校验阶段就中断（重试时重新计算）
  totalChunks: number
  nextIndex: number        // 下一个要传的分片；=totalChunks 表示片已齐、只需重发 complete
  opts: ChunkedUploadOpts
}

export class ChunkedUploadError extends Error {
  resume: ChunkedUploadResume
  constructor(message: string, resume: ChunkedUploadResume) {
    super(message)
    this.name = 'ChunkedUploadError'
    this.resume = resume
  }
}

class ChunkFailure extends Error {
  constructor(public index: number, public cause: any) {
    super(`分片 ${index} 上传失败`)
  }
}

export interface ChunkedUploadOpts {
  chunkUrl?: string                             // 默认通用 /uploads/chunk（业务可传自定义端点）
  completeUrl?: string                          // 默认通用 /uploads/complete
  completeFields?: Record<string, string>       // complete 附加字段（业务侧自定义，如 scope/conflict…）
  onProgress?: (p: UploadProgress) => void
  signal?: AbortSignal
}

export function newUploadId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export function isAborted(e: any): boolean {
  return e?.code === 'ERR_CANCELED' || e?.name === 'AbortError'
}

function abortError(): DOMException {
  return new DOMException('上传已取消', 'AbortError')
}

/** 让出主线程一次（进宏任务队列）：渲染/输入/定时器得以执行。 */
function yieldToUi(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** 流式 MD5（WebCrypto 不支持流式 4GB 大文件 → spark-md5）；onTick 回传已读字节供进度。 */
export async function computeFileMd5(file: File, onTick?: (read: number) => void,
                                     signal?: AbortSignal): Promise<string> {
  const spark = new SparkMD5.ArrayBuffer()
  const reader = file.stream().getReader()
  let read = 0
  let lastYield = performance.now()
  try {
    while (true) {
      if (signal?.aborted) throw abortError()
      const { value, done } = await reader.read()
      if (done) break
      const v = value as Uint8Array
      spark.append(new Uint8Array(v).buffer as ArrayBuffer)
      read += v.length
      onTick?.(read)
      // 2026-09-16（走查：大包上传"无响应/卡死，只能关页面"）：read() 的 await 走微任务，
      // 不让位给宏任务——整包算完前主线程 100% 占死（chromium 实测 1.2GB：4.9s 内定时器
      // 触发 0 次，页面不重绘、进度条不动；Windows 约 5s 即标"无响应"）。按时间片让出，
      // 页面全程可响应；实测代价约 +30% 校验耗时（1.2GB：4.9s→6.5s）。
      if (performance.now() - lastYield >= 100) {
        await yieldToUi()
        lastYield = performance.now()
      }
    }
  } catch (e) {
    try { await reader.cancel() } catch { /* 流已关闭 */ }
    throw e
  }
  return spark.end()
}

/** 单个分片上传（失败重试 3 次：2s/4s 退避；取消立即抛出）。 */
async function putChunk(url: string, uploadId: string, index: number, blob: Blob,
                        signal?: AbortSignal): Promise<void> {
  const fd = new FormData()
  fd.append('upload_id', uploadId)
  fd.append('chunk_index', String(index))
  fd.append('chunk', blob, `chunk-${index}`)
  for (let attempt = 0; ; attempt++) {
    try {
      await client.post(url, fd, { timeout: CHUNK_TIMEOUT, signal })
      return
    } catch (e: any) {
      if (signal?.aborted || isAborted(e)) throw abortError()
      if (attempt >= 2) throw e
      await new Promise((r) => setTimeout(r, 2000 * (attempt + 1)))
    }
  }
}

/** 显式取消：请服务端立刻删掉分片目录（否则要等 24h TTL 才清——走查反馈取消后几 GB 躺着）。
 *
 * 尽力而为，失败不影响前端流程；只在「用户主动取消」时调，网络中断仍保留分片供续传。 */
export function cancelChunkedUpload(uploadId: string): void {
  client.delete(`/uploads/chunk/${uploadId}`, { timeout: 5000 }).catch(() => { /* 忽略 */ })
}

/** 服务端阶段进度轮询（complete 挂起期间）。
 *
 * 2026-09-16（走查：进度条钉在 90% 近两分钟，然后直接跳 100）：complete 这一个请求里服务端要做
 * 合并 + 位级校验 + 解压归档，大包分钟级且此前完全无反馈。现由后端在 Redis 写阶段进度，
 * 前端 1s 轮询，把服务端的 0~100 映射到进度条的 90~99。轮询失败不影响 complete 本身。 */
function startFinalizePolling(uploadId: string, opts: ChunkedUploadOpts): () => void {
  let stopped = false
  const tick = async () => {
    if (stopped) return
    try {
      const { data } = await client.get(`/uploads/progress/${uploadId}`, { timeout: 5000 })
      if (!stopped && data?.text) {
        opts.onProgress?.({ phase: 'finalize',
          percent: Math.min(99, 90 + (Number(data.percent) || 0) / 10), text: String(data.text) })
      }
    } catch { /* 轮询是尽力而为：失败就保持上一条文案 */ }
    if (!stopped) setTimeout(tick, 1000)
  }
  setTimeout(tick, 1000)
  return () => { stopped = true }
}

/** complete：服务端齐备校验 + 合并 + 位级校验（幂等——分片保留在服务端，冲突/失败只需重发本请求）。 */
export async function completeChunkedUpload(uploadId: string, file: File, md5: string,
                                            totalChunks: number, opts: ChunkedUploadOpts = {}): Promise<any> {
  const fd = new FormData()
  fd.append('upload_id', uploadId)
  fd.append('total_chunks', String(totalChunks))
  fd.append('total_size', String(file.size))
  fd.append('md5', md5)
  fd.append('file_name', file.name)   // 2026-09-10：原始包名（客户名缺省取包名，防变 "upload"）
  Object.entries(opts.completeFields ?? {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') fd.append(k, v)
  })
  const stopPoll = startFinalizePolling(uploadId, opts)
  try {
    const { data } = await client.post(opts.completeUrl ?? '/uploads/complete', fd,
      { timeout: COMPLETE_TIMEOUT, signal: opts.signal })
    return data
  } finally {
    stopPoll()
  }
}

/** 分片循环：从 from 开始传（跳过已成功的片=续传只补缺口），**CHUNK_PARALLEL 路并发**。
 *
 * 失败抛 ChunkFailure（带**最小的**出错片号）：续传从那里开始（可能重传同一个失败点之后
 * 已成功的片——分片幂等，代价只是几片重复流量，换来实现简单、断点语义单一）。
 * 一片失败后停止派发新片（在途的那几片让它自然收尾），不让整包继续白跑。 */
async function pushChunks(file: File, uploadId: string, totalChunks: number, from: number,
                          opts: ChunkedUploadOpts): Promise<void> {
  const chunkUrl = opts.chunkUrl ?? '/uploads/chunk'
  const par = Math.max(1, Math.min(CHUNK_PARALLEL, totalChunks - from))
  let next = from                    // 下一个待取片号（worker 共享；取值与自增之间无 await）
  let completed = from               // 已完成片数（续传时把已跳过的算进来）
  let failedAt = -1
  let firstErr: unknown = null

  const worker = async (): Promise<void> => {
    for (;;) {
      if (failedAt >= 0 || opts.signal?.aborted) return
      const i = next++
      if (i >= totalChunks) return
      const blob = file.slice(i * CHUNK_SIZE, Math.min((i + 1) * CHUNK_SIZE, file.size))
      try {
        await putChunk(chunkUrl, uploadId, i, blob, opts.signal)
      } catch (e) {
        if (failedAt < 0 || i < failedAt) { failedAt = i; firstErr = e }
        return
      }
      completed++
      opts.onProgress?.({ phase: 'upload', percent: 15 + (completed / totalChunks) * 75,
        text: `上传分片 ${completed}/${totalChunks}` })
    }
  }

  await Promise.all(Array.from({ length: par }, () => worker()))
  if (failedAt >= 0) throw new ChunkFailure(failedAt, firstErr)
}

function hashProgress(opts: ChunkedUploadOpts, file: File) {
  return (read: number) => {
    const ratio = read / Math.max(1, file.size)
    opts.onProgress?.({ phase: 'hash', percent: ratio * 15,
      text: `校验中 ${Math.floor(ratio * 100)}%（浏览器读取整包计算 MD5）` })
  }
}

/** 切片上传入口：返回 { uploadId, md5, totalChunks, response }（response=complete 的结果）。

中断（非取消）抛 ChunkedUploadError，其 resume 断点交 resumeChunkedUpload 从失败片续传。 */
export async function uploadFileChunked(file: File, opts: ChunkedUploadOpts = {}): Promise<ChunkedUploadResult> {
  const uploadId = newUploadId()
  const totalChunks = Math.max(1, Math.ceil(file.size / CHUNK_SIZE))
  const resumeOf = (md5: string, nextIndex: number): ChunkedUploadResume =>
    ({ file, uploadId, md5, totalChunks, nextIndex, opts })
  // 2026-09-17（用户要求）：**校验与上传并行**。原实现"先算完整包 MD5 再开传"——大包要白等
  // 几秒（实测 ~5.4s/GB，500MB 包约 2.7s，进度条停在"校验中"）。md5 只在最后 complete 时
  // 用于位级校验，与分片上传互不依赖，所以两件事同时开跑、complete 前 await 拿到即可。
  // 续传语义不变：中途失败时 resume.md5 可能还是空串 → 续传入口会重算（resumeChunkedUpload 原逻辑）。
  const hashAbort = new AbortController()
  const onUserAbort = () => hashAbort.abort()
  opts.signal?.addEventListener('abort', onUserAbort, { once: true })
  const md5Promise = computeFileMd5(file, undefined, hashAbort.signal)
  md5Promise.catch(() => { /* 拒绝在 runChunks 里统一处理，这里只防"未捕获拒绝" */ })
  try {
    return await runChunks(file, md5Promise, 0, resumeOf)
  } catch (e) {
    hashAbort.abort()          // 上传失败/取消：停掉还在跑的校验，别白烧 CPU
    if (isAborted(e)) { cancelChunkedUpload(uploadId); throw abortError() }
    throw e
  } finally {
    opts.signal?.removeEventListener('abort', onUserAbort)
  }
}

/** 续传：从断点继续（已成功的片不重传；片已齐则只重发 complete）。 */
export async function resumeChunkedUpload(r: ChunkedUploadResume): Promise<ChunkedUploadResult> {
  let md5 = r.md5
  try {
    if (!md5) md5 = await computeFileMd5(r.file, hashProgress(r.opts, r.file), r.opts.signal)
  } catch (e) {
    if (isAborted(e)) throw e
    throw new ChunkedUploadError('读取文件计算校验值失败，可重试', { ...r, md5: '', nextIndex: 0 })
  }
  return await runChunks(r.file, md5, r.nextIndex, (m, n) => ({ ...r, md5: m, nextIndex: n }))
}

/** 分片 + complete 的公共尾段（首次上传与续传共用；出错统一包成可续传的 ChunkedUploadError）。

md5 传字符串（续传路径，值已知）或 Promise（首次上传，校验与上传并行跑着）。 */
async function runChunks(file: File, md5: string | Promise<string>, from: number,
                         resumeOf: (md5: string, nextIndex: number) => ChunkedUploadResume): Promise<ChunkedUploadResult> {
  const md5Known = typeof md5 === 'string' ? md5 : ''
  const { uploadId, totalChunks, opts } = resumeOf(md5Known, from)
  try {
    await pushChunks(file, uploadId, totalChunks, from, opts)
  } catch (e) {
    const idx = e instanceof ChunkFailure ? e.index : from
    if (isAborted((e as ChunkFailure).cause)) {
      cancelChunkedUpload(uploadId)   // 用户主动取消：分片不再续传，立即清掉（别等 24h TTL）
      throw abortError()
    }
    throw new ChunkedUploadError(`第 ${idx + 1}/${totalChunks} 片上传失败（网络中断），可重试续传`,
      resumeOf(md5Known, idx))
  }
  // complete 前取校验值：并行计算时在这里 await——上传通常远慢于本地校验，绝大多数情况早已算完
  let md5Hex = md5Known
  if (typeof md5 !== 'string') {
    try {
      md5Hex = await md5
    } catch (e) {
      if (isAborted(e)) throw e
      // 读文件失败（罕见）：分片已在服务端——重试会重算校验值再直接 complete，不重传分片
      throw new ChunkedUploadError('读取文件计算校验值失败——分片已上传，重试即可继续',
                                   resumeOf('', totalChunks))
    }
  }
  // 2026-09-16（用户要求）：这行原本写死「大包需数十秒，请勿关闭页面」——现在有服务端真实进度，
  // 1s 内就会被轮询结果替换，只留一句简短的过渡文案
  opts.onProgress?.({ phase: 'finalize', percent: 90, text: '服务端合并与校验中…' })
  try {
    const response = await completeChunkedUpload(uploadId, file, md5Hex, totalChunks, opts)
    opts.onProgress?.({ phase: 'done', percent: 100, text: '上传完成' })
    return { uploadId, md5: md5Hex, totalChunks, response }
  } catch (e) {
    if (isAborted(e)) throw e
    // 2026-09-16（走查：拖入非 zip 的大包，界面只说"可能是网络中断"，真原因被吞）：
    // 后端 4xx 的业务原因原样透出（分片不完整 / 校验不符 / zip 解压失败 / 超限…），
    // 否则用户只会对着"重试续传"反复重试一个永远不会成功的包。
    const serverMsg = (e as any)?.response?.data?.error?.message
    // 片已齐（分片保留在服务端）——重试只重发 complete
    throw new ChunkedUploadError(serverMsg || '服务端合并校验失败（可能是网络中断），可重试',
                                 resumeOf(md5Hex, totalChunks))
  }
}

/** 直传（小文件 / 未走切片的业务）：带 onUploadProgress 的普通 multipart，进度单段。

method：默认 POST（新建）；**改已有资源必须传 'put'**——2026-09-10 走查问题：工具包「编辑换文件」
走本函数时被固定成 POST，打到 `POST /admin/tool-downloads/{id}`（后端只注册 PUT）→ 405 →
前端只能显示通用"请求失败，请稍后重试"。凡是"按 id 更新"的业务接口都要显式传 method。 */
export async function uploadFileDirect(url: string, form: FormData,
                                       opts: { onProgress?: (p: UploadProgress) => void; signal?: AbortSignal;
                                               method?: 'post' | 'put' } = {}) {
  const { data } = await client.request({
    url,
    method: opts.method ?? 'post',
    data: form,
    timeout: COMPLETE_TIMEOUT,
    signal: opts.signal,
    onUploadProgress: (e) => {
      const total = e.total || 0
      const ratio = total ? (e.loaded / total) : 0
      opts.onProgress?.({ phase: 'upload', percent: Math.min(99, ratio * 100),
        text: total ? `上传中 ${Math.floor(ratio * 100)}%` : '上传中…' })
    },
  })
  opts.onProgress?.({ phase: 'done', percent: 100, text: '上传完成' })
  return data
}
