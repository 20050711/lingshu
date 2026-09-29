// @uploadintr 补测 B2：QA 页上传文件（filechooser）+ 上传后切页再回
// 场景 A：上传小 .md 文件 → 文件浮窗出现记录
// 场景 B：上传后立即切页再回 → 文件记录保留（不丢）
// 场景 C：无会话时点上传 → 提示"会话已失效"（不静默）
import { test, expect } from '@playwright/test'
import { loginViaUI } from '../helpers/auth'
import { apiClient } from '../helpers/api'
import { closeSessionsDrawer } from '../helpers/ui'

test.setTimeout(180_000)

async function cleanup(page: any) {
  const api = apiClient(page)
  const r = await api.get('/api/v1/chat/sessions').catch(() => null)
  const sessions = r ? await r.json() : []
  for (const s of sessions || []) await api.delete(`/api/v1/chat/sessions/${s.id}`).catch(() => {})
}

test('@uploadintr A 上传小文件 → 文件面板有记录', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  // 先建会话（发送一条消息）
  await page.locator('.chat-input').fill('上传测试')
  await page.keyboard.press('Enter')
  await page.locator('.msg-bubble').first().waitFor({ timeout: 60_000 })

  // D5（2026-08-14）：上传入口并入文件浮窗——先关 D6 抽屉遮罩，再开文件浮窗点「＋ 上传」
  await closeSessionsDrawer(page)
  await page.locator('.toolbar-btn', { hasText: '文件' }).first().click()
  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser', { timeout: 10_000 }),
    page.locator('.btn-ghost', { hasText: '上传' }).click(),
  ])
  await chooser.setFiles({
    name: 'e2e_upload_test.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# 上传测试文件\n这是 e2e 上传测试内容'),
  })

  // 等上传成功（文件面板「本会话暂无上传文件」消失或有文件 badge）
  await page.waitForTimeout(3000)
  const fileBadge = await page.locator('.badge', { hasText: '文件：' }).count()
  console.log(`[uploadA] 消息区文件 badge=${fileBadge}`)

  // 打开文件浮窗看记录（文件列表异步加载——用 toContainText 轮询，避免一次性 innerText 竞态）
  // 2026-08-19：上传后浮窗保持打开（前端 UX：可连续上传）——已开时不再点（toggle 会关掉它）
  const panel = page.locator('.float-panel')
  if (!(await panel.isVisible().catch(() => false))) {
    await page.locator('.toolbar-btn', { hasText: '文件' }).first().click()
  }
  await expect(panel).toBeVisible({ timeout: 10_000 })
  await expect(panel, '[uploadA] 文件浮窗应显示上传的文件').toContainText('e2e_upload_test.md', { timeout: 10_000 })
  console.log('[uploadA] 文件浮窗含上传文件 ✓')

  await cleanup(page)
})

test('@uploadintr B 上传后立即切页再回：文件保留', async ({ page, request }) => {
  await page.addInitScript(() => localStorage.setItem('qa_sessions_open', '1'))
  await loginViaUI(page)
  await page.goto('/qa')
  await page.locator('.chat-input').waitFor()

  await page.locator('.chat-input').fill('上传切页测试')
  await page.keyboard.press('Enter')
  await page.locator('.msg-bubble').first().waitFor({ timeout: 60_000 })

  await closeSessionsDrawer(page)
  await page.locator('.toolbar-btn', { hasText: '文件' }).first().click()
  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser', { timeout: 10_000 }),
    page.locator('.btn-ghost', { hasText: '上传' }).click(),
  ])
  await chooser.setFiles({
    name: 'e2e_interrupt_test.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('切页测试内容'),
  })

  // 立即切页再回（不等上传完成回调）
  await page.goto('/skills')
  await page.waitForTimeout(500)
  await page.goto('/qa')
  await page.waitForTimeout(2500)

  // 2026-08-19：切回后 localStorage qa_sessions_open=1 恢复会话抽屉遮罩——先关再点文件
  await closeSessionsDrawer(page)

  // 文件浮窗应有记录
  await page.locator('.toolbar-btn', { hasText: '文件' }).first().click()
  await expect(page.locator('.float-panel')).toBeVisible({ timeout: 10_000 })
  const panelText = await page.locator('.float-panel').innerText()
  const hasFile = panelText.includes('e2e_interrupt_test.txt')
  console.log(`[uploadB] 切页再回文件保留=${hasFile}`)
  expect(hasFile, '[uploadB] 切页再回上传文件应保留').toBe(true)

  await cleanup(page)
})

// 注：2026-08-12 核实删除——「无会话上传提示」场景与产品设计不符：
// QA 页挂载自动 newSession()（QAPage.tsx:19-23），sessionId 恒非空；
// 该提示只在「会话存在但无效」（BUG-2 场景）触发，已由 bug2 spec 覆盖。

