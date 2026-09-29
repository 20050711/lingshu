// @race 竞态矩阵：会话切换相关
// 场景 A：qaStore :390 `if (!confirmSeen) set({streaming:false})` 无 sessionId 守卫——
//   A 会话流式中切到 B 并在 B 发送，A 旧流 promise resolve 时会把 B 的 streaming 强置 false：
//   观测点 = B 停止按钮消失但 B 气泡仍是流式态（思考中/内容在涨）→ 无法停止 + 可并发发送
// 场景 B：流式中点当前会话项后立即重发——POST /ask 不得并发双流
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd, bubbleRoles } from '../helpers/wait'
import { LONG_QUESTION, SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(240_000)

test('@race A :390 无守卫竞态——A 流结束强置 B streaming=false', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 1. 建 A1（第一）与 A2（第二）两个会话
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  await page.locator('.toolbar-btn', { hasText: '+ 新建' }).click()
  await page.waitForTimeout(500)
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // 2. 切回 A1（非 active 项 = A1），发长问题
  // 2026-09-17：会话列默认收起（D6 抽屉化）——列表项被左侧导航遮挡，先打开抽屉（幂等）
  if ((await page.locator('.sessions-mask').count()) === 0) {
    await page.locator('.toolbar-btn', { hasText: '会话列表' }).click()
    await page.locator('.sessions-mask').waitFor({ timeout: 5000 })
  }
  await page.locator('.session-item:not(.active)').first().click()
  await page.waitForTimeout(800)
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)

  // 3. 流式中切到 A2（switchSession abort A1 流）
  // 2026-09-17：会话列默认收起（D6 抽屉化）——列表项被左侧导航遮挡，先打开抽屉（幂等）
  if ((await page.locator('.sessions-mask').count()) === 0) {
    await page.locator('.toolbar-btn', { hasText: '会话列表' }).click()
    await page.locator('.sessions-mask').waitFor({ timeout: 5000 })
  }
  await page.locator('.session-item:not(.active)').first().click()
  await page.waitForTimeout(800)

  // 4. 在 A2 发长问题（保持流长，为观测 :390 留窗口）
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)

  // 5. 等 A1 旧 promise resolve（abort 后 ~1-3s 内）→ :390 执行窗口
  await page.waitForTimeout(8000)

  // 6. 观测：B（A2）停止按钮 + 最后气泡状态
  const stopVisible = await page.locator('.btn-ghost', { hasText: '停止' }).isVisible().catch(() => false)
  const bubbles = await bubbleRoles(page)
  const last = bubbles[bubbles.length - 1]
  const lastText = last ? last.text : ''
  // 2026-08-18：思考中文字气泡改为 think 小标签（无文本时显示；有文本/工具则不出现）
  const stillStreamingUI = lastText.includes('think') || lastText.length === 0
  console.log(
    `[raceA] A2 停止按钮可见=${stopVisible} 最后气泡流式态=${stillStreamingUI}（文本前 60 字: ${lastText.slice(0, 60)}）`,
  )

  // 竞态判定：停止按钮消失但气泡仍是流式态 = :390 竞态确认（流被截断、无法停止、可并发发送）
  const raceHit = !stopVisible && stillStreamingUI
  if (raceHit) {
    console.log('[raceA] ROOTCAUSE_CONFIRMED: :390 无守卫把 A2 的 streaming 强置 false——停止按钮消失且流仍在收')
  }
  expect(raceHit, '[raceA] BUG确认: 旧流结束强置新会话 streaming=false（:390 无守卫竞态）').toBe(false)

  // 7. 收尾：若流还在则停止，清理两会话
  if (stopVisible) await page.locator('.btn-ghost', { hasText: '停止' }).click()
  await waitStreamEnd(page).catch(() => {})
  const api = apiClient(page)
  const sessions = await apiListSessionsSafe(api)
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})

test('@race B 流式中点当前会话项后立即重发：无并发双流', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 记录 POST /ask 时间戳（并发 = 两个 ask 重叠）
  const asks: number[] = []
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/chat/ask')) asks.push(Date.now())
  })

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  // 点当前会话项（B5 修复后：不 abort、不重拉——消息保留，流继续）
  await page.locator('.session-item.active').first().click()
  await page.waitForTimeout(1500)
  const kept = await bubbleRoles(page)
  const keptUser = kept.filter((b) => b.role === 'user').length
  console.log(`[raceB] 点击当前项后 user 气泡=${keptUser}（修复目标：保留）`)
  expect(keptUser, '[raceB] 点击当前会话项后消息应保留（B5 修复）').toBeGreaterThan(0)
  // 流继续（停止按钮仍在）→ 等流自然结束 → 重发（流式中无发送按钮，只能结束后重发）
  await waitStreamEnd(page)
  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  console.log(`[raceB] POST /ask 次数=${asks.length}（期望 2：长问题 + 重发）`)
  expect(asks.length, '[raceB] 重发应只有一次新请求').toBe(2)

  const api = apiClient(page)
  const sessions = await apiListSessionsSafe(api)
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})

async function apiListSessionsSafe(api: any) {
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  return r ? r.json() : []
}
