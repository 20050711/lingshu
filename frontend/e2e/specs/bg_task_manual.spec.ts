// @bgmanual 会话任务后台化手动验收固化（2026-08-19，S1-7）
// 覆盖 08-18 后台任务化上线时的手动验收项：切会话任务继续 / 刷新续播 / 停止按钮 /
// 双标签状态——沉淀为自动化（真 LLM flash 档；workers=1）
import { test, expect } from '@playwright/test'
import { loginViaUI, shareAuth } from '../helpers/auth'
import { LONG_QUESTION } from '../helpers/constants'
import { waitStreamingStart } from '../helpers/wait'

test.setTimeout(300_000)

test('@real-llm @bgmanual 切会话任务继续+切回续播', async ({ page, context }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 先建占位会话（流式中「+ 新建」禁用，切换走会话列表）
  await page.locator('.toolbar-btn', { hasText: '新建' }).click()
  await page.waitForTimeout(800)
  await page.locator('.chat-input').fill('占位会话')
  await page.keyboard.press('Enter')
  // 等占位会话完成（短问答）
  await page.locator('.btn-ghost', { hasText: '停止' }).waitFor({ timeout: 30_000 }).catch(() => {})
  await expect(page.locator('.btn-ghost', { hasText: '停止' })).toHaveCount(0, { timeout: 60_000 })

  // 发送长任务（无工具 800 字输出——拉长流式窗口）
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  console.log('[bgm1] 流式已开始')

  // 流式中切到占位会话（任务应在后台继续）
  await page.locator('.session-item', { hasText: '占位会话' }).first().click({ timeout: 10_000 })
  await page.waitForTimeout(1500)
  const inputText = await page.locator('.chat-input').inputValue().catch(() => '')
  console.log('[bgm1] 切到占位会话（输入框内容）:', JSON.stringify(inputText))

  // 切回长任务会话——任务结果应已落库（先关抽屉遮罩，再重开会话列表）
  const mask = page.locator('.sessions-mask')
  if (await mask.count()) await mask.click({ position: { x: 700, y: 400 } }).catch(() => {})
  await page.locator('.toolbar-btn', { hasText: '会话列表' }).click()
  await page.locator('.session-item', { hasText: '用 800 字' }).first().click({ timeout: 10_000 })
  await expect.poll(async () => {
    const bubbles = await page.locator('.msg-bubble').count()
    return bubbles
  }, { timeout: 90_000 }).toBeGreaterThanOrEqual(2)
  console.log('[bgm1] 切回后可见任务消息 ✓')
})

test('@real-llm @bgmanual 刷新页面自动续播（resumeSession）', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 短任务发完后立即刷新（任务可能仍在跑或已 done——刷新恢复语义：resumeSession 或历史加载）
  await page.locator('.chat-input').fill('用一句话介绍你自己')
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  await page.waitForTimeout(2000)  // 流式中刷新
  await page.reload()
  await page.locator('.chat-input').waitFor({ timeout: 30_000 })
  // 刷新后：会话列表存在且消息区能恢复（至少 1 条消息气泡或流式恢复中）
  await expect.poll(async () => {
    const bubbles = await page.locator('.msg-bubble').count()
    const stopping = await page.locator('.btn-ghost', { hasText: '停止' }).count()
    return bubbles + (stopping ? 1 : 0)
  }, { timeout: 90_000 }).toBeGreaterThanOrEqual(2)
  console.log('[bgm2] 刷新后会话/消息恢复 ✓')
})

test('@real-llm @bgmanual 停止按钮显式取消（/chat/stop）', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  // 关抽屉遮罩（qa_sessions_open=1 常开遮罩拦截停止按钮）
  const mask = page.locator('.sessions-mask')
  if (await mask.count()) await mask.click({ position: { x: 700, y: 400 } }).catch(() => {})
  await page.locator('.btn-ghost', { hasText: '停止' }).click()
  // 停止后：停止按钮消失（aborted 事件收尾）
  await expect(page.locator('.btn-ghost', { hasText: '停止' })).toHaveCount(0, { timeout: 30_000 })
  console.log('[bgm3] 停止按钮生效（aborted 收尾）✓')
})

test('@real-llm @bgmanual 双标签页：标签 A 任务在标签 B 可见状态', async ({ page, context }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 标签 B：共享登录态（同一 token，无互踢）
  const pageB = await context.newPage()
  await shareAuth(page, context)
  await pageB.goto('/qa')
  await pageB.locator('.chat-input').waitFor({ timeout: 30_000 })

  // A 发长任务
  await page.locator('.chat-input').fill(LONG_QUESTION)
  await page.keyboard.press('Enter')
  await waitStreamingStart(page)
  await page.waitForTimeout(3000)

  // B 应能看到会话列表中有该会话（任务进行中/已完成——状态可见即可）
  await pageB.locator('.session-item').first().waitFor({ timeout: 15_000 })
  const titles = await pageB.locator('.session-item').allInnerTexts().catch(() => [])
  const found = titles.some((t) => t.includes('用 800 字') || t.includes('MVCC'))
  console.log('[bgm4] 标签 B 会话列表:', titles.slice(0, 2).join(' | '))
  expect(found, '[bgm4] 标签 B 可见标签 A 的任务会话').toBe(true)
  await pageB.close()
})
