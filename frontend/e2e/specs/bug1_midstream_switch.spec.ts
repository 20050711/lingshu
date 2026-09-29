// @bug1 用户报告：发送消息后点击会话列表中的该会话（或切到其他会话）→ 消息消失；
// 刷新仍不出现、重登依旧；会话列表能看到会话；agent 称「之前的消息没收到」。
//
// 修复（2026-08-12）：
//   后端：断连降级落库移入独立任务（uncancel 循环无效——uvicorn 落库期间再 cancel），
//         断开后 user 消息后台落库（DB 有行，刷新可恢复）。
//   前端：switchSession 同 id 守卫——点击当前会话项不再 abort + 重拉覆盖（B5），
//         流自然结束正常落库，消息保留。
// 本 spec 由「复现确认态」改为「修复验收态」：点击当前项 → 消息保留（不中断流）→
// 等流自然结束 → DB user 行>0 → 刷新恢复。
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient, apiSessionMessages } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd, countBubbles } from '../helpers/wait'
import { collectErrors } from '../helpers/console'
import { LONG_QUESTION } from '../helpers/constants'

test.setTimeout(180_000)

test('@bug1 发送中点当前会话项 → 消息消失（复现+根因核实）', async ({ page, request }) => {
  // 会话列预设展开（默认收缩）
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  const errs = collectErrors(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 时间线：POST /ask 与 GET /messages 的相对时序（判断 GET 是否早于落库）
  const t0 = Date.now()
  const timeline: string[] = []
  page.on('request', (req) => {
    const dt = Date.now() - t0
    if (req.method() === 'POST' && req.url().includes('/chat/ask')) timeline.push(`t+${dt}ms ask`)
    if (req.method() === 'GET' && /\/chat\/sessions\/[^/]+\/messages$/.test(req.url()))
      timeline.push(`t+${dt}ms getMsg`)
  })

  // 1. 发送长问题（拉长流式窗口）
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)

  // 2. 流式中立即点击当前会话项（对当前会话 switch 自己 → abort + DB 全量重载）
  await page.locator('.session-item.active').first().click()

  // 3. 记录点击后状态（修复后：B5 守卫短路——不 abort 不重拉，消息保留）
  await page.waitForTimeout(1500)
  const afterSwitch = await countBubbles(page, 'user')

  // 4. 后端核实（修复后：点击不中断流 → 等流自然结束，persist_round 正常落库）
  await waitStreamEnd(page)
  const sid = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string
  const api = apiClient(page)
  const msgs = await apiSessionMessages(api, sid)
  const dbUserCount = msgs.ok
    ? (msgs.data.messages || []).filter((m: any) => m.role === 'user').length
    : -1

  // 5. 刷新后恢复情况（恢复 effect：localStorage 有 sessionId → switchSession 从 DB 拉）
  await page.reload()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(2000)
  const afterReload = await countBubbles(page, 'user')

  // 6. 输出诊断与根因判定
  const dbHasRound = dbUserCount >= 1
  const frontendLost = afterSwitch === 0
  console.log(`[bug1] 点后 user 气泡=${afterSwitch}（前端${frontendLost ? '【消息消失】' : '保留'}）`)
  console.log(`[bug1] DB user 行=${dbUserCount}（${dbHasRound ? '已落库' : '【未落库】'}）`)
  console.log(`[bug1] 刷新后 user 气泡=${afterReload}（${afterReload > 0 ? '恢复' : '【仍空】'}）`)
  console.log(`[bug1] 时间线: ${timeline.join(' | ') || '(未捕获到请求)'}`)
  const errText = errs.describe()
  if (errText) console.log(`[bug1] console/pageerror: ${errText}`)

  // 判定 A：后端必须已落库（否则「agent 说没收到」= 消息从未持久化，最严重）
  expect(dbHasRound, '[bug1] 后端降级落库失败：该轮 user 消息从未落库').toBe(true)

  // 判定 B：前端消息保留（消失 = bug 复现，测试失败即 bug 确认态）
  expect(
    afterSwitch,
    `[bug1] BUG确认: 发送中点当前会话项后消息消失（DB已落库=${dbHasRound}，刷新后=${afterReload > 0 ? '可恢复' : '仍空'}）`,
  ).toBeGreaterThan(0)

  // 判定 C：刷新可恢复（DB 有行但刷新仍空 → 恢复链路问题）
  expect(afterReload, '[bug1] 刷新后仍无法恢复（DB 已有行）').toBeGreaterThan(0)

  // 清理
  await apiDeleteCleanup(api, sid)
})

async function apiDeleteCleanup(api: Awaited<ReturnType<typeof apiClient>>, sid: string) {
  if (sid) await api.delete(`/api/v1/chat/sessions/${sid}`).catch(() => {})
}
