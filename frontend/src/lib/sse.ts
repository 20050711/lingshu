// SSE 客户端：fetch + ReadableStream（EventSource 不支持 POST body）
// v2（2026-08-14）：mode/intent/result/question/plan 事件；confirm/checkpoint 事件已随授权卡下线移除
// v3（2026-08-18）：任务后台化——断线自动重连（指数退避）+ seq 幂等断点续播
//   - 每帧带 seq（= 后端事件流 index），前端记录 lastSeq（onSeq 回调持久化）
//   - 非 AbortError 断连 → 退避重连（1/2/4/8/16s，5 次上限），重连 body 带 reconnect+last_seq
//   - 主动停止（AbortSignal）→ onAbort，绝不重连（停止=显式取消任务，走 /chat/stop）
//   - EOF 无终态（任务已结束且事件流被 TTL 清空）→ onStale（fallback 重拉 get_messages）
import { getClientId, handleAuthExpired } from '../api/client'
import { API_PREFIX } from '../api/prefix'

export interface AskHandlers {
  onText: (delta: string) => void
  onChart: (d: any) => void
  onTool: (d: any) => void
  onDone: (d: any) => void
  onError: (d: any) => void
  onHeartbeat?: () => void
  onAbort?: (d?: any) => void  // 2026-08-07：用户主动停止（AbortSignal）/ 2026-08-18：aborted 事件（显式停止）
  onMode?: (d: any) => void        // v2：双模式（quick/complex）
  onIntent?: (d: any) => void      // v2：意图预告气泡
  onResult?: (d: any) => void      // v2：结果条（绿勾/红叉+耗时）
  onQuestion?: (d: any) => void    // v2：反问浮窗卡
  onPlan?: (d: any) => void        // v2：计划批准卡（唯一人工门）
  onProgress?: (d: any) => void    // v2：子代理徽标（kind=subagent）
  onSeq?: (seq: number) => void    // v3：收到带 seq 帧（持久化断点，重连续播用）
  onStale?: () => void             // v3：EOF 无终态（任务已在别处收尾，流已清空）→ fallback 重拉
}

export function parseSSEFrame(frame: string): { event: string; data: string } | null {
  const lines = frame.split('\n')
  let event = 'message'
  let data = ''
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim()
    else if (line.startsWith('data:')) data += line.slice(5).trim()
  }
  if (!data) return null
  return { event, data }
}

function dispatch(event: string, d: any, handlers: AskHandlers) {
  switch (event) {
    case 'text': handlers.onText(d.delta ?? ''); break
    case 'chart': handlers.onChart(d); break
    case 'tool': handlers.onTool(d); break
    case 'done': handlers.onDone(d); break
    case 'error': handlers.onError(d); break
    case 'heartbeat': handlers.onHeartbeat?.(); break
    // v2 交互框架事件
    case 'mode': handlers.onMode?.(d); break
    case 'intent': handlers.onIntent?.(d); break
    case 'result': handlers.onResult?.(d); break
    case 'question': handlers.onQuestion?.(d); break
    case 'plan': handlers.onPlan?.(d); break
    case 'progress': handlers.onProgress?.(d); break
    // v3：显式停止（POST /chat/stop 的收尾事件）
    case 'aborted': handlers.onAbort?.(d); break
  }
}

const RETRY_DELAYS = [1000, 2000, 4000, 8000, 16000]  // 指数退避，上限 16s，5 次后放弃
// 退避等待：signal abort（用户点停止/切会话）时立即唤醒，不等满延迟（停止即时响应）
const sleep = (ms: number, signal?: AbortSignal) => new Promise<void>((r) => {
  const t = setTimeout(r, ms)
  if (signal) signal.addEventListener('abort', () => { clearTimeout(t); r() }, { once: true })
})

