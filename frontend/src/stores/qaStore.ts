// 功能问答页状态（v2 交互框架：双模式/事件流/反问卡/批准卡/todo 清单/插话）
import { create } from 'zustand'
import { askQuestion } from '../lib/sse'
import client from '../api/client'
// 2026-09-15：大文件分片上传（>50MB 切片——单请求大 body 会撞 portproxy/nginx/浏览器不可续传三层门槛）
import { CHUNK_THRESHOLD, ChunkedUploadError, resumeChunkedUpload, uploadFileChunked,
         type ChunkedUploadResume, type StagedFile, type UploadProgress as UploadProg } from '../lib/chunkUpload'

// 2026-09-01：手动模型选择持久化（刷新/重开保持；登出清——防跨账号残留）
const savedModelSel = (() => {
  try {
    const raw = localStorage.getItem('qa_model_sel')
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
})()

export interface Message {
  role: 'user' | 'assistant'
  content: string
  round_id?: number
  outputs?: any[]
  streaming?: boolean
  sysHint?: string
  // label = 后端随事件下发的中文工具名（2026-09-24：调用过程/结束不许出现英文 id）
  toolEvents?: { tool_name: string; label?: string; status: string; detail?: string; brief?: string; output?: string; args?: string; subagent_id?: string }[]
  toolCollapsed?: boolean
  elapsedS?: number  // 4.1：本轮回复总耗时（done 事件携带，气泡下方展示）
  // v2：小事件意图/结果（消息级渲染，与工具明细折叠区分开）
  intents?: { text: string; scope: string; step_index?: number }[]
  results?: { text: string; ok: boolean; duration_s?: number; scope: string; step_index?: number }[]
  // 2026-08-18：事件时间线（预告→工具→小结按到达顺序，含 kind 标记；历史从 tool_events 重建）
  timeline?: {
    kind: 'intent' | 'tool' | 'result' | 'interrupt'  // interrupt：流式插话（2026-08-20，插在当前执行步骤处）
    text?: string; ok?: boolean; duration_s?: number; scope?: string; step_index?: number
    tool_name?: string; label?: string; status?: string; detail?: string; brief?: string; subagent_id?: string
    output?: string  // 2026-08-20：子代理完整回复（tool 事件 output 字段，原漏拷致不显示）
  }[]
  interrupt?: boolean  // v2：插话气泡（流式中发送的用户消息）
  startedAt?: number   // 流式起点时间戳
  files?: any[]
}

export interface Round {
  round_id: number
  outputs: any[] // {type,label,chart_id,chart_type,option,file_path}
}

export interface QuestionItem {
  text: string
  options: string[]
  multi_select?: boolean
  recommended?: number[]
}

export interface QuestionState {
  question_id: string
  questions: QuestionItem[]
  timeout_s: number
  deadline: number  // 前端倒计时用（本地时间戳）
}

export interface PlanCardState {
  plan_id: string
  goal: string
  steps: { id: string; title: string; intent: string; verify: string }[]
  risks: string[]
  revision: number
  status: 'pending' | 'confirmed'
  expires_at?: string | null
  readonly?: boolean  // 跨 ask 恢复的只读态（已超时/待重开）
}

export interface ProgressAgent {
  agent: string
  status: 'running' | 'done' | 'failed'
  summary: string
}

// 2026-09-15：上下文水位与压缩状态（GET /chat/sessions/{id}/context；进度条 + 压缩卡片数据源）
export interface CtxStat {
  watermark_tokens: number
  budget_tokens: number
  pct: number
  boundary: number | null   // 已压缩到第几轮（null=从未压缩）
  summary: string | null    // 会话结构化压缩摘要
  rounds: number
  keep_recent: number
  compactible: boolean
  compacting: boolean
}

interface QAState {
  sessions: any[]
  sessionId: string | null
  messages: Message[]
  streaming: boolean
  creatingNew: boolean  // B7：新建会话请求进行中（防连点产生多个空会话）
  activeSkills: string[]
  autoSkill: boolean
  multiTable: boolean
  rounds: Round[]
  currentRound: number
  currentTab: string
  previewOpen: boolean
  previewWidth: number
  fileIds: string[]
  fileNames: string[]
  // v2 交互框架状态
  mode: 'quick' | 'complex'
  questionState: QuestionState | null
  planCard: PlanCardState | null
  todoState: Record<string, 'done' | 'failed'>
  lastStepIndex: number
  progressAgents: ProgressAgent[]
  modelSel: { thinking: 'off' | 'low' | 'high' | 'max' | null; auxOverrides: Record<string, any> }
  interruptedNote: string  // v3（2026-08-18）：服务重启后任务被中断的提示条文案（空=不显示）
  // 2026-09-15：上下文水位（进度条）与压缩状态（阻塞式回合——压缩中禁用发送）
  ctxStat: CtxStat | null
  compacting: boolean
  // 2026-09-15：大文件分片上传状态（进度/失败续传；小文件直传不进这里）
  uploadProg: UploadProg | null
  uploadErr: string | null
  uploadResume: ChunkedUploadResume | null
  // 2026-09-17：本次上传被安全规则跳过（可执行/载荷 + macOS 垃圾）的成员清单——小浮窗展示
  uploadSkipped: { zip: string; name: string; rule: string }[]

  loadSessions: () => Promise<void>
  newSession: () => Promise<void>
  switchSession: (id: string) => Promise<void>
  deleteSession: (id: string) => Promise<void>
  sendQuestion: (question: string, signal?: AbortSignal, opts?: { upgrade?: boolean }) => Promise<void>
  upgradeToComplex: () => Promise<void>  // v2：快速→复杂升级（P0：基于全部对话重新规划）
  abortStream: () => void  // 2026-08-07：中止当前流（切换/删除/新建会话时断开连接；任务后台化后任务在后台继续）
  stopStream: () => Promise<void>  // v3（2026-08-18）：显式停止——POST /chat/stop 取消后台任务（区别于断连）
  // v3（2026-08-18）：刷新/切换后恢复运行中任务的续播；
  // 2026-09-17：fromStart=false 仅供「同页断线重连」用（内存气泡仍在，走增量续播）
  resumeSession: (id: string, opts?: { fromStart?: boolean }) => Promise<void>
  // 2026-09-15：上下文水位刷新（切会话/每轮完成后拉取）+ 手动压缩（POST /chat/sessions/{id}/compact）
  refreshContext: (sessionId?: string) => Promise<void>
  compactContext: () => Promise<{ ok: boolean; error?: string }>
  reset: () => void  // C1（E-01）：登出清内存态（防换账号残留上一账号会话内容）
  answerQuestion: (answers: { index: number; selected: number[]; other_text: string }[], extraText?: string) => Promise<void>
  approvePlan: (decision: 'approve' | 'reject', feedback?: string) => Promise<void>
  dismissQuestion: () => void
  dismissPlanCard: () => void
  setMode: (m: 'quick' | 'complex') => void
  setModelSel: (sel: Partial<QAState['modelSel']>) => void
  uploadFiles: (files: File[]) => Promise<void>
  cancelUpload: () => void
  retryUpload: () => Promise<void>  // 2026-09-15：分片上传失败续传（从断点补片）
  // C16（2026-08-12）：支持函数式更新（SkillsPage 连点防闭包过期）
  setActiveSkills: (ids: string[] | ((prev: string[]) => string[])) => void
  setAutoSkill: (v: boolean) => void
  setMultiTable: (v: boolean) => void
  // 三期 M15：技能勾选按用户持久化（/skills 页与 QA 浮窗同源同步）
  loadSkillPrefs: () => Promise<void>
  saveSkillPrefs: () => Promise<void>
  openPreview: () => void
  closePreview: () => void
  setPreviewWidth: (w: number) => void
  setCurrentRound: (r: number) => void
  setCurrentTab: (t: string) => void
  clearFiles: () => void
  clearUploadSkipped: () => void
  removeFile: (id: string) => void  // C14（2026-08-12）：删除会话文件后同步清待发列表对应项
  toggleToolCollapsed: (m: Message) => void
}

// 当前流的 AbortController（2026-08-07：切换/删除/新建会话时中止——历史遗留"切走再回来空白"根因：
// 切走时旧会话的流式回调继续写 store，切回时加载被并发覆盖）
// v3（2026-08-18 任务后台化）：abort 只断开 SSE 连接（后端转发器注销，agent 任务继续后台执行），
// 显式停止任务走 stopStream（POST /chat/stop）
let streamAbort: AbortController | null = null

// 2026-09-15：分片上传的中断控制器 + 最近一次上传的文件队列（失败重试/续传用）
let uploadAbort: AbortController | null = null
let uploadQueue: File[] = []
// 2026-09-17：resumeSession 重入锁（见该函数注释——并发双流会把时间线回放两遍）
let resumingId: string | null = null

// v3（2026-08-18）：SSE 处理器工厂——sendQuestion 与 resumeSession 共用
// （含会话切换守卫/seq 断点持久化/onStale 兜底重拉）。可变状态放 ctx 闭包共享。
interface AskCtx {
  assistantIdx: number
  roundOutputs: any[]
  lastRound: number
  startTs: number
}

function buildHandlers(set: any, get: any, askSid: string, ctx: AskCtx, onSeq?: (seq: number) => void) {
  return {
    onHeartbeat: () => { /* 保活事件：计时已由前端秒级 interval 接管 */ },
    // v3：seq 断点持久化（localStorage），断线重连/刷新恢复续播的起点
    onSeq: (seq: number) => onSeq?.(seq),
    onText: (delta: string) => {
      if (get().sessionId !== askSid) return  // 2026-08-07：会话已切换，忽略旧流回调
      set((s: any) => {
        ctx.assistantIdx = s.messages.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx < 0) return s
        const msgs = [...s.messages]
        msgs[ctx.assistantIdx] = { ...msgs[ctx.assistantIdx], content: msgs[ctx.assistantIdx].content + delta }
        return { messages: msgs }
      })
    },
    onChart: (d: any) => {
      if (get().sessionId !== askSid) return  // 2026-08-07：会话已切换，忽略旧流回调
      ctx.lastRound = d.round_id || ctx.lastRound
      const item = { type: 'chart', label: d.label, chart_id: d.chart_id, chart_type: d.type, option: d.option }
      // B8：同轮内按 (chart_type, chart_label) 去重——最新覆盖（浏览区只显示最新一份）
      const idx = ctx.roundOutputs.findIndex((o: any) => o.type === 'chart' && o.chart_type === d.type && o.label === d.label)
      if (idx >= 0) ctx.roundOutputs[idx] = item
      else ctx.roundOutputs.push(item)
    },
    onMode: (d: any) => {
      if (get().sessionId !== askSid) return
      set({ mode: d.mode === 'complex' ? 'complex' : 'quick' })
    },
    onIntent: (d: any) => {
      if (get().sessionId !== askSid) return
      set((s: any) => {
        ctx.assistantIdx = s.messages.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx < 0) return s
        const msgs = [...s.messages]
        const item = { kind: 'intent' as const, text: d.text, scope: d.scope, step_index: d.step_index }
        msgs[ctx.assistantIdx] = {
          ...msgs[ctx.assistantIdx],
          intents: [...(msgs[ctx.assistantIdx].intents || []), item],
          timeline: [...(msgs[ctx.assistantIdx].timeline || []), item],
        }
        // scope=step：步骤级 intent（todo_step start）→ 推进当前步骤高亮
        const extra: any = {}
        if (d.scope === 'step' && d.step_index != null) extra.lastStepIndex = d.step_index
        return { messages: msgs, ...extra }
      })
    },
    onResult: (d: any) => {
      if (get().sessionId !== askSid) return
      set((s: any) => {
        ctx.assistantIdx = s.messages.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx < 0) return s
        const msgs = [...s.messages]
        const item = { kind: 'result' as const, text: d.text, ok: d.ok, duration_s: d.duration_s, scope: d.scope, step_index: d.step_index }
        msgs[ctx.assistantIdx] = {
          ...msgs[ctx.assistantIdx],
          results: [...(msgs[ctx.assistantIdx].results || []), item],
          timeline: [...(msgs[ctx.assistantIdx].timeline || []), item],
        }
        // scope=step：步骤级 result（todo_step done/fail）→ 清单勾选
        const extra: any = {}
        if (d.scope === 'step' && d.step_id) {
          extra.todoState = { ...s.todoState, [d.step_id]: d.ok ? 'done' : 'failed' }
        }
        return { messages: msgs, ...extra }
      })
    },
    onQuestion: (d: any) => {
      if (get().sessionId !== askSid) return
      set({ questionState: { question_id: d.question_id, questions: d.questions || [], timeout_s: d.timeout_s || 120, deadline: Date.now() + (d.timeout_s || 120) * 1000 } })
    },
    onPlan: (d: any) => {
      if (get().sessionId !== askSid) return
      if (d.status === 'confirmed') {
        set({ planCard: { plan_id: d.plan_id, goal: d.goal, steps: d.steps || [], risks: d.risks || [], revision: d.revision || 1, status: 'confirmed' } })
        return
      }
      set((s: any) => {
        // 修订重提（revision>1）：按 step_id 保留已完成项（done/failed 均保留，供用户看到历史）；
        // 全新计划（revision=1，含前一轮计划完成后的下一个任务）→ 重置 todoState——
        // 2026-08-14 反馈：新计划沿用旧计划的 s1..sn 完成态，清单被错误标记为已完成
        const rev = d.revision || 1
        const newIds = new Set((d.steps || []).map((st: any) => st.id))
        const todoState = rev > 1
          ? Object.fromEntries(Object.entries(s.todoState).filter(([k]) => newIds.has(k)))
          : {}
        return {
          planCard: { plan_id: d.plan_id, goal: d.goal, steps: d.steps || [], risks: d.risks || [], revision: rev, status: 'pending', expires_at: d.expires_at },
          todoState,
        }
      })
    },
    // v2：子代理徽标（kind=subagent）
    onProgress: (d: any) => {
      if (get().sessionId !== askSid) return
      if (d.kind !== 'subagent') return
      set((s: any) => {
        const others = s.progressAgents.filter((p: any) => p.agent !== d.agent)
        return { progressAgents: [...others, { agent: d.agent, status: d.status, summary: d.summary || '' }] }
      })
    },
    onTool: (d: any) => {
      if (get().sessionId !== askSid) return  // 2026-08-07：会话已切换，忽略旧流回调
      set((s: any) => {
        ctx.assistantIdx = s.messages.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx < 0) return s
        const msgs = [...s.messages]
        const cur = msgs[ctx.assistantIdx]
        const toolEvents = [...(cur.toolEvents || [])]
        const timeline = [...(cur.timeline || [])]
        // 支柱 1（2026-08-10）：子代理内部事件与主循环同名工具去重键隔离——
        // 去重键 = (subagent_id ?? 'main') + tool_name（否则子代理 run_script 与主循环 run_script 互相覆盖）
        const key = (d.subagent_id ?? 'main') + ':' + d.tool_name
        const existing = toolEvents.findIndex((t: any) => ((t.subagent_id ?? 'main') + ':' + t.tool_name) === key && t.status !== 'done' && t.status !== 'error')
        const ev = { kind: 'tool' as const, tool_name: d.tool_name, label: d.label, status: d.status, detail: d.detail, brief: d.brief, args: d.args, subagent_id: d.subagent_id, output: d.output }
        if (existing >= 0) toolEvents[existing] = ev
        else toolEvents.push(ev)
        // timeline：start/进行中追加一行；done/error 更新最后一条未完成同 key 行（跨轮同名工具各占一行）
        const tlExisting = timeline.findIndex((t: any) => t.kind === 'tool'
          && ((t.subagent_id ?? 'main') + ':' + t.tool_name) === key && t.status !== 'done' && t.status !== 'error')
        if (tlExisting >= 0) timeline[tlExisting] = ev
        else timeline.push(ev)
        msgs[ctx.assistantIdx] = { ...cur, toolEvents, timeline }
        return { messages: msgs }
      })
    },
    onDone: (d: any) => {
      if (get().sessionId !== askSid) return  // 2026-08-07：会话已切换，忽略旧流回调
      // v3：seq 基线跨轮保留（不清除）——同会话新一轮事件 seq 接续旧轮末尾，lastSeq 单调幂等
      ctx.lastRound = d.round_id || ctx.lastRound
      // v2：done 同步模式 + 清卡片；B8（D20）：plan_status 兼容旧流程
      const ps = d.plan_status
      set((s: any) => ({
        mode: d.mode === 'complex' ? 'complex' : 'quick',
        questionState: null,
        // done 时若批准卡仍 pending（超时收尾）→ 转只读保留，发送消息重开
        planCard: s.planCard && s.planCard.status === 'pending'
          ? { ...s.planCard, readonly: true }
          : (ps === 'pending' ? s.planCard : null),
      }))
      // done 事件的 outputs 已剥离 option（省流量）——chart 保留累积数据，
      // image/doc/file 等非 chart 类型合并进来（否则预览区看不到图片/文档）
      const extras = (d.outputs || []).filter((o: any) => o.type !== 'chart')
      // B8 扩展：非 chart 产出物按 (type, label/file_path) 去重——最新覆盖（同文件只显示最新一份）
      const seen = new Map<string, any>()
      for (const o of ctx.roundOutputs) {
        const key = o.type === 'chart' ? `chart|${o.chart_type}|${o.label}` : `${o.type}|${o.label || o.file_path}`
        seen.set(key, o)
      }
      for (const o of extras) seen.set(`${o.type}|${o.label || o.file_path}`, o)
      ctx.roundOutputs = Array.from(seen.values())
      // 4.1：总耗时（后端 elapsed_s 优先，回退前端计时）
      const elapsed = d.elapsed_s != null ? Math.round(Number(d.elapsed_s)) : Math.floor((Date.now() - ctx.startTs) / 1000)
      set((s: any) => {
        const msgs = [...s.messages]
        ctx.assistantIdx = msgs.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx >= 0) {
          const outputs = ctx.roundOutputs.filter((o: any) => o.type === 'chart' || o.type === 'image')
          let hint: string | undefined
          if (outputs.length) {
            const labels = outputs.map((o: any) => (o.type === 'image' ? '图片' : o.label)).join('、')
            // 2026-09-18：去掉 💡 前缀（全站图标统一 Phosphor，消息文案里不再夹 emoji；
            // 该行渲染处已用主色小字，无需再靠图标强调）
            hint = `已生成 ${labels} · 点击查看`
          }
          msgs[ctx.assistantIdx] = { ...msgs[ctx.assistantIdx], streaming: false, sysHint: hint, outputs: ctx.roundOutputs, elapsedS: elapsed, toolCollapsed: false }
        }
        const rounds = [...s.rounds.filter((r: any) => r.round_id !== ctx.lastRound), { round_id: ctx.lastRound, outputs: ctx.roundOutputs }].filter((r: any) => r.outputs.length > 0)
        // 2026-08-14 反馈：只有**当前轮**确实有产出物才自动打开浏览区
        // （原 rounds.length>0 按历史累计判断——用户自己关了浏览区，问个简单问题又被打开）
        const openPreview = ctx.roundOutputs.length > 0
        return { messages: msgs, streaming: false, rounds, previewOpen: openPreview, currentRound: ctx.lastRound, currentTab: ctx.roundOutputs[0]?.type || 'chart' }
      })
      get().loadSessions()
      get().refreshContext(askSid).catch(() => {})  // 2026-09-15：本轮后刷新上下文水位
    },
    onError: (d: any) => {
      if (get().sessionId !== askSid) return  // 2026-08-07：会话已切换，忽略旧流回调
      // v3：seq 基线跨轮保留（不清除）
      set((s: any) => {
        const msgs = [...s.messages]
        ctx.assistantIdx = msgs.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx >= 0) msgs[ctx.assistantIdx] = { ...msgs[ctx.assistantIdx], content: d.message || '请求失败', streaming: false }
        // 会话失效自愈：后端返回"会话不存在/无权访问"（admin 终止/已删除/跨账号）→
        // 清空本地会话与待发文件，防刷新恢复的旧 sessionId 导致后续上传/发送持续报错
        const msg = d?.message || ''
        let extra: any = {}
        if (msg.includes('会话不存在') || msg.includes('无权访问该会话')) {
          localStorage.removeItem('qa_current_session')
          // B10（2026-08-12）：同时清空 fileIds/fileNames——残留会让 sendQuestion
          // 跳过自动建会话（!sessionId && !fileIds.length 不成立），以 null session 发问
          extra = { sessionId: '', fileIds: [], fileNames: [] }
        }
        return { messages: msgs, streaming: false, questionState: null, ...extra }
      })
    },
    // 2026-08-07：用户点击"停止"——静默复位（不显示错误），消息标记已中断
    // v3：显式停止 = POST /chat/stop 的 aborted 事件（同样走此分支）；断连不再触发（重连循环接管）
    onAbort: () => {
      if (get().sessionId !== askSid) return  // 2026-08-07：会话已切换，忽略旧流回调
      // v3：seq 基线跨轮保留（不清除）
      set((s: any) => {
        const msgs = [...s.messages]
        ctx.assistantIdx = msgs.findIndex((m: any) => m.streaming)
        if (ctx.assistantIdx >= 0) msgs[ctx.assistantIdx] = { ...msgs[ctx.assistantIdx], streaming: false, sysHint: '已停止生成', toolCollapsed: false }
        return { messages: msgs, streaming: false, questionState: null }
      })
    },
    // v3：EOF 无终态（任务已结束且事件流被 TTL 清空/服务重启）→ fallback 重拉 get_messages
    onStale: () => {
      if (get().sessionId !== askSid) return
      get().switchSession(askSid).catch(() => {})
    },
  }
}

