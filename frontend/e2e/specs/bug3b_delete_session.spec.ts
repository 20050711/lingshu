// @bug3b 用户报告：删除对话后，列表显示为空，但聊天区仍有对话记录。
//
// 三个子场景：
//   A 基础删除：删除后列表空 ⇔ 聊天区必须回空态（核心不变量）
//   B 流式中删除当前会话：deleteSession :131 await DELETE 后 :132 判断 sessionId——抛错/竞态时聊天区残留
//   C Popconfirm 双击连点：DELETE 请求次数与聊天区一致性
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient, apiSessionMessages } from '../helpers/api'
import { waitStreamingStart, waitStreamEnd, countBubbles } from '../helpers/wait'
import { collectErrors } from '../helpers/console'
import { LONG_QUESTION, SHORT_QUESTION } from '../helpers/constants'

test.setTimeout(180_000)

// ---- A：基础删除 ----
test('@bug3b A 基础删除：列表空 ⇔ 聊天区空态一致', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  const api = apiClient(page)
  // 前置清理：删光 chaos01 全部会话（否则残留会话导致「列表空」断言失真）
  const all = await apiListAll(api)
  for (const s of all || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 发 2 条，确保有内容
  for (const q of [SHORT_QUESTION, '第二问']) {
    await page.locator('.chat-input').fill(q)
    await page.keyboard.press('Enter')
    await waitStreamEnd(page)
  }
  const before = await countBubbles(page, 'user')
  expect(before, '前置：应有 2 条 user 消息').toBe(2)

  // 删除当前会话（AntD Popconfirm 确认按钮为英文 OK——项目未配 zhCN locale）
  await page.locator('.session-item.active .toolbar-btn', { hasText: '删除' }).click()
  await page.locator('.ant-popover:visible .ant-btn-primary').click()
  await page.waitForTimeout(1500)

  // 断言：列表空 + 聊天区空态
  const itemCount = await page.locator('.session-item').count()
  const emptyWelcome = await page.getByText('你好', { exact: false }).first().isVisible().catch(() => false)
  const after = await countBubbles(page, 'user')
  console.log(`[bug3b-A] 删除后: 列表项=${itemCount} 聊天区 user 气泡=${after} 欢迎语可见=${emptyWelcome}`)
  expect(itemCount, '[bug3b-A] 会话列表应为空').toBe(0)
  expect(after, '[bug3b-A] BUG确认: 删除后聊天区仍有对话记录').toBe(0)
})

async function apiListAll(api: any) {
  const r = await api.get('/api/v1/chat/sessions')
  return r.json()
}

// ---- B：流式中删除当前会话 ----
test('@bug3b B 流式中删除当前会话：残留与孤儿行核实', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  const sid = (await page.evaluate(() => localStorage.getItem('qa_current_session'))) as string

  // 流式中删除
  await page.locator('.session-item.active .toolbar-btn', { hasText: '删除' }).click()
  await page.locator('.ant-popover:visible .ant-btn-primary').click()
  await page.waitForTimeout(2000)

  const after = await countBubbles(page, 'user')
  const api = apiClient(page)
  const check = await apiSessionMessages(api, sid)
  console.log(
    `[bug3b-B] 流式中删除后: 聊天区 user 气泡=${after}（${after > 0 ? '【残留】' : '已清空'}）| 后端 GET 状态=${check.status}（${check.status === 404 ? '已删除' : '仍存在'}）`,
  )
  // 聊天区不应残留（deleteSession :132-136 清空）；残留 = bug 确认
  expect(after, '[bug3b-B] BUG确认: 删除后聊天区仍有记录').toBe(0)
  expect(check.status, '[bug3b-B] 后端会话应已删除').toBe(404)
})

// ---- C：Popconfirm 确认按钮双击连点 ----
test('@bug3b C 删除确认按钮双击连点：DELETE 请求仅 1 次', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(SHORT_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamEnd(page)

  let deleteCount = 0
  page.on('request', (req) => {
    if (req.method() === 'DELETE' && /\/chat\/sessions\/[^/]+$/.test(req.url())) deleteCount++
  })

  await page.locator('.session-item.active .toolbar-btn', { hasText: '删除' }).click()
  const confirmBtn = page.locator('.ant-popover:visible .ant-btn-primary')
  await confirmBtn.waitFor()
  await confirmBtn.dblclick()
  await page.waitForTimeout(1500)

  const after = await countBubbles(page, 'user')
  console.log(`[bug3b-C] 双击确认: DELETE 请求=${deleteCount} 聊天区 user 气泡=${after}`)
  expect(deleteCount, '[bug3b-C] 双击不应触发多次 DELETE').toBe(1)
  expect(after, '[bug3b-C] 删除后聊天区应清空').toBe(0)
})
