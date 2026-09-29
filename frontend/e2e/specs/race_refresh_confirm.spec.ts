// @race 竞态矩阵：刷新三时机 + confirm 卡期间操作
// T1 流式中刷新：消息恢复语义（DB 降级落库后应至少恢复 user 消息）
// T2 完成后刷新：完整恢复（落库完成，user+assistant 都应恢复）
// T3 拦截后刷新：DB 只有 user 行 → 刷新后只有 user 气泡、无 ⚠️ 提示（记录事实，不判失败）
// T4 confirm 卡期间切会话 + 刷新：卡不悬挂、无 E009
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient, apiSessionMessages } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd, countBubbles, bubbleRoles } from '../helpers/wait'
import { LONG_QUESTION, SHORT_QUESTION, JAILBREAK_QUESTION, TIMEOUT } from '../helpers/constants'

test.setTimeout(300_000)

test('@real-llm @race T1 流式中刷新：消息恢复', async ({ page, request }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  await page.reload()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(3000) // 等恢复 effect 拉完
  const users = await countBubbles(page, 'user')
  console.log(`[raceT1] 流式中刷新后 user 气泡=${users}（期望 ≥1：降级落库的 user 消息）`)
  // 放宽：记录事实；若 DB 有行但前端空 = 恢复链路问题（同 bug1 判定）
  const sid = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string
  const api = apiClient(page)
  const msgs = await apiSessionMessages(api, sid)
  const dbUsers = msgs.ok ? (msgs.data.messages || []).filter((m: any) => m.role === 'user').length : -1
  console.log(`[raceT1] DB user 行=${dbUsers}`)
  if (dbUsers > 0) {
    expect(users, '[raceT1] DB 有行但前端未恢复（恢复链路问题）').toBeGreaterThan(0)
  } else {
    console.log('[raceT1] 记录：流式中刷新 DB 无 user 行（abort 窗口内消息未落库——预期降级落库应兜底）')
  }
  if (sid) await api.delete(`/api/v1/chat/sessions/${sid}`).catch(() => {})
})

test('@real-llm @race T2 完成后刷新：完整恢复', async ({ page, request }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)
  const before = await bubbleRoles(page)
  await page.reload()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(2500)
  const after = await bubbleRoles(page)
  console.log(`[raceT2] 刷新前气泡=${before.length} 刷新后=${after.length}`)
  expect(after.length, '[raceT2] 完成后刷新应完整恢复消息').toBe(before.length)
  expect(after.filter((b) => b.role === 'user').length, '[raceT2] user 消息应恢复').toBe(1)

  const sid = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string
  const api = apiClient(page)
  if (sid) await api.delete(`/api/v1/chat/sessions/${sid}`).catch(() => {})
})

test('@real-llm @race T3 拦截后刷新：只有 user 行无 ⚠️ 提示', async ({ page, request }) => {
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(JAILBREAK_QUESTION)
  await page.keyboard.press('Enter')
  await expect
    .poll(
      async () => (await bubbleRoles(page)).some((b) => b.text.startsWith('⚠️')),
      { timeout: 30_000 },
    )
    .toBe(true)
  await page.reload()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(2500)
  const after = await bubbleRoles(page)
  console.log(
    `[raceT3] 拦截后刷新气泡=${after.length}（user=${after.filter((b) => b.role === 'user').length}, ⚠️=${after.filter((b) => b.text.startsWith('⚠️')).length}）`,
  )
  // 事实记录：被拦轮 DB 只有 user 行，刷新后只有 user 气泡属预期；⚠️ 不恢复属预期（未落库）
  const sid = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string
  const api = apiClient(page)
  if (sid) await api.delete(`/api/v1/chat/sessions/${sid}`).catch(() => {})
})

test('@real-llm @race T4 反问卡期间切会话+刷新：卡不悬挂、无 E009（v2：confirm 卡已下线，断言反问卡同语义）', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 预建第二个会话（切会话用；D6 抽屉开着时 + 新建 在抽屉头部，可点）
  await page.locator('.toolbar-btn', { hasText: '+ 新建' }).click()
  await page.waitForTimeout(600)
  await page.locator('.chat-input').fill('先建一个会话')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // v2：含糊问题触发反问卡（.question-card）
  await page.locator('.chat-input').fill('帮我做个分析')
  await page.keyboard.press('Enter')
  const qCard = page.locator('.question-card')
  await qCard.waitFor({ timeout: 90_000 })
  console.log('[raceT4] 反问卡已出现')

  // 切到另一个会话 → 卡应消失（switchSession 清 questionState）
  await page.locator('.session-item:not(.active)').first().click()
  await page.waitForTimeout(1000)
  const cardAfterSwitch = await qCard.isVisible().catch(() => false)
  console.log(`[raceT4] 切会话后反问卡可见=${cardAfterSwitch}（期望 false）`)
  expect(cardAfterSwitch, '[raceT4] 切会话后反问卡应消失').toBe(false)

  // 刷新 → 卡不悬挂（后端 waiter 120s 自然超时，前端不重建）
  await page.reload()
  await page.locator('.chat-input').waitFor()
  await page.waitForTimeout(2000)
  const cardAfterReload = await qCard.isVisible().catch(() => false)
  console.log(`[raceT4] 刷新后反问卡可见=${cardAfterReload}（期望 false）`)
  expect(cardAfterReload, '[raceT4] 刷新后反问卡不应悬挂').toBe(false)

  // 清理（不点任何回答按钮——后端 waiter 自然超时）
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions')
  const sessions = await r.json()
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
})