export const useQAStore = create<QAState>((set, get) => ({
  sessions: [],
  // 当前会话持久化：浏览器后台标签页被丢弃重载（tab discard）后恢复，避免消息"不见"
  sessionId: localStorage.getItem('qa_current_session') || null,
  messages: [],
  streaming: false,
  creatingNew: false,
  // 2026-09-17：默认技能去掉 sql_query（数据查询线下线）；与后端 /skills 的 DEFAULT_TOOLS 对齐
  activeSkills: ['generate_chart', 'file_parse', 'doc_export', 'run_script'],
  autoSkill: false,
  multiTable: false,
  rounds: [],
  currentRound: 0,
  currentTab: '',
  previewOpen: false,
  // 浏览区宽度：localStorage 持久化（关闭重开/刷新均保持上次展开大小）
  previewWidth: Number(localStorage.getItem('preview_width')) || 420,
  fileIds: [],
  fileNames: [],
  // v2 交互框架
  mode: 'quick',
  questionState: null,
  planCard: null,
  todoState: {},
  lastStepIndex: 0,
  progressAgents: [],
  // 2026-09-01：modelSel 持久化（localStorage）——手动选的主模型/思考档刷新后保持
  // （原纯内存：刷新即回团队配置档，表现为"切换不生效"）
  modelSel: savedModelSel || { thinking: null, auxOverrides: {} },
  interruptedNote: '',
  // 2026-09-15：上下文水位/压缩（压缩中禁用发送——阻塞式回合）
  ctxStat: null,
  compacting: false,
  uploadProg: null,
  uploadErr: null,
  uploadResume: null,
  uploadSkipped: [],

  loadSessions: async () => {
    try {
      const r = await client.get('/chat/sessions')
      set({ sessions: r.data })
    } catch { /* 忽略 */ }
  },

  newSession: async () => {
    streamAbort?.abort(); streamAbort = null  // 2026-08-07：中止当前流（防旧流回调污染）
    const { creatingNew } = get()
    if (creatingNew) return  // B7：请求进行中忽略重复点击（防连点暴增的守卫，保留）
    // A2（2026-08-12，BUG-2 修复）：移除「空会话复用」提前 return——空会话（无消息无文件）
    // 点「+ 新建」应真正新建（原只清面板不新建，配合 BUG-1 断连落库失败时表现为"无法新建"）；
    // 连点防护由 creatingNew 标志承担
    set({ creatingNew: true })
    try {
      const r = await client.post('/chat/sessions', { client_id: localStorage.getItem('client_id') })
      localStorage.setItem('qa_current_session', r.data.session_id)
      set({
        sessionId: r.data.session_id, messages: [], rounds: [], currentRound: 0, previewOpen: false,
        mode: 'quick', questionState: null, planCard: null, todoState: {}, lastStepIndex: 0, progressAgents: [],
        ctxStat: null, compacting: false,  // 2026-09-15：新会话无水位/压缩态
      })
      get().loadSessions()
    } finally {
      set({ creatingNew: false })
    }
  },

  deleteSession: async (id) => {
    // C2（E-03）：仅删除当前会话才中止流——删除其他历史会话不应打断正在生成的会话
    if (get().sessionId === id) {
      streamAbort?.abort(); streamAbort = null
    }
    await client.delete(`/chat/sessions/${id}`)
    if (get().sessionId === id) {
      // 删除当前会话 → 进入空态（不强制新建/刷新；发送消息时自动建）
      localStorage.removeItem('qa_current_session')
      set({
        sessionId: '', messages: [], rounds: [], currentRound: 0, previewOpen: false,
        mode: 'quick', questionState: null, planCard: null, todoState: {}, lastStepIndex: 0,
      })
    }
    get().loadSessions()
  },

  switchSession: async (id) => {
    // B5（2026-08-12，BUG-1 前端触发路径）：流式中点击当前会话项直接短路——
    // 原无条件 abort + GET 重拉会清掉流式中的气泡（getMsg 早于后端降级落库完成 → 空覆盖）。
    // 注意：刷新恢复场景（messages 空）必须放行——sessionId 已从 localStorage 初始化，
    // 若短路则消息永远不拉取（@bug1/@race T1/T2 刷新后 0 的根因）
    if (id === get().sessionId && get().messages.length) return
    streamAbort?.abort(); streamAbort = null  // 2026-08-07：切换会话中止当前流（修复"切走再回来空白"）
    // 历史脏数据防御：早期 JSONB 双重编码落库的字段是字符串，统一解析为对象
    const parseJson = (v: any): any => {
      if (typeof v === 'string') {
        try { return JSON.parse(v) } catch { return undefined }
      }
      return v
    }
    try {
      const r = await client.get(`/chat/sessions/${id}/messages`)
      // 重建消息（含工具调用记录与文件归属）+ 预览区轮次（图表 option 来自 chart_outputs）
      const msgs = (r.data.messages as any[]).map((m) => {
        // 2026-08-18：tool_events（含 kind 标记）→ timeline 完整还原（预告/工具/小结循环）
        const te = parseJson(m.tool_events) || []
        const timeline = Array.isArray(te)
          ? te.map((e: any) => e?.kind === 'intent'
              ? { kind: 'intent' as const, text: e.text || '', scope: e.scope }
              : e?.kind === 'result'
                ? { kind: 'result' as const, text: e.text || '', ok: !!e.ok, duration_s: e.duration_s, scope: e.scope }
                : { kind: 'tool' as const, tool_name: e.tool_name, label: e.label, status: e.status, detail: e.detail, brief: e.brief, subagent_id: e.subagent_id, output: e.output })
          : []
        return {
          role: m.role,
          content: m.content,
          round_id: m.round_id,
          outputs: parseJson(m.outputs),
          toolEvents: te || undefined,
          timeline,
          files: parseJson(m.files) || undefined,
          toolCollapsed: true,
        }
      })
      const charts = r.data.charts || []
      const roundsMap = new Map<number, any>()
      for (const m of msgs) {
        if (m.round_id == null || !m.outputs?.length) continue
        const round = roundsMap.get(m.round_id) || { round_id: m.round_id, outputs: [] }
        round.outputs = [...round.outputs, ...m.outputs.filter((o: any) => o.type !== 'chart')]
        roundsMap.set(m.round_id, round)
      }
      // B8：同 (chart_type, chart_label) 跨轮去重——最新覆盖（移除旧轮次旧条目，最新条目进它所在轮次）
      const chartMap = new Map<string, any>()
      for (const ch of charts) {
        const key = `${ch.chart_type}|${ch.label}`
        const item = { type: 'chart', label: ch.label, chart_type: ch.chart_type, option: parseJson(ch.option), chart_id: ch.id }
        const prev = chartMap.get(key)
        if (prev) {
          const oldRound = roundsMap.get(prev.round_id)
          if (oldRound) oldRound.outputs = oldRound.outputs.filter((o: any) => !(o.type === 'chart' && o.chart_id === prev.chart_id))
        }
        chartMap.set(key, { ...item, round_id: ch.round_id })
        const round = roundsMap.get(ch.round_id) || { round_id: ch.round_id, outputs: [] }
        // chart_id 用真实 id（PNG 导出 /chat/charts/{id}/png 需要）
        round.outputs.push(item)
        roundsMap.set(ch.round_id, round)
      }
      localStorage.setItem('qa_current_session', id)
      const sess = r.data.session || {}
      const sessMode = sess.mode === 'complex' ? 'complex' : 'quick'
      // v2：pending 计划恢复（批准卡超时/被拒后跨 ask 重开——只读展示，发送消息后走修订路径）
      let planCard: PlanCardState | null = null
      const pj = parseJson(sess.plan_json)
      if (sess.plan_status === 'pending' && pj && pj.steps) {
        planCard = {
          plan_id: pj.plan_id || '', goal: pj.goal || '', steps: pj.steps, risks: pj.risks || [],
          revision: 1, status: 'pending', readonly: true,
        }
      }
      set({
        sessionId: id,
        interruptedNote: '', // 2026-09-08：切会话清空中断提示条（原全局悬留 → 任意会话都弹"上一轮任务被服务中断"）
        messages: msgs,
        rounds: Array.from(roundsMap.values()).sort((a, b) => a.round_id - b.round_id),
        previewOpen: false,
        streaming: false, // 重置流式状态（上个会话若流未正常结束，否则输入框被永久禁用）
        mode: sessMode,
        questionState: null,
        planCard,
        todoState: {},
        lastStepIndex: 0,
        progressAgents: [],
        fileIds: [],
        fileNames: [],
        ctxStat: null,  // 2026-09-15：清旧会话水位（随 refreshContext 回填）
      })
      // v3（2026-08-18）：任务后台化——打开会话时若该会话有运行中任务，自动恢复续播
      // （刷新恢复/手动切回同一会话均生效；resumeSession 内部有 streaming 守卫防重入）
      get().resumeSession(id).catch(() => {})
      get().refreshContext(id).catch(() => {})  // 2026-09-15：拉取上下文水位/压缩状态
    } catch (e: any) {
      // 加载失败给出提示而非静默；会话不存在/无权访问（已被删除/终止）→ 清空本地会话自愈
      const msg = e?.response?.data?.error?.message || ''
      if (msg.includes('会话不存在') || msg.includes('无权访问该会话')) {
        // C21（2026-08-12）：预期分支不打印错误栈，降 console.warn
        console.warn('会话已失效（不存在或无权访问），本地自愈清空', msg)
        localStorage.removeItem('qa_current_session')
        // B10（2026-08-12）：自愈同时清空待发文件——残留 fileIds 会让 sendQuestion 的
        // `!sessionId && !fileIds.length` 不成立而跳过自动建会话，以 null session 发问报错
        set({ sessionId: '', fileIds: [], fileNames: [] })
      } else {
        console.error('加载会话失败', e)
      }
    }
  },

  // 2026-09-15：上下文水位刷新（切会话/每轮完成后拉取；进度条 + 压缩卡片数据源）
  refreshContext: async (sessionId) => {
    const sid = sessionId || get().sessionId
    if (!sid) { set({ ctxStat: null }); return }
    try {
      const r = await client.get(`/chat/sessions/${sid}/context`)
      if (get().sessionId !== sid) return  // 会话已切换，忽略旧回调
      set({ ctxStat: r.data, compacting: !!r.data?.compacting })
    } catch (e) {
      console.warn('上下文水位获取失败', e)
    }
  },

  // 2026-09-15：手动压缩上下文（阻塞式回合：压缩期间后端拒新提问，前端禁用发送）
  compactContext: async () => {
    const sid = get().sessionId
    if (!sid || get().compacting || get().streaming) return { ok: false, error: '当前不可压缩' }
    set({ compacting: true })
    try {
      const r = await client.post(`/chat/sessions/${sid}/compact`)
      if (get().sessionId === sid) set({ ctxStat: r.data })
      return { ok: true }
    } catch (e: any) {
      return { ok: false, error: e?.response?.data?.error?.message || '压缩失败，请重试' }
    } finally {
      if (get().sessionId === sid) set({ compacting: false })
    }
  },

  abortStream: () => {
    streamAbort?.abort()
    streamAbort = null
  },

  // v3（2026-08-18）：显式停止任务（区别于断连——断连任务在后台继续）——
  // POST /chat/stop 取消 agent 任务（后端 relay 写 aborted 事件 → 活跃连接/重连回放收尾），
  // 再 abort fetch 立即复位本地流式态
  stopStream: async () => {
    const sid = get().sessionId
    if (sid) {
      try {
        await client.post('/chat/stop', { session_id: sid })
      } catch { /* 无运行中任务/网络异常：仍 abort 本地复位 */ }
    }
    streamAbort?.abort()
    streamAbort = null
  },

  // v3（2026-08-18）：刷新/切换会话后恢复运行中任务的续播。
  // GET /status → running：插入「任务进行中」占位气泡 + 从 localStorage seq 断点发起
  // reconnect 尾随（回放增量 + live；seq 幂等去重保证不重不丢）；interrupted：置提示条。
  resumeSession: async (id, opts) => {
    const { streaming, sessionId } = get()
    if (streaming || sessionId !== id) return  // 已在流式/已切走：防重复挂接
    // 2026-09-17（并发重入）：`streaming` 要等 /status 回来才置位，两次调用在此窗口内会双双通过
    // ——表现为**两个占位气泡 + 两条 reconnect 流**（开发态 React StrictMode 复跑挂载 effect 实锤：
    // 两次 /chat/ask reconnect 相隔 23ms），时间线被回放两遍（intent/tool/result 各两份）。
    if (resumingId === id) return
    resumingId = id
    try {
      let st: any
      try {
        st = (await client.get(`/chat/sessions/${id}/status`)).data
      } catch { return }  // status 不可达（网络/服务）→ 维持现状，刷新时再试
      if (st?.running) {
        // 2026-09-17（走查 bug：工具调用过程刷新/切会话后消失）：本函数下面**新建两个空气泡**
        // ——内存里的半截时间线此刻已经没了（刷新后从历史重建；历史里没有"运行中"那一轮）。
        // 若仍沿用断点 seq，服务端只回放 (last_seq..] 增量 → **刷新前已发生的工具过程（以及已流出的文本）
        // 永久缺失**。故默认从本任务开头整轮回放：last_seq=-1 → 服务端取 max(-1, start_seq)，
        // 恰好=本轮首个事件（不会混入旧轮，见 chat_service.tail_stream 注释）。
        // 同页断线重连（气泡仍在内存、只是连接断了）传 fromStart:false，保持增量语义防重复。
        const lastSeq = opts?.fromStart === false
          ? (Number(localStorage.getItem('qa_seq:' + id)) || 0)
          : -1
        const ctrl = new AbortController()
        streamAbort = ctrl
        set((s) => ({
          streaming: true,
          questionState: null,
          planCard: null,
          messages: [...s.messages,
            { role: 'user' as const, content: st.question || '（任务进行中）' },
            { role: 'assistant' as const, content: '', streaming: true, startedAt: Date.now(), timeline: [] }],
        }))
        const askSid = id
        const askCtx = { assistantIdx: -1, roundOutputs: [] as any[], lastRound: 0, startTs: Date.now() }
        try {
          await askQuestion(
            { session_id: id, reconnect: true, last_seq: lastSeq },
            buildHandlers(set, get, askSid, askCtx, (seq) => localStorage.setItem('qa_seq:' + askSid, String(seq))),
            ctrl.signal,
          )
        } finally {
          if (!get().questionState) set({ streaming: false })  // 与 sendQuestion 同源兜底
        }
      } else if (st?.task_status === 'interrupted') {
        // 服务重启时任务被标记中断（lifespan 恢复）——提示用户重发，不自动重跑
        set({ interruptedNote: '上一轮任务被服务中断，已停止执行。请重新发送消息继续。' })
      }
    } finally {
      resumingId = null
    }
  },

  sendQuestion: async (question, signal, opts) => {
    const { sessionId, activeSkills, autoSkill, multiTable, fileIds, streaming, questionState, mode, modelSel } = get()
    const upgrade = !!opts?.upgrade
    // v2 常开输入框：流式中发送 = 插话（反问卡在等待 → 作为回答提交；否则入中断队列）
    if (streaming) {
      const qs = questionState
      if (qs) {
        await get().answerQuestion(
          qs.questions.map((q, i) => ({ index: i, selected: [...(q.recommended || [0])], other_text: '' })),
          question,
        )
      } else {
        const sid = get().sessionId
        if (!sid) return
        try {
          await client.post('/chat/interrupt', { session_id: sid, message: question })
        } catch (e) {
          // 2026-08-20（P1-②）：插话发送失败不再静默吞掉——抛带标记错误，
          // 由 ChatPanel send() 恢复输入框并 toast 提示（原 catch 空 + 无条件插气泡：
          // 用户看到"消息消失"但后端从未收到）
          console.error('插话发送失败', e)
          throw Object.assign(new Error('插话发送失败'), { interruptFailed: true })
        }
        set((s) => {
          // 2026-08-20（走查修正）：插话插在「当前执行步骤」处——作为 streaming 消息
          // timeline 的 interrupt 条目（紧跟当前进度点，后续工具事件继续追加其后，标记
          // 插入时刻位置）；原实现插独立气泡在流式消息前，用户看到插话贴在用户问题下方、
          // 与执行位置脱节。流式消息无 timeline（占位/纯文本）时回退独立气泡插在其前
          const idx = s.messages.findIndex((m) => m.streaming)
          const messages = [...s.messages]
          if (idx >= 0 && Array.isArray(messages[idx].timeline)) {
            messages[idx] = {
              ...messages[idx],
              timeline: [...messages[idx].timeline, { kind: 'interrupt' as const, text: question }],
            }
          } else {
            const msg = { role: 'user' as const, content: question, interrupt: true, sysHint: '已插入计划，当前步骤完成后生效' }
            if (idx >= 0) messages.splice(idx, 0, msg)
            else messages.push(msg)
          }
          return { messages }
        })
      }
      return
    }
    // B11（2026-08-12，BUG-4 + 用户侧 1 同源）：streaming 同步置位做原子占位锁——
    // 原代码在 await newSession() 之后才置位，await 期间让出执行权，第二发可绕过检查进入
    // 双 ask 竞态（两个 SSE 流 + streaming 复位错乱 → 停止/发送同时禁用；第二发 sid 仍为
    // null → 422 幽灵流；且第二发先覆盖 streamAbort，第一发停止按钮失效）
    set({ streaming: true })
    // 删除当前会话后处于空态 → 发送时自动建会话（无需用户手动新建）
    if (!sessionId && !fileIds.length) {
      try {
        await get().newSession()
      } catch {
        // 建会话失败（网络/服务异常）→ 复位 streaming，并在消息区给 ⚠️ 提示（2026-08-14：
        // 原静默返回用户无感知——netdata A2 实测断网发送零反馈，BUG-5 契约回归）
        set((s) => ({
          streaming: false,
          messages: [...s.messages,
            { role: 'user', content: question },
            { role: 'assistant', content: '网络中断，已停止接收', streaming: false }],
        }))
        return
      }
    }
    const { sessionId: sid2, fileIds: fids2, fileNames: fnames2 } = get()
    // newSession 未得到会话（异常/自愈残留）→ 复位后返回，不发起 422 幽灵流
    if (!sid2) {
      set({ streaming: false })
      return
    }
    // 2026-08-07：内部创建 AbortController（切换会话由 abortStream 中止；外部 signal 并存兼容）
    const ctrl = new AbortController()
    streamAbort = ctrl
    if (signal) {
      signal.addEventListener('abort', () => ctrl.abort())
    }
    const askSid = sid2
    // v3：seq 基线跨轮保留（不清除）——事件流按会话累积（seq=流 index），
    // 后端首连按任务起始 seq 回放，前端 lastSeq 只需单调递增即幂等
    set((s) => ({
      // 用户消息携带本轮上传文件（归属展示；后端 persist_round 已持久化，刷新可恢复）
      messages: [...s.messages,
        { role: 'user', content: upgrade ? '升级为复杂任务模式（基于以上对话重新规划）' : question, files: fnames2.map((n) => ({ file_name: n })) },
        { role: 'assistant', content: '', streaming: true, startedAt: Date.now(), timeline: [] }],
      streaming: true,
      questionState: null,
      planCard: null,
      fileIds: [],
      fileNames: [],
    }))

    // v3（2026-08-18）：处理器工厂共用（sendQuestion/resumeSession）；seq 断点写
    // localStorage 供刷新/断线重连续播；startTs 为 done elapsed 回退计时起点
    const askCtx = { assistantIdx: -1, roundOutputs: [] as any[], lastRound: 0, startTs: Date.now() }
    // B11/B12（2026-08-12）：finally 兜底复位——askQuestion 任何未走 onError/onAbort 的
    // 异常路径（解析错误等）都必须复位 streaming，否则输入框/按钮永久禁用
    try {
      await askQuestion(
        {
          session_id: sid2, question, active_skills: activeSkills, auto_skill: autoSkill,
          multi_table: multiTable, file_ids: fids2,
          mode: upgrade ? 'complex' : mode,  // v2：会话级模式（后端持久化）
          upgrade: upgrade || undefined,  // P0：升级请求（question 可空，后端替换衔接文本）
          thinking: modelSel.thinking ?? undefined,  // D11/D3：思考强度（null=按模式默认）
          aux_overrides: modelSel.auxOverrides ?? undefined,  // D3：辅助模型覆盖
        },
        buildHandlers(set, get, askSid, askCtx, (seq) => localStorage.setItem('qa_seq:' + askSid, String(seq))),
        ctrl.signal,
      )
    } finally {
      if (!get().questionState) set({ streaming: false })  // 反问卡等待中保持 streaming（输入框常开，发送=回答）
    }
  },

  // v2：快速→复杂升级（P0：基于全部对话重新规划；前端确认后调用，question 由后端替换衔接文本）
  upgradeToComplex: async () => {
    if (get().streaming) return
    await get().sendQuestion('', undefined, { upgrade: true })
  },

  // v2：反问卡提交（POST /chat/answer；E009 过期时清卡提示）
  answerQuestion: async (answers, extraText = '') => {
    const { questionState, sessionId } = get()
    if (!questionState || !sessionId) return
    try {
      await client.post('/chat/answer', {
        session_id: sessionId, question_id: questionState.question_id,
        answers, extra_text: extraText,
      })
    } catch (e: any) {
      const msg = e?.response?.data?.error?.message || '回答已过期'
      set({ questionState: null })
      throw new Error(msg)
    }
    set({ questionState: null })
  },

  // v2：计划批准卡（POST /chat/plan-approve；deferred=批准已生效待下条消息执行）
  approvePlan: async (decision, feedback = '') => {
    const { planCard, sessionId } = get()
    if (!planCard || !sessionId) return
    let deferred = false
    try {
      const r = await client.post('/chat/plan-approve', {
        session_id: sessionId, plan_id: planCard.plan_id, decision, feedback,
      })
      deferred = !!r.data?.deferred
    } catch (e: any) {
      const msg = e?.response?.data?.error?.message || '批准已过期'
      set({ planCard: null })
      throw new Error(msg)
    }
    if (decision === 'approve') {
      set({ planCard: { ...planCard, status: 'confirmed', readonly: !!deferred } })
    } else {
      // 不同意：卡片隐藏等待修订重提（plan 事件会重新弹出）
      set({ planCard: null })
      // 2026-08-19（S1-2）：修订重提期间 SSE 直连可能静默（实测 reject 后 54s 无任何帧
      // 含 heartbeat，revision=2 plan 事件收不到——agent 修订 subagent 运行期间服务端
      // 推帧中断）。兜底：20s 后若仍无新计划卡且流式在跑 → 断开静默连接并 resumeSession
      // 从 Redis 事件流回放（rev=2 plan 已写流，回放可靠；前端 seq 幂等去重保证不重不丢）。
      setTimeout(() => {
        const s = get()
        if (!s.planCard && s.streaming && s.sessionId) {
          streamAbort?.abort(); streamAbort = null
          // 2026-08-19（S1-2）：abort 异步生效（fetch reader 抛错 → finally 置 streaming=false
          // 是微任务）——立即 resumeSession 会被 streaming 守卫挡住；延迟 500ms 等 finally 完成
          setTimeout(() => {
            const s2 = get()
            if (!s2.planCard && s2.sessionId && !s2.streaming) {
              s2.resumeSession(s2.sessionId, { fromStart: false }).catch(() => {})
            }
          }, 500)
        }
      }, 20000)
    }
  },

  dismissQuestion: () => set({ questionState: null }),  // 超时/本地关闭（后端已按推荐项自动提交）
  dismissPlanCard: () => set((s) => (s.planCard ? { planCard: { ...s.planCard, readonly: true } } : {})),
  setMode: (m) => set({ mode: m }),
  setModelSel: (sel) => set((s) => {
    const next = { ...s.modelSel, ...sel }
    try { localStorage.setItem('qa_model_sel', JSON.stringify(next)) } catch { /* 忽略 */ }
    return { modelSel: next }
  }),

  // C1（E-01）：登出清内存态——换账号登录不得看到上一账号的会话消息（localStorage 由 authStore 清；
  // preview_width 为设备偏好保留不清）
  reset: () => {
    streamAbort?.abort(); streamAbort = null
    // K2（2026-08-19）：清 qa_seq 断点（重连回放基线）——防跨账号/跨会话残留导致
    // 新会话重连 seq 错位（todo 勾选丢失）
    const prevSid = get().sessionId
    if (prevSid) localStorage.removeItem('qa_seq:' + prevSid)
    set({
      sessions: [], messages: [], rounds: [], currentRound: 0, currentTab: '',
      previewOpen: false, streaming: false, creatingNew: false,
      mode: 'quick', questionState: null, planCard: null, todoState: {}, lastStepIndex: 0, progressAgents: [],
      fileIds: [], fileNames: [], sessionId: null, interruptedNote: '',
      modelSel: { thinking: null, auxOverrides: {} },
      ctxStat: null, compacting: false,  // 2026-09-15：登出清上下文水位/压缩态
    })
    try { localStorage.removeItem('qa_model_sel') } catch { /* 忽略 */ }
  },

  uploadFiles: async (files) => {
    const { sessionId } = get()
    if (!sessionId) return
    // 2026-09-15：>50MB（音视频 600MB 级）走分片上传（/uploads/chunk → /uploads/complete → staged 取件）；
    // 小文件维持单请求直传（切片对小文件只是白付一次全量 MD5 读盘）
    const big = files.filter((f) => f.size > CHUNK_THRESHOLD)
    const small = files.filter((f) => f.size <= CHUNK_THRESHOLD)
    uploadQueue = files
    const staged: StagedFile[] = []
    if (big.length) {
      uploadAbort = new AbortController()
      set({ uploadErr: null, uploadProg: { phase: 'hash', percent: 0, text: '准备上传…' } })
      try {
        for (const f of big) {
          const rs = get().uploadResume
          const onProgress = (p: UploadProg) => set({ uploadProg: p })
          const res = rs && rs.file === f
            ? await resumeChunkedUpload({ ...rs, opts: { ...rs.opts, signal: uploadAbort.signal, onProgress } })
            : await uploadFileChunked(f, { completeUrl: '/uploads/complete', signal: uploadAbort.signal, onProgress })
          staged.push({ upload_id: res.response.upload_id, file_name: res.response.file_name })
          set({ uploadResume: null, uploadProg: { phase: 'done', percent: 100, text: '上传完成' } })
        }
      } catch (e: any) {
        // 中断可续传（与会议纪要等工具同模型）：保留进度条 + 重试入口（已传分片服务端保留）
        set({
          uploadErr: e?.message || '上传失败',
          uploadResume: e instanceof ChunkedUploadError ? e.resume : null,
        })
        return
      } finally {
        uploadAbort = null
      }
    }
    if (!small.length && !staged.length) {
      set({ uploadProg: null })
      return
    }
    const fd = new FormData()
    fd.append('session_id', sessionId)
    if (staged.length) fd.append('staged_files', JSON.stringify(staged))
    small.forEach((f) => fd.append('files', f))
    try {
      const r = await client.post('/chat/files', fd)
      const skipped = r.data?.extra?.skipped ?? []
      set((s) => ({
        fileIds: [...s.fileIds, ...r.data.files.map((f: any) => f.file_id)],
        fileNames: [...s.fileNames, ...r.data.files.map((f: any) => f.file_name)],
        uploadProg: null, uploadErr: null, uploadResume: null,
        // 2026-09-17：zip 内有可执行/载荷或 macOS 垃圾成员被跳过 → 存清单，界面出小浮窗
        uploadSkipped: skipped.length ? [...s.uploadSkipped, ...skipped] : s.uploadSkipped,
      }))
    } catch (e) {
      set({ uploadProg: null, uploadErr: null, uploadResume: null })
      throw e  // 交调用方 toast（原行为）
    }
  },

  cancelUpload: () => {
    uploadAbort?.abort()
    uploadAbort = null
    set({ uploadProg: null, uploadErr: null, uploadResume: null })
  },

  retryUpload: () => {
    const q = uploadQueue
    set({ uploadErr: null })
    return q.length ? get().uploadFiles(q) : Promise.resolve()
  },

  setActiveSkills: (ids) =>
    set((s) => ({ activeSkills: typeof ids === 'function' ? ids(s.activeSkills) : ids })),
  setAutoSkill: (v) => set({ autoSkill: v }),
  // 三期 M15：加载/保存用户技能勾选（失败回退现有默认，不阻塞对话）
  loadSkillPrefs: async () => {
    try {
      const d = await client.get('/skills/prefs').then((r) => r.data)
      set({ activeSkills: d.enabled_ids, autoSkill: d.auto_skill })
    } catch { /* 后端异常保持默认 */ }
  },
  saveSkillPrefs: async () => {
    const { activeSkills, autoSkill } = get()
    await client.put('/skills/prefs', { enabled_ids: activeSkills, auto_skill: autoSkill })
  },
  setMultiTable: (v) => set({ multiTable: v }),
  openPreview: () => set({ previewOpen: true }),
  // 关闭只收起面板，保留宽度（再次打开保持上次大小）
  closePreview: () => set({ previewOpen: false }),
  setPreviewWidth: (w) => {
    const v = Math.min(800, Math.max(280, w))
    set({ previewWidth: v })
    localStorage.setItem('preview_width', String(v))
  },
  setCurrentRound: (r) => set({ currentRound: r, currentTab: get().rounds.find((x) => x.round_id === r)?.outputs[0]?.type || 'chart' }),
  setCurrentTab: (t) => set({ currentTab: t }),
  clearFiles: () => set({ fileIds: [], fileNames: [] }),
  clearUploadSkipped: () => set({ uploadSkipped: [] }),
  removeFile: (id) =>
    set((s) => {
      const idx = s.fileIds.indexOf(id)
      if (idx < 0) return s
      // fileIds/fileNames 索引一一对应（uploadFiles 同时 push）
      return {
        fileIds: s.fileIds.filter((_, i) => i !== idx),
        fileNames: s.fileNames.filter((_, i) => i !== idx),
      }
    }),
  toggleToolCollapsed: (m) =>
    set((s) => ({
      messages: s.messages.map((x) => (x === m ? { ...x, toolCollapsed: !x.toolCollapsed } : x)),
    })),
}))