export async function askQuestion(payload: any, handlers: AskHandlers, signal?: AbortSignal) {
  let lastSeq = -1   // 已消费到的最大 seq（seq=0 是 mode 首帧，必须消费）
  let attempt = 0

  for (;;) {
    const body = attempt === 0
      ? payload
      : { ...payload, reconnect: true, last_seq: lastSeq }  // 重连：后端回放增量 + 尾随 live
    let res: Response
    try {
      res = await fetch(`${API_PREFIX}/chat/ask`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Client-ID': getClientId(),  // L11：token 改 cookie，同源自动携带
        },
        body: JSON.stringify(body),
        signal,
      })
    } catch (e) {
      // B12（2026-08-12）：fetch 网络层 reject（断网/服务不可达）统一纳入
      if (e instanceof DOMException && e.name === 'AbortError') {
        handlers.onAbort?.()
        return
      }
      if (attempt >= RETRY_DELAYS.length) {
        handlers.onError({ code: 'E016', message: '网络中断，请刷新页面恢复' })
        return
      }
      await sleep(RETRY_DELAYS[attempt], signal); attempt++
      continue
    }
    if (!res.ok) {
      const err = await res.json().catch(() => null)
      // 发现 17（2026-08-12）：SSE 401 与 axios 路径一致跳登录（fetch 不经 axios 拦截器）
      if (res.status === 401 && err?.error?.code === 'E006' && !location.pathname.startsWith('/login')) {
        handleAuthExpired()
      }
      // 后端明确拒绝（4xx/5xx）→ 终态，不重连
      handlers.onError({ code: err?.error?.code, message: err?.error?.message || `请求失败 (${res.status})` })
      return
    }
    const reader = res.body!.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    let sawTerminal = false
    // 2026-08-20（滚动卡顿修复②）：text 事件 rAF 合并批量派发——后端节流约 25 次/s，
    // 每帧合并为一次 onText（React 同帧批处理 → render 次数减半以上）；终态/中断路径
    // 必须 flush 残留，否则丢尾字（onText 在 streaming 消息不存在时静默丢弃）
    let textPending = ''
    let textRaf = 0
    const flushText = () => {
      textRaf = 0
      if (textPending) { const t = textPending; textPending = ''; handlers.onText(t) }
    }
    // 2026-08-19（K1 根本缓解）：静默检测——服务端 heartbeat 每 15s 一次，正常连接
    // 必然有帧；若 20s 无任何帧（连接半开/服务端停推/浏览器网络栈冻结——实测复杂任务
    // reject 后 54s 静默），主动 cancel 触发重连（从 lastSeq 回放续播，不依赖服务端）。
    // 与 AbortSignal（用户停止）区分：cancel 引发的 AbortError 带 idleTimeout 标志走重连。
    let idleTimeout = false
    let idleTimer: any = null
    const resetIdle = () => {
      clearTimeout(idleTimer)
      idleTimer = setTimeout(() => {
        idleTimeout = true
        reader.cancel().catch(() => {})  // 取消挂起的 read() → catch 分支走重连
      }, 20_000)
    }
    resetIdle()
    try {
      while (true) {
        const { done, value } = await reader.read()
        resetIdle()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const frames = buf.split('\n\n')
        buf = frames.pop() ?? ''
        for (const frame of frames) {
          const parsed = parseSSEFrame(frame)
          if (!parsed) continue
          let d: any = {}
          try { d = JSON.parse(parsed.data) } catch { d = { raw: parsed.data } }
          // seq 幂等去重（覆盖回放/live 衔接竞态、多连接、重复 reconnect 请求）
          if (typeof d.seq === 'number') {
            if (d.seq <= lastSeq) continue
            lastSeq = d.seq
            handlers.onSeq?.(lastSeq)
          }
          if (parsed.event === 'text') {
            // 批量：累积 delta 到 rAF（同帧多次 text 合并为一次 onText）
            textPending += d.delta ?? ''
            if (!textRaf) textRaf = requestAnimationFrame(() => {
              textRaf = 0
              if (textPending) { const t = textPending; textPending = ''; handlers.onText(t) }
            })
            continue
          }
          // 终态前先 flush 残留文本（否则 done/error 置 streaming=false 后 onText 丢弃尾字）
          if (textPending && (parsed.event === 'done' || parsed.event === 'error' || parsed.event === 'aborted')) {
            if (textRaf) { cancelAnimationFrame(textRaf); textRaf = 0 }
            flushText()
          }
          dispatch(parsed.event, d, handlers)
          if (parsed.event === 'done' || parsed.event === 'error' || parsed.event === 'aborted') {
            sawTerminal = true
            break
          }
        }
        if (sawTerminal) break
      }
      clearTimeout(idleTimer)
    } catch (e) {
      clearTimeout(idleTimer)
      // 中断路径 flush 残留文本（重连从 lastSeq 回放，已消费未 flush 的 delta 不能丢）
      if (textRaf) { cancelAnimationFrame(textRaf); textRaf = 0 }
      flushText()
      // 用户主动停止（AbortSignal）→ 静默复位，不显示"网络中断"错误
      // （idleTimeout 的 cancel 也抛 AbortError——但那是静默检测触发的重连，不走 onAbort）
      if (e instanceof DOMException && e.name === 'AbortError' && !idleTimeout) {
        handlers.onAbort?.()
        return
      }
      // S7/v3：流中断（网络断开/服务重启/切后台）→ 指数退避重连，从 lastSeq 续播
      if (attempt >= RETRY_DELAYS.length) {
        handlers.onError({ code: 'E016', message: '网络中断，请刷新页面恢复' })
        return
      }
      attempt++
      await sleep(RETRY_DELAYS[attempt - 1], signal)
      continue
    }
    if (sawTerminal) return
    // EOF 路径 flush 残留（流结束前的最后一个 batch）
    if (textRaf) { cancelAnimationFrame(textRaf); textRaf = 0 }
    flushText()
    // EOF 且无终态：任务已在别处收尾且事件流被清空（或瞬态 EOF）——先退避重试一次，
    // 重连拿到空流仍无终态 → onStale 兜底重拉 get_messages（DB 有该轮记录）
    if (attempt === 0 && !signal?.aborted) {
      attempt++
      await sleep(RETRY_DELAYS[attempt - 1], signal)
      continue
    }
    handlers.onStale?.()
    return
  }
}
