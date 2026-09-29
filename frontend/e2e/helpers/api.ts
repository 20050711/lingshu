// 后端核实 API：会话/消息查询与清理（区分「前端丢 / 后端没落库 / 纯时序」的核心手段）
// 2026-08-13 单点登录适配：不再独立 apiLogin——同账号再登录会 token_version+1 踢掉页面 UI
// 的 token（页面随即 401 跳登录，整批 spec 连环失败）。改用 page.context().request：
// 与页面共享 cookie，天然复用 UI 登录态，零新登录、零互踢。
import type { Page } from '@playwright/test'

/** 复用页面登录态的后端核实 API client（无新登录，无互踢） */
export function apiClient(page: Page) {
  return page.context().request
}

/** 返回会话数组（后端 GET /chat/sessions 直接返回数组，非 {sessions: []}） */
export async function apiListSessions(request: APIRequestContext): Promise<any[]> {
  const r = await request.get('/api/v1/chat/sessions')
  if (!r.ok()) throw new Error(`GET /chat/sessions ${r.status()}: ${await r.text()}`)
  return r.json()
}

export async function apiSessionMessages(request: APIRequestContext, sid: string) {
  const r = await request.get(`/api/v1/chat/sessions/${sid}/messages`)
  return { ok: r.ok(), status: r.status(), data: r.ok() ? await r.json() : null }
}

export async function apiDeleteSession(request: APIRequestContext, sid: string) {
  await request.delete(`/api/v1/chat/sessions/${sid}`)
}

/** 清理：删除 chaos01 全部会话（fuzz 收尾用） */
export async function apiDeleteAllSessions(request: APIRequestContext) {
  const sessions = await apiListSessions(request)
  for (const s of sessions || []) await apiDeleteSession(request, s.id)
}

/** 提取某会话全部 user 消息文本（bug1/bug3a 根因核实用） */
export function userTexts(messages: any[]): string[] {
  return (messages || []).filter((m) => m.role === 'user').map((m) => String(m.content || ''))
}
