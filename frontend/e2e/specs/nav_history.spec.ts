// @navhistory 补测 B1：浏览器前进/后退（history 导航与 QA state 联动）
// 场景 A：QA 发消息后切页 → 浏览器后退回 QA → 消息保留、可继续操作
// 场景 B：后退到 QA 再前进到技能页 → 技能页正常渲染
// 场景 C：QA 流式中浏览器后退 → 回来时流状态（不永久卡死、停止按钮可用）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { waitStreamEnd } from '../helpers/wait'
import { closeSessionsDrawer } from '../helpers/ui'
import { LONG_QUESTION } from '../helpers/constants'

test.setTimeout(240_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@navhistory A 发送后切页再浏览器后退：消息保留', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  // 前置清理：删光残留会话（后退恢复依赖 qa_current_session 指向本测试会话，残留会串）
  const apiPre = apiClient(page)
  const rawPre = await apiPre.get('/api/v1/chat/sessions')
  const listPre = await rawPre.json()
  const preArr = Array.isArray(listPre) ? listPre : (listPre.sessions || [])
  for (const s of preArr) await apiPre.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 2026-09-17：断言找的是唯一标记「历史导航测试问题」，这里就得发它（原来发「你好」、
  // 断言另一个串 → 恒失败，属过时断言）
  await page.locator('.chat-input').fill('历史导航测试问题')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  // 切到技能页
  await page.goto('/skills')
  await page.locator('body').waitFor()
  await page.waitForTimeout(800)

  // 浏览器后退 → /qa
  await page.goBack()
  await page.waitForURL('**/qa', { timeout: 15_000 })
  await page.waitForTimeout(1500)

  // 直接从气泡文本断言（.chat-area/.msg-list 类并不存在，容器兜底选择器会因歧义失败）
  const bubbleTexts = await page.locator('.msg-bubble').allInnerTexts().catch(() => [])
  const bubbles = bubbleTexts.length
  const msgKept = bubbles > 0 && bubbleTexts.some((t) => t.includes('历史导航测试问题'))
  console.log(`[navA] 后退后气泡数=${bubbles} 消息保留=${msgKept}`)
  expect(msgKept, '[navA] 浏览器后退回 QA 后消息应保留').toBe(true)

  // 可继续输入（输入框可用 + 发送成功）
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  await cleanup(page)
})

test('@navhistory B 后退后再前进：技能页正常', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()
  await page.locator('.chat-input').fill('你好')
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  await page.goto('/skills')
  await page.waitForTimeout(800)
  await page.goBack()
  await page.waitForURL('**/qa', { timeout: 15_000 })
  await page.waitForTimeout(800)
  await page.goForward()
  await page.waitForTimeout(1500)
  const url = page.url()
  const bodyLen = (await page.locator('body').innerText()).length
  console.log(`[navB] 前进后 URL=${url} body 长度=${bodyLen}`)
  expect(url).toContain('/skills')
  expect(bodyLen, '[navB] 前进到技能页不应白屏').toBeGreaterThan(100)

  await cleanup(page)
})

test('@navhistory C QA 流式中浏览器后退再回来：无永久卡死', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  // 流式中切走（不等流结束）
  await page.waitForTimeout(800)
  await page.goto('/skills')
  await page.waitForTimeout(500)
  await page.goBack()
  await page.waitForURL('**/qa', { timeout: 15_000 })
  await page.waitForTimeout(2000)

  // 回来：停止按钮应存在（流未结束）或已结束；不卡死（主线程心跳 + 输入框可用）
  const stopCount = await page.locator('.btn-ghost', { hasText: '停止' }).count()
  const inputOk = await page.locator('.chat-input').isEnabled().catch(() => false)
  const alive = await page.evaluate(() => Date.now()).then(() => true).catch(() => false)
  console.log(`[navC] 回来: 停止按钮=${stopCount} 输入可用=${inputOk} 主线程=${alive}`)
  expect(alive, '[navC] 主线程不应卡死').toBe(true)
  expect(inputOk, '[navC] 输入框应可用').toBe(true)
  // 2026-09-18：收尾点击前先关会话抽屉——`.sessions-mask` 会拦截 pointer events
  // （断言本身全过，挂在收尾点击；与 netdata A3 同款处理）
  await closeSessionsDrawer(page)
  if (stopCount > 0) await page.locator('.btn-ghost', { hasText: '停止' }).click({ timeout: 10_000 })

  await cleanup(page)
})
